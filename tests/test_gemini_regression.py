import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest
from google import genai
from google.genai import types

import Filter_RSS as rss
import enhance_slides as slides
import gemini_runtime as runtime
import run_filter_resilient as runner


class APIError(Exception):
    def __init__(self, code, message, headers=None):
        super().__init__(message)
        self.code = code
        self.response = SimpleNamespace(headers=headers or {})


def entry(i):
    return {'title': f'Electronic structure sample {i}', 'summary': 'Synthetic test abstract.',
            'link': f'https://example.invalid/paper/{i}'}


def response(items):
    return SimpleNamespace(text=json.dumps(items))


def decision(item, score=8):
    return {'title': item['title'], 'score': score, 'reason': 'Synthetic reason.'}


def setup_clients(monkeypatch, module, side_effects, models=('preview', 'stable')):
    clients = []
    calls = []
    for idx, effect in enumerate(side_effects):
        call = Mock(side_effect=effect)
        calls.append(call)
        clients.append((f'KEY{idx+1}', SimpleNamespace(models=SimpleNamespace(generate_content=call))))
    monkeypatch.setattr(module, 'gemini_clients', clients)
    monkeypatch.setattr(module, 'MODEL_CANDIDATES', list(models))
    monkeypatch.setattr(module, 'current_api_index', 0)
    monkeypatch.setattr(module, 'current_model_index', 0)
    monkeypatch.setattr(module, 'gemini_failures', runtime.FailureTracker(clock=lambda: 0))
    return calls


def test_sdk_v3_default_route_and_explicit_beta(monkeypatch):
    # Real SDK serialization, synthetic response; not an API integration test.
    requests = []
    def send(client, request, **kwargs):
        requests.append(request)
        return httpx2.Response(200, request=request, json={
            'candidates': [{'content': {'parts': [{'text': '[]'}], 'role': 'model'}}]})
    monkeypatch.setattr(httpx2.Client, 'send', send)
    config = types.GenerateContentConfig(response_mime_type='application/json',
                                         response_schema={'type': 'ARRAY', 'items': {'type': 'STRING'}})
    with genai.Client(api_key='synthetic-test-key') as old:
        assert old._api_client._http_options.api_version == 'v1'
        old.models.generate_content(model='synthetic-model', contents='Synthetic', config=config)
    assert requests[0].url.path.startswith('/v1/')
    with runtime.create_client('synthetic-test-key') as fixed:
        assert fixed._api_client._http_options.retry_options.attempts == 1
        result = fixed.models.generate_content(model='synthetic-model', contents='Synthetic', config=config)
    assert result.text == '[]'
    assert len(requests) == 2
    assert requests[1].url.path.startswith('/v1beta/')
    assert json.loads(requests[1].content)['generationConfig']['responseMimeType'] == 'application/json'


