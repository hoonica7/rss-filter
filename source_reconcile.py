"""Compare publisher listings with observed RSS/classifier IDs, per journal."""
from datetime import date, timedelta
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from lxml import html
from lxml.etree import ParserError

from restore_checkpoint import atomic_json, load_json

STATE = "source_coverage.json"
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


def get(session, url, **params):
    response = session.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response


def crossref_record(work):
    parts = (work.get("published-online") or work.get("published") or {}).get("date-parts", [[]])[0]
    return dict(title=(work.get("title") or [""])[0], summary=work.get("abstract", ""),
                link="https://doi.org/" + work["DOI"], id=work["DOI"],
                published="-".join(f"{v:02d}" for v in parts),
                authors=[{"name": " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "")}
                         for a in work.get("author", [])])


def crossref_listing(session, issn):
    since = (date.today() - timedelta(days=3)).isoformat()
    cursor = "*"
    works = {}
    while True:
        msg = get(session, f"https://api.crossref.org/journals/{issn}/works",
                  filter=f"from-online-pub-date:{since},until-online-pub-date:{date.today().isoformat()}",
                  rows=100, cursor=cursor).json()["message"]
        items = msg["items"]
        if not works and not items:
            latest = get(session, f"https://api.crossref.org/journals/{issn}/works",
                         sort="published", order="desc", rows=50).json()["message"]["items"]
            if not latest:
                raise ValueError("no recent Crossref deposits found")
            return {w["DOI"].lower(): crossref_record(w) for w in latest}
        before = len(works)
        works.update({w["DOI"].lower(): crossref_record(w) for w in items if w.get("DOI")})
        if len(items) < 100 or len(works) >= msg["total-results"]:
            return works
        if len(works) == before or not msg.get("next-cursor"):
            raise ValueError("Crossref pagination stalled")
        cursor = msg["next-cursor"]


def metadata(session, key, link, record=None):
    record = dict(record or {})
    if not record and not key.startswith("arxiv:"):
        try:
            work = get(session, "https://api.crossref.org/works/" + key).json()["message"]
            if work["DOI"].lower() != key:
                raise ValueError("Crossref returned a different DOI")
            record = crossref_record(work)
        except requests.HTTPError as exc:
            if exc.response.status_code != 404:
                raise
    if not record.get("summary"):
        try:
            doc = html.fromstring(get(session, link).content)
            title = doc.xpath("string(//meta[@name='citation_title']/@content)")
            if not title and not record.get("title"):
                raise ValueError("no article title or publisher metadata")
            parts = doc.xpath("//section[starts-with(@aria-labelledby,'Abs')]//p | "
                              "//div[@id='Abs1-content']//p | //blockquote[contains(@class,'abstract')]")
            # A Nature abstract can match both section and div paths; XPath union deduplicates nodes.
            summary = " ".join(p.text_content().strip() for p in parts)
            if not summary:
                summary = doc.xpath("string(//meta[@name='citation_abstract' or @name='dc.Description']/@content)")
            record.update(title=record.get("title") or title, summary=summary, id=key,
                          authors=record.get("authors") or [{"name": n} for n in
                                 doc.xpath("//meta[@name='citation_author']/@content")])
        except (requests.RequestException, ParserError):
            if not record:
                raise
    record["link"] = link
    return record


def supplement(journal, current):
    url, issn = LISTS[journal]
    state = load_json(STATE, {})
    ledger = state.setdefault(journal, {})
    in_feed = {entry_id(e) for e in current}
    records = {}
    listed = {}
    source = "publisher"
    errors = (requests.RequestException, ValueError, KeyError, IndexError, ParserError)
    with requests.Session() as session:
        session.headers["User-Agent"] = "RSS-coverage/1.0 (+https://github.com/hoonica7/rss-filter)"
        try:
            content = get(session, url).content
            listed = website_ids(journal, content)
            first = list(listed)
            previous = set(ledger.get("$front", []))
            # Follow actual next-page links until we reach the last snapshot.
            if previous and ("nature.com" in url or "aps.org" in url):
                for page in range(2, 52):
                    if previous.intersection(listed):
                        break
                    next_url = None
                    for href in html.fromstring(content).xpath("//a/@href"):
                        dest = urlsplit(urljoin(url, href))
                        if (dest.netloc, dest.path) == (urlsplit(url).netloc, urlsplit(url).path) and (
                                parse_qs(dest.query).get("page") == [str(page)]):
                            next_url = dest.geturl()
                            break
                    if not next_url:
                        break
                    content = get(session, next_url).content
                    listed.update(website_ids(journal, content))
                else:
                    raise ValueError("could not reach prior snapshot within 51 pages")
            ledger["$front"] = first
        except errors as exc:
            print(f"::warning::Coverage {journal}: website unavailable ({exc})")
            source = "unavailable"
            if issn:
                try:
                    records = crossref_listing(session, issn)
                    listed.update({k: v["link"] for k, v in records.items()})
                    source = "Crossref (3-day window or latest 50 deposits)"
                except errors as err:
                    print(f"::warning::Coverage {journal}: Crossref unavailable ({err})")
        for key, link in listed.items():
            ledger.setdefault(key, link)
        # URLs mean unresolved. Keep them even after the listing rolls over.
        atomic_json(STATE, state)
        added = []
        for key, link in ledger.items():
            if key.startswith("$") or link is True or key in in_feed:
                continue
            try:
                row = metadata(session, key, link, records[key]) if key in records else metadata(session, key, link)
                if not row["title"].strip():
                    raise ValueError("title not available yet")
                added.append(row)
            except errors as exc:
                print(f"::warning::Coverage {journal}: retry metadata {key} ({exc})")
    print(f"Coverage {journal}: {source}; listed={len(listed)}, "
          f"overlap_with_input={len(in_feed.intersection(listed))}, supplemental={len(added)}")
    return added


def acknowledge(journal, decided):
    state = load_json(STATE, {})
    ledger = state.setdefault(journal, {})
    for entry in decided:
        key = entry_id(entry)
        if key:
            ledger[key] = True
    atomic_json(STATE, state)
