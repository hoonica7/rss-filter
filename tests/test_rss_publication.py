import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from lxml import etree as ET
import rss_publication as p

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
URL = 'https://arxiv.org/abs/2611.09857'

def rss(title='[10] Paper', date='Mon, 09 Nov 2026 00:00:00 -0500', items=True):
    item = f'<item><title>{title}</title><link>{URL}</link><guid>{URL}</guid><pubDate>{date}</pubDate><description>score ten</description></item>' if items else ''
    return f'<rss><channel><title>Feed</title>{item}</channel></rss>'.encode()

def run(tmp_path, monkeypatch, xml, pending=(), now=NOW):
    monkeypatch.chdir(tmp_path)
    result = p.stabilize_feed(xml, 'test', pending, 'out.xml', now)
    Path('out.xml').write_bytes(result)
    return ET.fromstring(result)

def test_first_snapshot_frozen(tmp_path, monkeypatch):
    first = run(tmp_path, monkeypatch, rss())
    second = run(tmp_path, monkeypatch, rss('[7] Changed'), now=NOW+timedelta(days=1))
    assert ET.tostring(first.find('.//item')) == ET.tostring(second.find('.//item'))
    assert second.findtext('.//pubDate').startswith('Mon, 09')

def test_recovered_once_and_source_reappears(tmp_path, monkeypatch):
    pending = [{'link': URL+'v1', 'published':'2026-11-09'}]
    first = run(tmp_path, monkeypatch, rss(), pending)
    item = first.find('.//item'); guid = item.findtext('guid')
    assert item.findtext('title').startswith('[RSS recovery]')
    assert 'Original source' in item.findtext('description')
    assert 'Mon, 09 Nov' in item.findtext('description')
    assert guid.startswith('urn:rss-filter:recovery:')
    later = run(tmp_path, monkeypatch, rss('[2] Changed'), pending, NOW+timedelta(days=1))
    assert later.findtext('.//guid') == guid
    assert later.findtext('.//pubDate') == item.findtext('pubDate')
    assert len(later.findall('.//item')) == 1

def test_known_pending_not_reissued(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, rss())
    result=run(tmp_path, monkeypatch, rss(), [{'link':URL}])
    assert result.findtext('.//guid') == URL

def test_retention_and_no_reset_after_expiry(tmp_path, monkeypatch):
    first=run(tmp_path, monkeypatch, rss(), [{'link':URL}])
    retained=run(tmp_path, monkeypatch, rss(items=False), now=NOW+timedelta(days=29))
    assert len(retained.findall('.//item')) == 1
    expired=run(tmp_path, monkeypatch, rss(items=False), now=NOW+timedelta(days=31))
    assert not expired.findall('.//item')
    returned=run(tmp_path, monkeypatch, rss(), [{'link':URL}], NOW+timedelta(days=32))
    assert returned.findtext('.//guid') == first.findtext('.//guid')

def test_seed_confirmed_manual_reissue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('out.xml').write_bytes(rss('[RSS reissue] Paper').replace(f'<guid>{URL}</guid>'.encode(), b'<guid>urn:confirmed</guid>'))
    result=run(tmp_path, monkeypatch, rss(), [{'link':URL+'v1'}])
    assert result.findtext('.//guid') == 'urn:confirmed'

def test_atom(tmp_path, monkeypatch):
    xml=f'<feed xmlns="{p.ATOM}"><entry><title>P</title><id>x</id><link href="{URL}"/><published>2026-11-09T00:00:00Z</published><updated>2026-11-10T00:00:00Z</updated><content type="text">a &lt; b</content></entry></feed>'.encode()
    root=run(tmp_path, monkeypatch,xml,[{'link':URL}]); ns={'a':p.ATOM}
    assert root.findtext('a:entry/a:published',namespaces=ns) == NOW.isoformat()
    assert '&lt;' in root.findtext('a:entry/a:content',namespaces=ns)
    assert root.findtext('a:entry/a:id',namespaces=ns).startswith('urn:')