@pytest.mark.parametrize('value,expected', [(None, 'v1beta'), ('', 'v1beta'), ('  ', 'v1beta'), ('v1alpha', 'v1alpha')])
def test_version_default_and_override(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv('GEMINI_API_VERSION', raising=False)
    else:
        monkeypatch.setenv('GEMINI_API_VERSION', value)
    fake = Mock()
    monkeypatch.setattr(runtime.genai, 'Client', fake)
    runtime.create_client('synthetic')
    assert fake.call_args.kwargs['http_options'].api_version == expected


@pytest.mark.parametrize('module', [rss, slides])
def test_both_client_initializers_use_explicit_version(monkeypatch, module):
    monkeypatch.setenv('GOOGLE_API_KEY1', 'synthetic')
    fake = Mock()
    with monkeypatch.context() as scope:
        scope.setattr(runtime.genai, 'Client', fake)
        importlib.reload(module)
        assert fake.call_args.kwargs['http_options'].api_version == 'v1beta'
        assert len(module.gemini_clients) == 1
    monkeypatch.delenv('GOOGLE_API_KEY1')
    importlib.reload(module)


def test_429_respects_retry_after_and_then_recovers():
    now = [100.0]
    state = runtime.FailureTracker(clock=lambda: now[0])
    error = APIError(429, "{'retryDelay': '90s'}", {'Retry-After': '120'})
    assert state.record('KEY1', 'model', error) == 'quota'
    assert not state.can_try('KEY1', 'model')
    now[0] = 219.9
    assert not state.can_try('KEY1', 'model')
    now[0] = 220
    assert state.can_try('KEY1', 'model')


@pytest.mark.parametrize('error', [APIError(404, 'not found'), APIError(403, 'permission'),
                                    APIError(429, 'requestsPerDay quota')])
def test_permanent_failures_not_retried_during_run(error):
    now = [0]
    state = runtime.FailureTracker(clock=lambda: now[0])
    state.record('KEY1', 'model', error)
    now[0] = 86400
    assert not state.can_try('KEY1', 'model')
    assert state.can_try('KEY2', 'model')


def test_empty_and_missing_clients(monkeypatch):
    monkeypatch.setattr(rss, 'gemini_clients', [])
    assert rss.classify_entries_with_gemini('Nature', []) == ([], [], [], {})
    items = [entry(1)]
    assert rss.classify_entries_with_gemini('Nature', items) == ([], [], items, {})


def test_success_pass_and_remove(monkeypatch):
    items = [entry(1), entry(2)]
    calls = setup_clients(monkeypatch, rss, [[response([decision(items[0]), decision(items[1], 1)])]])
    passed, removed, pending, metadata = rss.classify_entries_with_gemini('Nature', items)
    assert (passed, removed, pending) == ([items[0]], [items[1]], [])
    assert len(metadata) == 2
    assert calls[0].call_count == 1


def test_429_calls_bounded_across_batches_and_journals(monkeypatch, capsys):
    monkeypatch.setenv('GEMINI_BATCH_SIZE', '1')
    calls = setup_clients(monkeypatch, rss, [APIError(429, 'SECRET_TOKEN quota'), APIError(429, 'quota')])
    items = [entry(i) for i in range(6)]
    assert rss.classify_entries_with_gemini('Nature', items)[2] == items
    assert rss.classify_entries_with_gemini('Science', items)[2] == items
    assert [call.call_count for call in calls] == [2, 2]
    assert 'SECRET_TOKEN' not in capsys.readouterr().err


def test_bad_model_falls_back_and_does_not_repeat(monkeypatch):
    monkeypatch.setenv('GEMINI_BATCH_SIZE', '1')
    items = [entry(1), entry(2)]
    calls = setup_clients(monkeypatch, rss, [[APIError(404, 'not found'),
                                            response([decision(items[0])]), response([decision(items[1])])]])
    assert rss.classify_entries_with_gemini('Nature', items)[0] == items
    assert [c.kwargs['model'] for c in calls[0].call_args_list] == ['preview', 'stable', 'stable']


def test_schema_fallback_only_for_schema_errors(monkeypatch):
    item = entry(1)
    calls = setup_clients(monkeypatch, rss, [[APIError(400, 'response_schema is not supported'),
                                            response([decision(item)])]], models=('stable',))
    assert rss.classify_entries_with_gemini('Nature', [item])[0] == [item]
    assert calls[0].call_count == 2
    assert calls[0].call_args.kwargs['config'].response_schema is None
    calls = setup_clients(monkeypatch, rss, [ValueError('response_mime_type is not supported')], models=('stable',))
    assert rss.classify_entries_with_gemini('Nature', [item])[2] == [item]
    assert calls[0].call_count == 1


def test_truncated_response_rolls_back_before_fallback(monkeypatch):
    items = [entry(i) for i in range(4)]
    setup_clients(monkeypatch, rss, [[response([decision(items[0])]), response([decision(e, 1) for e in items])]])
    passed, removed, pending, metadata = rss.classify_entries_with_gemini('Nature', items)
    assert (passed, removed, pending) == ([], items, [])
    assert all(m['score'] == 1 for m in metadata.values())


def test_missing_response_item_rechecked(monkeypatch):
    items = [entry(i) for i in range(3)]
    calls = setup_clients(monkeypatch, rss, [[response([decision(e) for e in items[:2]]), response([decision(items[2])])]])
    assert rss.classify_entries_with_gemini('Nature', items)[:3] == (items, [], [])
    assert calls[0].call_count == 2


def test_slide_cooldown_and_fallback(monkeypatch):
    calls = setup_clients(monkeypatch, slides, [APIError(429, 'quota')])
    assert slides.generate_json_with_gemini('Synthetic') is None
    assert slides.generate_json_with_gemini('Synthetic') is None
    assert calls[0].call_count == 2
    calls = setup_clients(monkeypatch, slides, [[APIError(404, 'not found'), response({'ok': True})]])
    assert slides.generate_json_with_gemini('Synthetic') == {'ok': True}
    assert calls[0].call_count == 2


def test_health_reports_partial_and_fails_total(tmp_path, monkeypatch, capsys):
    summary = tmp_path / 'summary.txt'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
    runtime.check_classification_health(2, 1)
    assert '::warning::' in capsys.readouterr().err
    with pytest.raises(RuntimeError, match='pending queue preserved'):
        runtime.check_classification_health(0, 3)
    assert '::error::' in capsys.readouterr().err
    assert '0 classified, 3 pending retry' in summary.read_text()
    runtime.check_classification_health(0, 0)


@pytest.mark.parametrize('classified', [False, True])
def test_runner_checkpoints_before_failure_or_success(tmp_path, monkeypatch, classified):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, 'install_resilient_feed_fetch', lambda: None)
    monkeypatch.setattr(rss, 'JOURNAL_URLS', {'Nature': 'https://example.invalid/rss'})
    item = entry(1)
    pending = [] if classified else [item]
    passed = [item] if classified else []
    monkeypatch.setattr(rss, 'filter_rss_for_journal', lambda *args: (b'<rss/>', [], passed, [], [], pending, {}))
    monkeypatch.setattr(rss, 'paper_record', lambda *args: {})
    for name in ['create_index_html', 'create_results_html_file', 'create_briefing_html', 'create_slideshow_html']:
        monkeypatch.setattr(rss, name, Mock())
    if classified:
        runner.run()
    else:
        with pytest.raises(RuntimeError, match='No Gemini decisions completed'):
            runner.run()
    queue = json.loads((tmp_path / 'pending_classification_queue.json').read_text())
    assert len(queue.get('Nature', [])) == (0 if classified else 1)
    assert (tmp_path / 'filtered_feed_Nature.xml').read_bytes() == b'<rss/>'
    assert 'Gemini classification:' in (tmp_path / 'filtered_titles.txt').read_text()
    assert (tmp_path / 'last_failed_journal.txt').read_text() == 'SUCCESS'


