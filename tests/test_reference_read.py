import json
from pathlib import Path

import feedparser

import Filter_RSS as rss
from gemini_runtime import configured_models
from reference_read import is_reference_read


def configure(monkeypatch):
    monkeypatch.setenv('RSS_REFERENCE_RULES_JSON', json.dumps({'topic': 'sample', 'use': 'measurement'}))


def test_reference_requires_genre_topic_and_use(monkeypatch):
    configure(monkeypatch)
    assert is_reference_read('A perspective on samples', 'Measurement')
    assert is_reference_read('A sample', 'In this Perspective we explain the measurement.')
    assert not is_reference_read('A sample measurement: a geometric perspective', 'Original calculation')
    assert not is_reference_read('Tutorial: samples', '')
    assert not is_reference_read('Tutorial: measurement', '')
    assert not is_reference_read('Correction: Tutorial on sample measurement', '')
    monkeypatch.delenv('RSS_REFERENCE_RULES_JSON')
    assert not is_reference_read('Tutorial: sample measurement', '')


def test_reference_feed_and_briefing_are_separate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    configure(monkeypatch)
    info = dict(title='Tutorial: Sample measurement', link='https://example.org/ref',
                summary='Original abstract', authors=[{'name': 'A Person'}],
                media_thumbnail=[{'url': 'https://example.org/figure.jpg'}])
    rescued, = rss.rescued_reference_records([info,dict(title='Not a review')], 'Nature',
        {info['link']: dict(score=4, tier='C_MAYBE', reason='A review')})
    passed = dict(rescued, link='https://example.org/passed', score=8, reference_only=False)
    records = [rescued, passed, rescued]
    rss.create_briefing_html(records)
    rss.create_slideshow_html(records)
    rss.create_index_html({}, 'filtered_feed')
    result = feedparser.parse(rss.REFERENCE_READ_FILE)
    assert [(r.id,r.title) for r in result.entries] == [
        (passed['link'], '[8] '+info['title']), (info['link'], '[4] '+info['title'])]
    item = result.entries[1]
    assert item.media_thumbnail[0]['url'].endswith('figure.jpg')
    assert all(x in item.summary for x in ['Original abstract','Last authors:', 'Score 4/10'])
    assert not feedparser.parse(rss.MUST_READ_FILE).entries
    assert info['link'] not in Path('slides.html').read_text()
    brief = Path('briefing.html').read_text()
    assert info['link'] not in brief.split("id='reference-read'")[0]
    assert info['link'] in brief.split("id='reference-read'")[1]
    assert rss.REFERENCE_READ_FILE in Path('index.html').read_text()
    rss.create_briefing_html([])
    assert not feedparser.parse(rss.REFERENCE_READ_FILE).entries


def test_model_defaults_and_override(monkeypatch):
    monkeypatch.setenv('GEMINI_MODELS', ' ')
    assert configured_models() == ['gemini-4-hydrogen', 'gemini-3.8-flash']
    monkeypatch.setenv('GEMINI_MODELS', ' new-first, second ')
    assert configured_models() == ['new-first','second']
