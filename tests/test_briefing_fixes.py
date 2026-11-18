from lxml import etree as ET

import Filter_RSS as rss
import enhance_slides as slides


def test_combined_authors_and_initial_alias(monkeypatch):
    assert rss._split_author_string("A. Smith, B. Jones, C. Wang, D. Lee") == [
        "A. Smith", "B. Jones", "C. Wang", "D. Lee"]
    assert rss._split_author_string("Smith, A.; Jones, B.J.") == ["Smith, A.", "Jones, B.J."]
    monkeypatch.setattr(rss, "IMPORTANT_AUTHOR_WHITELIST", ["Alex-Brian Smith"])
    rss.important_author_lookup.cache_clear()
    try:
        assert rss.find_whitelisted_author(["A. B. Smith"])
        assert not rss.find_whitelisted_author(["Adam-Ben Smith"])
    finally:
        rss.important_author_lookup.cache_clear()


def test_abstract_keeps_short_final_result():
    assert rss.clean_abstract_text("<p>Author(s): A. Smith</p><p>We measured the sample at high pressure.</p>"
                                   "<p>No gap appeared.</p><p>[Journal] Published today</p>") == (
        "We measured the sample at high pressure. No gap appeared.")


def test_must_read_matches_visible_section(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rss, "has_a_must_trigger", lambda e: "direct" in e["title"])
    records = [dict(title=t, link=f"https://example.org/{i}", tier="A_MUST_READ", score=i,
                    summary="A & B", journal="Journal") for i, t in enumerate(["direct one", "other", "direct two"])]
    rss.create_briefing_html(records)
    root = ET.parse(rss.MUST_READ_FILE)
    assert root.xpath("//item/title/text()") == ["direct two", "direct one"]
    assert root.xpath("//item/guid/text()") == root.xpath("//item/link/text()")
    rss.create_briefing_html([])
    assert not ET.parse(rss.MUST_READ_FILE).xpath("//item")


def test_slide_41_gets_an_attempt(monkeypatch):
    seen = []
    monkeypatch.setattr(slides, "build_prompt", lambda b: seen.extend(b) or "prompt")
    monkeypatch.setattr(slides, "generate_json_with_gemini", lambda *a, **kw: [])
    papers = [{"title": str(i)} for i in range(41)]
    assert slides.generate_briefings(papers) == {}
    assert seen == papers
