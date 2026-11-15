"""Stable reader-facing publication snapshots, separate from classifier/audit state.

Keep accepted items for 30 days from first observation so short-lived source feeds
and recovered backlog are not missed between reader syncs. Pending recoveries
receive a fixed recovery identity/date exactly once; source dates stay explicit.
This cannot determine whether an individual reader already downloaded an item.
"""
import copy
import hashlib
import html
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import re

from lxml import etree as ET

LEDGER = 'rss_publication_history.json'
RETENTION_DAYS = 30
ATOM = 'http://www.w3.org/2005/Atom'
RSS1 = 'http://purl.org/rss/1.0/'
RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
DC = 'http://purl.org/dc/elements/1.1/'
CONTENT = 'http://purl.org/rss/1.0/modules/content/'


def parse(data):
    return ET.fromstring(data, ET.XMLParser(resolve_entities=False, no_network=True))


def empty_history():
    return {'version': 1, 'journals': {}}


def validate_history(history):
    if history.get('version') != 1 or not isinstance(history.get('journals'), dict):
        raise ValueError('Invalid RSS publication history; refusing to reset reader identities')
    for entries in history['journals'].values():
        if not isinstance(entries, dict):
            raise ValueError('Invalid RSS publication journal')
        for record in entries.values():
            datetime.fromisoformat(record['first_seen'])
            parse(record['xml'].encode())
            if record['kind'] not in ('rss2', 'rss1', 'atom'):
                raise ValueError('Unknown publication format')
    return history


def merge_histories(local, published):
    """Published identity wins on a conflict; retain unpublished partial work too."""
    merged = copy.deepcopy(validate_history(local))
    for journal, entries in validate_history(published)['journals'].items():
        merged['journals'].setdefault(journal, {}).update(copy.deepcopy(entries))
    return merged


def identity(value):
    value = (value or '').strip()
    match = re.search(r'(?:arxiv\.org/(?:abs|pdf)/|oai:arXiv\.org:)(\d{4}\.\d{4,5})(?:v\d+)?', value, re.I)
    if match:
        # Existing generated feeds expose unversioned links. Preserve their
        # identity; a version change alone must not silently mint a new item.
        return 'arxiv:' + match[1]
    match = re.search(r'(?:doi\.org/|^doi:)(10\.\d{4,9}/.+)', value, re.I)
    if match:
        return 'doi:' + match[1].lower()
    return value


def layout(root):
    if root.tag == 'rss' and root.find('channel') is not None:
        return 'rss2', root.find('channel'), 'item', ''
    if root.tag == '{%s}feed' % ATOM:
        return 'atom', root, '{%s}entry' % ATOM, '{%s}' % ATOM
    if root.tag == '{%s}RDF' % RDF:
        return 'rss1', root, '{%s}item' % RSS1, '{%s}' % RSS1
    raise ValueError('Unsupported feed layout; refusing to overwrite existing RSS')


def key_for(item, kind, ns):
    if kind == 'atom':
        links = item.findall(ns + 'link')
        value = next((e.get('href') for e in links if e.get('rel', 'alternate') == 'alternate'), '')
    else:
        value = item.findtext(ns + 'link')
    return identity(value or item.findtext(ns + ('id' if kind == 'atom' else 'guid'))
                    or item.get('{%s}about' % RDF))


def put(item, tag, text, **attrs):
    el = item.find(tag)
    if el is None:
        el = ET.SubElement(item, tag)
    el.text = text
    el.attrib.update(attrs)