def test_workflow_pin_and_private_patch():
    root = Path(__file__).resolve().parents[1]
    assert 'google-genai==3.0.0' in (root / 'requirements.txt').read_text()
    workflow = (root / '.github/workflows/Main.yml').read_text()
    assert 'pip install --upgrade google-genai' not in workflow
    assert 'GEMINI_API_VERSION: "v1beta"' in workflow
    import apply_private_config_patch as private_patch
    source = (root / 'Filter_RSS.py').read_text()
    assert private_patch.patch_text(source) == source


def test_full_pipeline_preserves_and_recovers_pending(tmp_path, monkeypatch):
    """Real feed parsing/classification/checkpoints/output, mocked external I/O."""
    import requests
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, 'install_resilient_feed_fetch', lambda: None)
    monkeypatch.setattr(rss, 'JOURNAL_URLS', {'Nature': 'https://example.invalid/rss'})
    monkeypatch.setattr(rss, 'get_article_image', lambda *args: '')
    monkeypatch.setattr(rss, 'entry_publication_info', lambda *args: {})
    feed = requests.Response()
    feed.status_code = 200
    feed._content = b'''<rss version="2.0"><channel><title>Test feed</title>
    <item><title>Electronic structure sample 1</title><link>https://example.invalid/paper/1</link>
    <description>Synthetic test abstract.</description></item></channel></rss>'''
    monkeypatch.setattr(rss.requests, 'get', lambda *args, **kwargs: feed)
    calls = setup_clients(monkeypatch, rss, [APIError(429, 'quota')], models=('stable',))
    with pytest.raises(RuntimeError, match='No Gemini decisions completed'):
        runner.run()
    queue = json.loads(Path('pending_classification_queue.json').read_text())
    assert len(queue['Nature']) == 1
    assert calls[0].call_count == 1
    assert '0 classified, 1 pending retry' in Path('filtered_titles.txt').read_text()
    # Next run: source no longer carries the paper; recover from saved queue.
    feed._content = b'<rss version="2.0"><channel><title>Test feed</title></channel></rss>'
    setup_clients(monkeypatch, rss, [[response([decision(entry(1))])]], models=('stable',))
    runner.run()
    assert json.loads(Path('pending_classification_queue.json').read_text()) == {}
    assert 'Electronic structure sample 1' in Path('filtered_feed_Nature.xml').read_text()
    assert '1 classified, 0 pending retry' in Path('filtered_titles.txt').read_text()
    assert Path('slides.html').exists()


