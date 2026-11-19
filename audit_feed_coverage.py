"""Read-only, point-in-time audit. Never import the production classifier."""
import os
import re
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from lxml import html
from lxml.etree import ParserError

from journal_sources import JOURNAL_URLS

LISTS = {
    "Nature": ("https://www.nature.com/nature/research-articles", "0028-0836"),
    "Nature_Physics": ("https://www.nature.com/nphys/research-articles", "1745-2473"),
    "Nature_Materials": ("https://www.nature.com/nmat/research-articles", "1476-1122"),
    "Nature_Communications": ("https://www.nature.com/ncomms/research-articles", "2041-1723"),
    "npj_QuantumMaterials": ("https://www.nature.com/npjquantmats/research-articles", "2397-4648"),
    "Science": ("https://www.science.org/toc/science/current", "0036-8075"),
    "Science_Advances": ("https://www.science.org/toc/sciadv/current", "2375-2548"),
    "PRL_Recent": ("https://journals.aps.org/prl/recent", "0031-9007"),
    "PRB_Recent": ("https://journals.aps.org/prb/recent", "2469-9950"),
    "arXiv_CondMat": ("https://arxiv.org/list/cond-mat/new?show=2000", None),
}


def paper_id(value):
    value = (value or "").lower().strip().split("?")[0].split("#")[0]
    m = re.search(r"arxiv(?:\.org/(?:abs|pdf)/|:)(\d{4}\.\d{4,5})", value)
    if m:
        return "arxiv:" + m[1]
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", value)
    if m:
        return m[1]
    m = re.search(r"nature\.com/articles/([\w.-]+)", value)
    return "10.1038/" + m[1] if m else ""


def entry_id(entry):
    for field in ("link", "doi", "prism_doi", "id"):
        key = paper_id(entry.get(field))
        if key:
            return key
    return ""


def website_ids(journal, content):
    url, _ = LISTS[journal]
    doc = html.fromstring(content)
    if "nature.com" in url:
        paths = doc.xpath("//article//h3//a/@href | //a[contains(@class,'c-card__link')]/@href")
        pattern = r"/articles/[\w.-]+"
    elif "aps.org" in url:
        paths = doc.xpath("//h2//a/@href | //h3//a/@href | //h5//a/@href")
        pattern = r"/" + urlsplit(url).path.split("/")[1] + r"/(?:abstract|article)/10\.1103/.+"
    elif "science.org" in url:
        paths = doc.xpath("//a/@href")
        prefix = "science" if journal == "Science" else "sciadv"
        pattern = rf"/doi/(?:abs/|full/)?10\.1126/{prefix}\.[\w.-]+"
    else:
        paths = doc.xpath("//dt//a/@href")
        pattern = r"/abs/\d{4}\.\d{4,5}(?:v\d+)?"
    found = {}
    for href in paths:
        link = urljoin(url, href.strip())
        parts = urlsplit(link)
        if parts.hostname == urlsplit(url).hostname and re.fullmatch(pattern, parts.path, re.I):
            found[paper_id(link)] = link
    if not found:
        if journal == "arXiv_CondMat" and re.search(r"Total of 0 entries", doc.text_content(), re.I):
            return {}
        raise ValueError("no recognized article links")
    return found


def audit(session, journal):
    def fetch(url):
        response = session.get(url, timeout=25)
        response.raise_for_status()
        return response.content

    feed = feedparser.parse(fetch(JOURNAL_URLS[journal]))
    if not feed.version or (feed.bozo and not feed.entries):
        raise ValueError("RSS unavailable or invalid")
    ids = {entry_id(e) for e in feed.entries} - {""}
    if feed.entries and not ids:
        raise ValueError("RSS has no comparable DOI/arXiv IDs")
    listed = website_ids(journal, fetch(LISTS[journal][0]))
    missing = sorted(listed.keys() - ids)
    return (f"{len(feed.entries)} RSS entries; {len(ids)} distinct IDs; "
            f"{len(listed) - len(missing)}/{len(listed)} listed IDs in this RSS snapshot."
            + (" Examples not in snapshot: " + ", ".join(missing[:5]) if missing else ""))


def main():
    lines = ["## Weekly RSS spot check", "",
             "Current publisher listing vs. raw RSS only. Different dates, feed windows and update times "
             "can produce differences. This is not a historical miss rate. No papers are imported.", ""]
    with requests.Session() as session:
        session.headers["User-Agent"] = "RSS-audit/1.0 (+https://github.com/hoonica7/rss-filter)"
        for journal in JOURNAL_URLS:
            try:
                result = audit(session, journal)
            except (requests.RequestException, ValueError, ParserError) as exc:
                result = f"Not verified: {exc}"
            lines.append(f"- **{journal}**: {result}")
    report = "\n".join(lines)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            print(report, file=handle)


if __name__ == "__main__":
    main()
