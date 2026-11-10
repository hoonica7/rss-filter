import copy
import json
from pathlib import Path
import subprocess

import pytest

import restore_checkpoint as recovery


REF = 'a' * 40
PUBLISHED = 'b' * 40


def paper(number, **updates):
    return {'title': f'Paper {number}', 'link': f'https://example.invalid/{number}',
            'summary': 'Stored abstract', 'authors': ['Synthetic Author'], **updates}


def audit(journal, record, prefix='🤖❌'):
    return f"--- {journal} ---\n\n  {prefix} {record['title']} ({record['link']})\n"


def test_reconcile_only_completed_decisions_and_preserve_metadata():
    current = {'A': [paper(1)]}
    historical = {'A': [paper(1), paper(2), paper(3), paper(4)]}
    originals = copy.deepcopy((current, historical))
    merged, report = recovery.reconcile(current, historical, [
        audit('A', paper(2), '✅') + audit('A', paper(3), '⏸'),
    ])
    assert merged == {'A': [paper(1), paper(3), paper(4)]}
    assert report['added'] == 2
    assert report['already_decided'] == 1
    assert report['already_pending'] == 1
    assert (current, historical) == originals
    again, report = recovery.reconcile(merged, historical, [audit('A', paper(2))])
    assert again == merged
    assert report['added'] == 0


def test_do_not_drop_cross_journal_or_title_only_matches():
    version = paper(2, title='Same title')
    original = paper(1, title='Same title')
    merged, _ = recovery.reconcile({}, {'A': [original, version]}, [audit('B', original)])
    assert merged == {'A': [original, version]}
    merged, _ = recovery.reconcile({}, {'A': [version]}, [audit('A', original)])
    assert merged == {'A': [version]}


def test_current_pending_is_authoritative_and_duplicates_removed():
    record = paper(1)
    merged, report = recovery.reconcile({'A': [record, record]}, {'A': [record]}, [audit('A', record)])
    assert merged == {'A': [record]}
    assert report['already_pending'] == 1
    assert report['current_duplicates_removed'] == 1


def test_decision_parser_handles_parentheses_and_rejects_pending():
    record = paper(1, title='A title (with parentheses)', link='https://example.invalid/paper(a)')
    result = recovery.audit_decisions(audit('A', record, '🤖✅') + audit('B', record, '⏸'))
    assert result == {'A': {'link:https://example.invalid/paper(a)'}}
    assert recovery.audit_decisions('  ✅ Unscoped (https://example.invalid/1)') == {}


@pytest.mark.parametrize('queue', [[], {'A': {}}, {'A': [None]}, {'A': [{}]},
                                   {'A': [paper(1, link=1)]}, {'A': [paper(1, authors='Author')]}])
def test_invalid_queue_rejected(queue):
    with pytest.raises(ValueError):
        recovery.validate_queue(queue)


def fake_git(monkeypatch, snapshot, historical=None):
    calls = []
    def run(*args):
        calls.append(args)
        if args[:1] == ('fetch',):
            return b''
        if args == ('rev-parse', 'FETCH_HEAD'):
            return PUBLISHED.encode()
        if args[:1] == ('ls-tree',):
            return '\0'.join(snapshot).encode()
        if args[:1] == ('show',):
            ref, name = args[1].split(':', 1)
            if ref == REF:
                assert name == recovery.QUEUE_FILE
                return json.dumps(historical).encode()
            assert ref == PUBLISHED
            return snapshot[name]
        raise AssertionError(args)
    monkeypatch.setattr(recovery, 'git', run)
    return calls


def test_prefix_cache_restore_preserves_working_state_and_reconciles_published(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recovery.atomic_json(recovery.QUEUE_FILE, {'A': [paper(1)]})
    fake_git(monkeypatch, {recovery.QUEUE_FILE: b'{}'})
    Path('last_failed_journal.txt').write_text('A')
    recovery.restore(cache_restored='rss-checkpoint-previous-run')
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1)]}
    assert Path('last_failed_journal.txt').read_text() == 'A'