@pytest.mark.parametrize('saved_count', [None, 2])
def test_resume_restores_previous_passed_and_removed_decisions(tmp_path, monkeypatch, saved_count):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, 'install_resilient_feed_fetch', lambda: None)
    monkeypatch.setattr(rss, 'JOURNAL_URLS', {'Nature': 'https://example.invalid/first',
                                          'Science': 'https://example.invalid/second'})
    Path('last_failed_journal.txt').write_text('Science')
    Path('partial_email_content.txt').write_text(
        '--- Nature ---\n  🤖✅ Synthetic pass (link)\n  🤖❌ Synthetic remove (link)\n')
    if saved_count is not None:
        rss.save_json_file('partial_classification_count.json', saved_count)
    rss.save_json_file('pending_classification_queue.json', {'Nature': [entry(3)]})
    monkeypatch.setattr(rss, 'filter_rss_for_journal', lambda *args: (b'<rss/>', [], [], [], [], [], {}))
    for name in ['create_index_html', 'create_results_html_file', 'create_briefing_html', 'create_slideshow_html']:
        monkeypatch.setattr(rss, name, Mock())
    runner.run()
    assert '2 classified, 1 pending retry' in Path('filtered_titles.txt').read_text()
    assert json.loads(Path('pending_classification_queue.json').read_text()) == {'Nature': [entry(3)]}
    assert not Path('partial_classification_count.json').exists()


def test_partial_checkpoint_saves_decision_count(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rss, 'JOURNAL_URLS', {'Nature': 'https://example.invalid/rss'})
    runner.persist_partial('Synthetic audit', [], {}, {}, 'Nature', 3)
    assert json.loads(Path('partial_classification_count.json').read_text()) == 3


def test_live_smoke_wrapper_uses_single_synthetic_fixture(monkeypatch):
    import smoke_test_gemini as smoke
    call = Mock(return_value=([entry(1)], [], [], {}))
    monkeypatch.setattr(smoke.rss, 'classify_entries_with_gemini', call)
    smoke.main()
    journal, fixture = call.call_args.args
    assert journal == 'Nature'
    assert len(fixture) == 1
    assert fixture[0]['link'].startswith('https://example.invalid/')


def test_safe_smoke_workflow_has_no_production_side_effects():
    import yaml
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/Test.yml').read_text())
    assert workflow['permissions'] == {'contents': 'read'}
    live = workflow['jobs']['synthetic-live-smoke']
    assert live['if'] == "github.event_name == 'workflow_dispatch'"
    assert live['timeout-minutes'] == 5
    assert live['needs'] == 'regression'
    steps = live['steps']
    env = steps[-1]['env']
    assert not any(k.startswith('RSS_') or k.startswith('MAIL_') for k in env)
    assert steps[-1]['run'] == 'python smoke_test_gemini.py'
    assert not any('send-mail' in s.get('uses', '') or 'gh-pages' in s.get('uses', '') for s in steps)