def test_rdf_sequence_and_date(tmp_path, monkeypatch):
    xml=f'<rdf:RDF xmlns:rdf="{p.RDF}" xmlns="{p.RSS1}" xmlns:dc="{p.DC}"><channel><items><rdf:Seq><rdf:li rdf:resource="{URL}"/></rdf:Seq></items></channel><item rdf:about="{URL}"><title>P</title><link>{URL}</link><description>x</description></item></rdf:RDF>'.encode()
    root=run(tmp_path, monkeypatch,xml,[{'link':URL,'published':'2026-11-09'}]); item=root.find('{%s}item'%p.RSS1)
    assert item.findtext('{%s}date'%p.DC)==NOW.isoformat()
    assert '2026-11-09' in item.findtext('{%s}description'%p.RSS1)
    assert root.find('.//{%s}li'%p.RDF).get('{%s}resource'%p.RDF)==item.get('{%s}about'%p.RDF)

def test_bad_input_does_not_destroy_state(tmp_path,monkeypatch):
    run(tmp_path,monkeypatch,rss())
    before=Path(p.LEDGER).read_bytes()
    with pytest.raises(ET.XMLSyntaxError):
        run(tmp_path,monkeypatch,b'bad')
    assert Path(p.LEDGER).read_bytes()==before

def test_published_ledger_wins_stale_cache():
    local=p.bootstrap_history(p.empty_history(),{'filtered_feed_test.xml':rss('[3] stale')},NOW)
    live=p.bootstrap_history(p.empty_history(),{'filtered_feed_test.xml':rss('[10] published')},NOW)
    merged=p.merge_histories(local,live)
    assert 'published' in merged['journals']['test']['arxiv:2611.09857']['xml']

def test_duplicate_items(tmp_path,monkeypatch):
    xml=rss().replace(b'</channel>',rss().split(b'<channel>')[1].split(b'<item>')[1].split(b'</channel>')[0].join([b'<item>',b''])+b'</channel>')
    result=run(tmp_path,monkeypatch,xml)
    assert len(result.findall('.//item'))==1

def test_atom_initial_dates_distinct_and_unchanged(tmp_path, monkeypatch):
    xml=f'<feed xmlns="{p.ATOM}"><entry><title>P</title><id>x</id><link href="{URL}"/><published>2026-11-09T00:00:00Z</published><updated>2026-11-10T00:00:00Z</updated></entry></feed>'.encode()
    root=run(tmp_path, monkeypatch,xml)
    assert root.findtext('.//{%s}published'%p.ATOM)=='2026-11-09T00:00:00Z'
    assert root.findtext('.//{%s}updated'%p.ATOM)=='2026-11-10T00:00:00Z'

def test_missing_date_is_explicit_not_invented(tmp_path, monkeypatch):
    xml=rss().replace(b'<pubDate>Mon, 09 Nov 2026 00:00:00 -0500</pubDate>',b'')
    root=run(tmp_path,monkeypatch,xml,[{'link':URL}])
    assert 'not supplied by source' in root.findtext('.//description')


def test_invalid_ledger_does_not_reset(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(p.LEDGER).write_text('{"version":99,"journals":{}}')
    with pytest.raises(ValueError):
        p.stabilize_feed(rss(),'test')
    assert '99' in Path(p.LEDGER).read_text()

def test_atom_xhtml_keeps_content_and_notice(tmp_path, monkeypatch):
    xml=f'<feed xmlns="{p.ATOM}"><entry><title>P</title><id>x</id><link href="{URL}"/><published>2026-11-09T00:00:00Z</published><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Original</p></div></content><summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Summary</p></div></summary></entry></feed>'.encode()
    root=run(tmp_path, monkeypatch,xml,[{'link':URL}])
    for tag in ('content','summary'):
        el=root.find('.//{%s}%s'%(p.ATOM,tag))
        assert el.get('type')=='xhtml'
        assert 'RSS recovery' in ''.join(el.itertext())
    assert 'Original' in ''.join(root.itertext())