def test_cache_miss_restores_only_checkpoint_data_and_removes_stale_partial(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('partial_email_content.txt').write_text('stale partial audit')
    Path('Filter_RSS.py').write_text('current code')
    snapshot = {recovery.QUEUE_FILE: json.dumps({'A': [paper(1)]}).encode(),
                'last_failed_journal.txt': b'SUCCESS',
                'filtered_feed_A.xml': b'<rss/>', 'Filter_RSS.py': b'untrusted old code'}
    fake_git(monkeypatch, snapshot)
    recovery.restore()
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1)]}
    assert Path('filtered_feed_A.xml').read_text() == '<rss/>'
    assert Path('Filter_RSS.py').read_text() == 'current code'
    assert not Path('partial_email_content.txt').exists()


@pytest.mark.parametrize('snapshot', [{}, {recovery.QUEUE_FILE: b'null'},
                                     {recovery.QUEUE_FILE: b'{}', recovery.MARKER_FILE: b'{}'}])
def test_bad_snapshot_does_not_destroy_working_state(tmp_path, monkeypatch, snapshot):
    monkeypatch.chdir(tmp_path)
    Path(recovery.QUEUE_FILE).write_text('{"Existing": []}')
    fake_git(monkeypatch, snapshot)
    with pytest.raises(ValueError):
        recovery.restore()
    assert Path(recovery.QUEUE_FILE).read_text() == '{"Existing": []}'


def test_historical_import_uses_published_and_partial_audits_and_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recovery.atomic_json(recovery.QUEUE_FILE, {'A': [paper(1)]})
    Path('partial_email_content.txt').write_text(audit('A', paper(3)))
    snapshot = {recovery.QUEUE_FILE: b'{}', 'filtered_titles.txt': audit('A', paper(2)).encode()}
    calls = fake_git(monkeypatch, snapshot, {'A': [paper(i) for i in range(1, 5)]})
    recovery.restore(cache_restored='prior', historical_ref=REF)
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1), paper(4)]}
    assert json.loads(Path(recovery.MARKER_FILE).read_text()) == [REF]
    report = json.loads(Path('backlog_recovery_report.json').read_text())
    assert report['added'] == 1
    assert report['already_decided'] == 2
    # Even after later audits no longer mention the source, it must not requeue it.
    recovery.atomic_json(recovery.QUEUE_FILE, {})
    Path('partial_email_content.txt').unlink()
    recovery.restore(cache_restored='prior', historical_ref=REF)
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {}
    assert calls.count(('fetch', '--no-tags', '--depth=1', 'origin', REF)) == 1


def test_cache_miss_restores_marker_before_recovery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = {recovery.QUEUE_FILE: b'{}', recovery.MARKER_FILE: json.dumps([REF]).encode()}
    calls = fake_git(monkeypatch, snapshot)
    recovery.restore(historical_ref=REF)
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {}
    assert ('fetch', '--no-tags', '--depth=1', 'origin', REF) not in calls


def test_bad_ref_is_rejected_before_git_or_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(recovery, 'git', lambda *args: pytest.fail('Invalid SHA must not reach git.'))
    with pytest.raises(ValueError):
        recovery.restore(historical_ref='main; touch bad')
    assert list(tmp_path.iterdir()) == []


def test_git_fetch_failure_stops_instead_of_creating_empty_queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def fail(*args):
        raise subprocess.CalledProcessError(128, ['git', *args])
    monkeypatch.setattr(recovery, 'git', fail)
    with pytest.raises(subprocess.CalledProcessError):
        recovery.restore()
    assert not Path(recovery.QUEUE_FILE).exists()


def test_import_marker_is_written_only_after_queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recovery.atomic_json(recovery.QUEUE_FILE, {})
    fake_git(monkeypatch, {recovery.QUEUE_FILE: b'{}'}, {'A': [paper(1)]})
    real_atomic = recovery.atomic_json
    def fail_marker(path, value):
        if path == recovery.MARKER_FILE and value:
            raise OSError('synthetic marker failure')
        return real_atomic(path, value)
    monkeypatch.setattr(recovery, 'atomic_json', fail_marker)
    with pytest.raises(OSError):
        recovery.restore(cache_restored='prior', historical_ref=REF)
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1)]}
    assert json.loads(Path(recovery.MARKER_FILE).read_text()) == []


