from types import SimpleNamespace

import pytest

import audit_feed_coverage as sc
from journal_sources import JOURNAL_URLS

def test_all_journals_and_schemes():
    assert set(sc.LISTS) == set(JOURNAL_URLS)
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



def test_only_reports_website_only_paper(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    data = {
        JOURNAL_URLS["Nature"]: b"<rss version='2.0'><channel><item><title>seen</title>"
                                b"<link>https://doi.org/10.1038/seen</link></item></channel></rss>",
        sc.LISTS["Nature"][0]: b"<article><h3><a href='/articles/seen'>Seen</a></h3></article>"
                               b"<article><h3><a href='/articles/other'>Other</a></h3></article>",
    }
    session = SimpleNamespace(get=lambda url, **kw: SimpleNamespace(content=data[url], raise_for_status=lambda: None))
    report = sc.audit(session, "Nature")
    assert "1/2" in report and "10.1038/other" in report
    assert not list(tmp_path.iterdir())


def test_filter_ignores_old_supplement_ledger(monkeypatch, tmp_path):
    import Filter_RSS as rss
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source_coverage.json").write_text('{"Nature":{"10.1038/old":"https://doi.org/10.1038/old"}}')
    def fetch(url, **kw):
        assert url == JOURNAL_URLS["Nature"]
        return SimpleNamespace(content=b"<rss version='2.0'><channel><title>Nature</title></channel></rss>",
                               raise_for_status=lambda: None)
    monkeypatch.setattr(rss.requests, "get", fetch)
    xml, *decisions, meta = rss.filter_rss_for_journal("Nature", JOURNAL_URLS["Nature"])
    assert not any(decisions) and not meta and b"10.1038/old" not in xml
