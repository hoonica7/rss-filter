"""Restore checkpoint state, with an explicit, idempotent historical-backlog import.

Only checkpoint data is read from Git history. This script never checks out or
executes historical code, calls Gemini, publishes output, or sends email.
"""
import argparse
import copy
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

CHECKPOINT_FILES = {
    'last_failed_journal.txt', 'partial_email_content.txt',
    'partial_briefing_records.json', 'partial_classification_count.json',
    'filtered_titles.txt', 'briefing.html', 'slides.html',
    'filtered_results.html', 'index.html', 'pending_classification_queue.json',
    'recovered_backlog_refs.json',
}
QUEUE_FILE = 'pending_classification_queue.json'
MARKER_FILE = 'recovered_backlog_refs.json'


def title_fingerprint(title):
    """Review hint only; title equality never silently discards an article."""
    text = html.unescape(re.sub(r'<[^>]+>', '', str(title or '')))
    text = re.sub(r'\(arXiv:\s*\S+?\)', '', text, flags=re.I)
    text = re.sub(r'\b(?:arxiv|doi):\s*\S+', '', text, flags=re.I)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    text = text.translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹', '01234567890123456789'))
    return ''.join(c for c in text.lower() if c.isalnum())


def identity(record):
    # Match the classifier's link -> id -> title identity priority.
    for key in ('link', 'id'):
        if record.get(key):
            return key + ':' + record[key].strip()
    title = title_fingerprint(record.get('title'))
    return 'title:' + title if title else ''


def validate_queue(queue):
    if not isinstance(queue, dict):
        raise ValueError('Checkpoint queue must be an object keyed by journal.')
    for journal, records in queue.items():
        if not isinstance(journal, str) or not isinstance(records, list):
            raise ValueError('Invalid journal or article list in checkpoint.')
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('Each pending article must be an object.')
            if any(key in record and not isinstance(record[key], str)
                   for key in ('link', 'id', 'title', 'summary', 'published')):
                raise ValueError('Pending article text fields must be strings.')
            if not isinstance(record.get('authors', []), list) or any(
                    not isinstance(author, str) for author in record.get('authors', [])):
                raise ValueError('Pending authors must be a list of strings.')
            if not identity(record):
                raise ValueError('Pending article lacks a usable identity.')
    return queue


def audit_decisions(text):
    """Parse only completed decisions; pending/source-error lines are excluded."""
    decisions = {}
    journal = None
    for line in text.splitlines():
        header = re.fullmatch(r'--- (.+) ---', line)
        if header:
            journal = header.group(1)
        match = re.fullmatch(r'\s+(?:🤖)?[✅❌] (.+) \((https?://.*)\)', line)
        if journal and match:
            title, link = match.groups()
            decisions.setdefault(journal, set()).add('link:' + link.strip())
    return decisions


def reconcile(current, historical, audits):
    """Keep current work and add only historical identities lacking a decision.

    Journal scope is intentional: different journals have different thresholds.
    Cross-publication title matches remain eligible, rather than assuming two
    articles or versions with the same title have the same classification.
    """
    validate_queue(current)
    validate_queue(historical)
    completed = {}
    for audit in audits:
        for journal, keys in audit_decisions(audit).items():
            completed.setdefault(journal, set()).update(keys)
    merged = {}
    counts = {'historical_records': sum(map(len, historical.values())),
              'already_decided': 0, 'already_pending': 0, 'added': 0,
              'current_duplicates_removed': 0, 'added_by_journal': {}}
    seen = {}
    for journal, records in current.items():
        merged[journal] = []
        seen[journal] = set()
        for record in records:
            key = identity(record)
            if key in seen[journal]:
                counts['current_duplicates_removed'] += 1
                continue
            merged[journal].append(copy.deepcopy(record))
            seen[journal].add(key)
    for journal, records in historical.items():
        seen.setdefault(journal, set())
        for record in records:
            key = identity(record)
            if key in seen[journal]:
                counts['already_pending'] += 1
            elif key in completed.get(journal, set()):
                counts['already_decided'] += 1
            else:
                merged.setdefault(journal, []).append(copy.deepcopy(record))
                seen[journal].add(key)
                counts['added'] += 1
                counts['added_by_journal'][journal] = counts['added_by_journal'].get(journal, 0) + 1
    return {j: rs for j, rs in merged.items() if rs}, counts


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as file:
        temp = file.name
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write('\n')
    try:
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True).stdout