def test_workflow_uses_matched_key_and_keeps_marker_in_all_state_stores():
    workflow = (Path(__file__).parents[1] / '.github/workflows/Main.yml').read_text()
    assert 'outputs.cache-matched-key' in workflow
    assert 'outputs.cache-hit' not in workflow
    assert workflow.count('            recovered_backlog_refs.json\n') == 3
    assert 'cancel-in-progress: false' in workflow
    assert 'HISTORICAL_PENDING_REF: ${{ inputs.historical_pending_ref }}' in workflow


def test_stale_cache_cannot_erase_published_queue_or_import_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recovery.atomic_json(recovery.QUEUE_FILE, {'A': [paper(1)]})
    snapshot = {recovery.QUEUE_FILE: json.dumps({'A': [paper(2)]}).encode(),
                recovery.MARKER_FILE: json.dumps([REF]).encode()}
    calls = fake_git(monkeypatch, snapshot, {'A': [paper(3)]})
    recovery.restore(cache_restored='old-cache', historical_ref=REF)
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1), paper(2)]}
    assert json.loads(Path(recovery.MARKER_FILE).read_text()) == [REF]
    assert ('fetch', '--no-tags', '--depth=1', 'origin', REF) not in calls


def test_stale_cached_audit_cannot_suppress_published_pending(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recovery.atomic_json(recovery.QUEUE_FILE, {})
    Path('filtered_titles.txt').write_text(audit('A', paper(1)))
    fake_git(monkeypatch, {recovery.QUEUE_FILE: json.dumps({'A': [paper(1), paper(2)]}).encode()})
    recovery.restore(cache_restored='new-cache')
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1), paper(2)]}


def test_historical_import_rejects_cache_missing_queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(recovery, 'git', lambda *args: pytest.fail('Invalid cache must stop before git.'))
    with pytest.raises(ValueError, match='no pending queue'):
        recovery.restore(cache_restored='partial-cache', historical_ref=REF)


def test_resumed_partial_persistence_keeps_recovered_earlier_journal(tmp_path, monkeypatch):
    import run_filter_resilient as runner
    monkeypatch.chdir(tmp_path)
    pending = {'A': [paper(1)], 'B': [paper(2)], 'C': [paper(3)]}
    runner.persist_partial('audit', [], pending, {}, 'B', 1, processed_journals=['B'])
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1)], 'C': [paper(3)]}
    runner.persist_partial('audit', [], pending, {}, 'C', 2, processed_journals=['B', 'C'])
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1)]}


@pytest.mark.parametrize('failure', ['exception', 'fetch'])
def test_resume_then_failure_keeps_imported_earlier_journal(tmp_path, monkeypatch, failure):
    import run_filter_resilient as runner
    import requests
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, 'install_resilient_feed_fetch', lambda: None)
    monkeypatch.setattr(runner.rss, 'JOURNAL_URLS', {'A': 'a', 'B': 'b', 'C': 'c'})
    Path('last_failed_journal.txt').write_text('B')
    recovery.atomic_json(recovery.QUEUE_FILE, {'A': [paper(1)], 'B': [paper(2)], 'C': [paper(3)]})
    def filter_journal(journal, url, records):
        if journal == 'B':
            return (b'<rss/>', [], [], [], [paper(2)], [], {})
        assert journal == 'C'
        if failure == 'fetch':
            raise requests.exceptions.RequestException('synthetic fetch failure')
        raise RuntimeError('synthetic interrupted classification')
    monkeypatch.setattr(runner.rss, 'filter_rss_for_journal', filter_journal)
    for name in ('create_index_html', 'create_results_html_file', 'create_briefing_html', 'create_slideshow_html'):
        monkeypatch.setattr(runner.rss, name, lambda *args: None)
    if failure == 'exception':
        with pytest.raises(RuntimeError, match='synthetic interrupted'):
            runner.run()
        assert Path('last_failed_journal.txt').read_text() == 'C'
    else:
        runner.run()
        assert Path('last_failed_journal.txt').read_text() == 'SUCCESS'
    assert json.loads(Path(recovery.QUEUE_FILE).read_text()) == {'A': [paper(1)], 'C': [paper(3)]}