def recover(item, journal, key, kind, ns, now, original_date=None):
    source_date = item.findtext(ns + ('published' if kind == 'atom' else 'pubDate'))
    if not source_date:
        source_date = item.findtext('{%s}date' % DC) or item.findtext(ns + 'updated') or original_date or 'not supplied by source'
    stamp = now.isoformat()
    guid = 'urn:rss-filter:recovery:' + hashlib.sha256((journal + '\n' + key + '\n' + stamp).encode()).hexdigest()
    title_tags = [ns + 'title'] + (['{%s}title' % DC] if kind == 'rss1' else [])
    for tag in title_tags:
        for el in item.findall(tag):
            el.text = '[RSS recovery] ' + (el.text or '')
    notice = ('<p><b>RSS recovery:</b> ' + html.escape(stamp) + '. '
              'Delayed/recovered feed delivery, not a new paper version. '
              '<b>Original source publication/announcement date:</b> ' + html.escape(source_date) + '.</p>')
    tags = ([ns + 'content', ns + 'summary'] if kind == 'atom'
            else [ns + 'description', '{%s}encoded' % CONTENT])
    for tag in tags:
        el = item.find(tag)
        if el is None:
            continue
        if kind == 'atom' and el.get('type') == 'xhtml':
            div = el.find('{http://www.w3.org/1999/xhtml}div')
            if div is None:
                raise ValueError('Invalid Atom XHTML content')
            paragraph = ET.Element('{http://www.w3.org/1999/xhtml}p')
            paragraph.text = html.unescape(re.sub(r'<[^>]+>', '', notice))
            div.insert(0, paragraph)
            continue
        prior = el.text or ''
        if kind == 'atom' and el.get('type', 'text') == 'text':
            prior = html.escape(prior)
        el.text = notice + prior
        if kind == 'atom':
            el.set('type', 'html')
    if kind == 'atom':
        if item.find(ns + 'summary') is None:
            put(item, ns + 'summary', notice, type='html')
        put(item, ns + 'id', guid)
        put(item, ns + 'published', stamp)
        put(item, ns + 'updated', stamp)
    elif kind == 'rss1':
        # RDF about is the item identity, while link remains the original article.
        item.set('{%s}about' % RDF, guid)
        put(item, '{%s}date' % DC, stamp)
    else:
        put(item, 'guid', guid, isPermaLink='false')
        put(item, 'pubDate', format_datetime(now))
    return item


def atomic_write(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.publication.tmp')
    temp.write_bytes(data)
    temp.replace(path)


def stabilize_feed(xml, journal, pending_records=(), output_path=None, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Publication clock must be timezone aware')
    root = parse(xml)
    kind, parent, tag, ns = layout(root)
    history_path = Path(LEDGER)
    history = validate_history(json.loads(history_path.read_text())) if history_path.exists() else empty_history()
    records = history['journals'].setdefault(journal, {})
    # Bootstrap from the last output (including the confirmed manual reissue).
    # Never treat a cache miss as evidence that a reader never received a paper.
    if output_path and Path(output_path).exists():
        old = parse(Path(output_path).read_bytes())
        old_kind, old_parent, old_tag, old_ns = layout(old)
        for item in old_parent.findall(old_tag):
            key = key_for(item, old_kind, old_ns)
            if key and key not in records:
                records[key] = {'first_seen': now.isoformat(), 'kind': old_kind,
                                'xml': ET.tostring(item, encoding='unicode')}
    pending = {identity(r.get('link') or r.get('id')): r for r in pending_records}
    emitted = set()
    output = []
    for item in parent.findall(tag):
        key = key_for(item, kind, ns)
        if not key:
            output.append(item)
            continue
        if key in emitted:
            continue
        emitted.add(key)
        if key not in records:
            if key in pending:
                recover(item, journal, key, kind, ns, now, pending[key].get('published') or pending[key].get('updated'))
            records[key] = {'first_seen': now.isoformat(), 'kind': kind,
                            'xml': ET.tostring(item, encoding='unicode')}
        if records[key]['kind'] != kind:
            raise ValueError('Feed format changed; publication migration required for ' + journal)
        output.append(parse(records[key]['xml'].encode()))
    for key, record in records.items():
        if key not in emitted and now < datetime.fromisoformat(record['first_seen']) + timedelta(days=RETENTION_DAYS):
            if record['kind'] != kind:
                raise ValueError('Feed format changed; retained publication format differs')
            output.append(parse(record['xml'].encode()))
    for item in parent.findall(tag):
        parent.remove(item)
    for item in output:
        parent.append(item)
    if kind == 'rss1':
        for seq in root.findall('.//{%s}Seq' % RDF):
            for child in list(seq):
                seq.remove(child)
            for item in output:
                ET.SubElement(seq, '{%s}li' % RDF, {'{%s}resource' % RDF: item.get('{%s}about' % RDF) or item.findtext(ns + 'link')})
    result = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    # Save identity first: a retry after interrupted output reuses identical bytes.
    atomic_write(history_path, (json.dumps(history, ensure_ascii=False, indent=2) + '\n').encode())
    return result


def bootstrap_history(history, snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    history = copy.deepcopy(validate_history(history))
    for name, data in snapshot.items():
        match = re.fullmatch(r'filtered_feed_(.+)\.xml', name)
        if not match:
            continue
        root = parse(data)
        kind, parent, tag, ns = layout(root)
        records = history['journals'].setdefault(match[1], {})
        for item in parent.findall(tag):
            key = key_for(item, kind, ns)
            if key and key not in records:
                records[key] = {'first_seen': now.isoformat(), 'kind': kind,
                                'xml': ET.tostring(item, encoding='unicode')}
    return history