def read_snapshot(ref):
    paths = git('ls-tree', '-z', '--name-only', ref).decode().split('\0')
    selected = [p for p in paths if p in CHECKPOINT_FILES or re.fullmatch(r'filtered_feed_[^/]+\.xml', p)]
    snapshot = {p: git('show', f'{ref}:{p}') for p in selected}
    if QUEUE_FILE not in snapshot:
        raise ValueError(f'{ref} has no pending queue; refusing to assume an empty backlog.')
    validate_queue(json.loads(snapshot[QUEUE_FILE]))
    if MARKER_FILE in snapshot:
        validate_markers(json.loads(snapshot[MARKER_FILE]))
    return snapshot


def validate_markers(markers):
    if not isinstance(markers, list) or any(not isinstance(ref, str) or not re.fullmatch(r'[0-9a-f]{40}', ref) for ref in markers):
        raise ValueError('Recovery markers must be a list of full commit SHAs.')
    return markers


def load_json(path, default):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def restore(cache_restored='', historical_ref=''):
    if historical_ref and not re.fullmatch(r'[0-9a-fA-F]{40}', historical_ref):
        raise ValueError('Historical recovery requires a full 40-character commit SHA.')
    historical_ref = historical_ref.lower()
    if cache_restored:
        if not Path(QUEUE_FILE).exists():
            raise ValueError('Restored cache has no pending queue.')
        validate_queue(load_json(QUEUE_FILE, {}))
        validate_markers(load_json(MARKER_FILE, []))

    # Fetch only data. A failed fetch must not silently start from an empty queue.
    git('fetch', '--no-tags', '--depth=1', 'origin', 'gh-pages')
    published_ref = git('rev-parse', 'FETCH_HEAD').decode().strip()
    snapshot = read_snapshot(published_ref)
    if not cache_restored:
        # Remove stale partial-state files absent from this complete snapshot.
        for name in CHECKPOINT_FILES:
            if name not in snapshot and Path(name).exists():
                Path(name).unlink()
        for name, content in snapshot.items():
            Path(name).write_bytes(content)
        print(f'Restored published checkpoint from {published_ref}.')
    current = validate_queue(load_json(QUEUE_FILE, {}))
    markers = validate_markers(load_json(MARKER_FILE, []))
    published_markers = validate_markers(json.loads(snapshot.get(MARKER_FILE, b'[]')))
    # Both queues are authoritative pending work. Without trustworthy
    # provenance, an older audit must not suppress a newer explicit retry.
    # Conservative reclassification is safer than silently deleting an item.
    merged, merge_report = reconcile(current, json.loads(snapshot[QUEUE_FILE]), [])
    markers = sorted(set(markers) | set(published_markers))
    atomic_json(QUEUE_FILE, merged)
    atomic_json(MARKER_FILE, markers)
    current = merged
    if merge_report['added']:
        print(f"Preserved {merge_report['added']} pending records from the published checkpoint.")
    if not historical_ref:
        return

    if historical_ref in markers:
        print(f'Historical checkpoint {historical_ref} was already imported; keeping current progress.')
        return
    git('fetch', '--no-tags', '--depth=1', 'origin', historical_ref)
    historical = validate_queue(json.loads(git('show', f'{historical_ref}:{QUEUE_FILE}')))
    audits = [snapshot.get('filtered_titles.txt', b'').decode('utf-8')]
    for name in ('filtered_titles.txt', 'partial_email_content.txt'):
        if Path(name).exists():
            audits.append(Path(name).read_text(encoding='utf-8'))
    merged, report = reconcile(current, historical, audits)
    report.update(historical_ref=historical_ref, published_ref=published_ref,
                  title_policy='Only exact identity matches in the same journal are skipped.')
    # Queue first, marker last: a crash can replay the idempotent union, not lose work.
    atomic_json(QUEUE_FILE, merged)
    atomic_json('backlog_recovery_report.json', report)
    atomic_json(MARKER_FILE, markers + [historical_ref])
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-restored', default='')
    parser.add_argument('--historical-ref', default='')
    args = parser.parse_args()
    restore(args.cache_restored, args.historical_ref)


if __name__ == '__main__':
    main()
