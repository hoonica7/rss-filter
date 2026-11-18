import json
from types import SimpleNamespace

import pytest
import requests

import Filter_RSS as rss
import source_reconcile as sc


def test_all_journals_and_schemes():
    assert set(sc.LISTS) == set(rss.JOURNAL_URLS)
    assert sc.paper_id("https://www.nature.com/articles/s1?x=y") == sc.paper_id("doi:10.1038/s1")
    assert sc.paper_id("https://arxiv.org/abs/2611.12345v2") == "arxiv:2611.12345"
    cases = [
        ("Nature", "<article><h3><a href='/articles/s1'>X</a></h3></article>", "10.1038/s1"),
        ("Science", "<a href='/doi/10.1126/science.X'>X</a>", "10.1126/science.x"),
        ("PRL_Recent", "<h2><a href='/prl/abstract/10.1103/x'>X</a></h2>", "10.1103/x"),
        ("arXiv_CondMat", "<dl><dt><a href=' /abs/2611.12345 '>X</a></dt></dl>", "arxiv:2611.12345"),
    ]
    for journal, body, key in cases:
        assert list(sc.website_ids(journal, body)) == [key]
    with pytest.raises(ValueError):
        sc.website_ids("Science", "<a href='/doi/10.1126/sciadv.x'>Wrong journal</a>")


def test_compare_follow_pages_preserve_retry(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    sc.atomic_json(sc.STATE, {"Nature": {"$front": ["10.1038/old"], "10.1038/old": True}})
    def page(session, url):
        keys = ["old", "extra"] if "page=2" in url else ["current", "retry"]
        links = "".join(f"<article><h3><a href='/articles/{k}'>x</a></h3></article>" for k in keys)
        return SimpleNamespace(content=links + "<a href='?page=2'>next</a>")
    monkeypatch.setattr(sc, "get", page)
    def metadata(session, key, link):
        if key.endswith("retry"):
            raise requests.HTTPError("unavailable")
        return dict(title="paper", link=link)
    monkeypatch.setattr(sc, "metadata", metadata)
    current = [{"link": "https://doi.org/10.1038/current"}]
    extra = sc.supplement("Nature", current)
    assert [sc.entry_id(e) for e in extra] == ["10.1038/extra"]
    sc.acknowledge("Nature", current + extra)
    assert sc.supplement("Nature", []) == []
    assert json.loads(tmp_path.joinpath(sc.STATE).read_text())["Nature"]["10.1038/retry"].endswith("/retry")


def test_science_fallback_empty_window(monkeypatch):
    def get(session, url, **params):
        items = [] if "filter" in params else [{"DOI":"10.1126/science.x", "title":["test"]}]
        return SimpleNamespace(json=lambda: {"message": {"items":items, "total-results":len(items)}})
    monkeypatch.setattr(sc, "get", get)
    assert "10.1126/science.x" in sc.crossref_listing(None, "0036-8075")


def test_website_only_paper_reaches_output(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    link = "https://www.nature.com/articles/test"
    monkeypatch.setattr(sc, "supplement", lambda *_: [dict(title="test paper", link=link, summary="two paragraphs")])
    monkeypatch.setattr(rss, "find_title_autopass", lambda _: "test")
    monkeypatch.setattr(rss, "get_article_image", lambda *_: None)
    monkeypatch.setattr(rss.requests, "get", lambda *a, **kw: SimpleNamespace(
        content=b"<rss version='2.0'><channel><title>Test</title></channel></rss>", raise_for_status=lambda: None))
    xml, passed, *_ = rss.filter_rss_for_journal("Nature", "https://example.org/feed")
    assert [e["link"] for e in passed] == [link]
    assert rss.ET.fromstring(xml).findtext("channel/item/link") == link
