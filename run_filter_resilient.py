"""Run Filter_RSS.py with publisher-feed fetch failures isolated per journal.

The main Filter_RSS module intentionally contains the filtering logic. This
runner mirrors its __main__ loop, but treats source RSS fetch failures such as
transient 403s from publisher sites as a skipped journal instead of a failed
workflow. Cached filtered_feed_*.xml files restored by actions/cache are kept.
"""

import os
import datetime
import email.utils
import requests
import xml.etree.ElementTree as ET

import Filter_RSS as rss


FEED_URLS = {url.strip("<> ") for url in rss.JOURNAL_URLS.values()}
SCIENCE_CROSSREF_FALLBACKS = {
    rss.JOURNAL_URLS["Science"].strip("<> "): ("Science", "0036-8075"),
    rss.JOURNAL_URLS["Science_Advances"].strip("<> "): ("Science Advances", "2375-2548"),
}
ORIGINAL_REQUESTS_GET = rss.requests.get
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def crossref_author_text(authors):
    names = []
    for author in authors or []:
        given = rss.strip_html(author.get("given", ""))
        family = rss.strip_html(author.get("family", ""))
        name = " ".join(part for part in (given, family) if part).strip()
        if name:
            names.append(name)
    return "; ".join(names)


def crossref_date(item):
    for key in ("published-online", "published-print", "issued", "created"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            year, month, day = (list(parts[0]) + [1, 1])[:3]
            try:
                return datetime.datetime(int(year), int(month), int(day), tzinfo=datetime.timezone.utc)
            except Exception:
                continue
    return datetime.datetime.now(datetime.timezone.utc)


def crossref_science_response(feed_url):
    fallback = SCIENCE_CROSSREF_FALLBACKS.get(feed_url)
    if not fallback:
        return None
    journal, issn = fallback
    from_date = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
    api_url = (
        f"https://api.crossref.org/journals/{issn}/works"
        f"?filter=from-pub-date:{from_date}"
        "&sort=published&order=desc&rows=80"
    )
    try:
        resp = ORIGINAL_REQUESTS_GET(
            api_url,
            timeout=30,
            headers={"User-Agent": "hoonica-rss-filter/1.0 (Crossref fallback; mailto:actions@github.com)"},
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except Exception as error:
        print(f"Crossref fallback failed for {journal}: {error}")
        return None
    if not items:
        return None

    rss_el = ET.Element("rss", {"version": "2.0", "xmlns:dc": "http://purl.org/dc/elements/1.1/"})
    channel = ET.SubElement(rss_el, "channel")
    ET.SubElement(channel, "title").text = f"{journal} Crossref fallback"
    ET.SubElement(channel, "link").text = "https://www.science.org/"
    ET.SubElement(channel, "description").text = f"{journal} metadata from Crossref because direct RSS fetch failed."

    for item in items:
        titles = item.get("title") or []
        title = rss.strip_html(titles[0] if titles else "")
        doi = rss.strip_html(item.get("DOI", ""))
        if not title or not doi:
            continue
        article_url = item.get("URL") or f"https://www.science.org/doi/abs/{doi}?af=R"
        article_dt = crossref_date(item)
        abstract = rss.strip_html(item.get("abstract", ""))
        description = abstract or f"{journal}. DOI: {doi}. Crossref fallback metadata; original RSS was unavailable."
        authors = crossref_author_text(item.get("author"))

        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text = title
        ET.SubElement(item_el, "link").text = article_url
        guid = ET.SubElement(item_el, "guid", {"isPermaLink": "false"})
        guid.text = doi
        ET.SubElement(item_el, "description").text = description
        ET.SubElement(item_el, "pubDate").text = email.utils.format_datetime(article_dt)
        if authors:
            ET.SubElement(item_el, "{http://purl.org/dc/elements/1.1/}creator").text = authors

    content = ET.tostring(rss_el, encoding="utf-8", xml_declaration=True)
    response = requests.Response()
    response.status_code = 200
    response.url = api_url
    response._content = content
    response.headers["content-type"] = "application/rss+xml; charset=utf-8"
    print(f"RSS fetch recovered using Crossref fallback: {journal}")
    return response


def install_resilient_feed_fetch():
    """Retry journal RSS fetches with browser-like headers before giving up."""

    def resilient_get(url, *args, **kwargs):
        clean_url = str(url).strip("<> ")
        if clean_url not in FEED_URLS:
            return ORIGINAL_REQUESTS_GET(url, *args, **kwargs)

        timeout = kwargs.get("timeout", 30)
        try:
            response = ORIGINAL_REQUESTS_GET(url, *args, **kwargs)
            if response.status_code < 400:
                return response
            print(f"RSS fetch plain request got HTTP {response.status_code} for {clean_url}; retrying.")
        except requests.exceptions.RequestException as error:
            print(f"RSS fetch plain request failed for {clean_url}: {error}; retrying.")

        retry_attempts = [
            ("browser headers", {"headers": BROWSER_HEADERS, "allow_redirects": True}),
            (
                "browser headers + Science cookie",
                {
                    "headers": {
                        **BROWSER_HEADERS,
                        "Referer": "https://www.science.org/",
                        "Cookie": "cookiePolicy=iaccept",
                    },
                    "allow_redirects": True,
                },
            ),
        ]
        last_response = None
        for label, retry_kwargs in retry_attempts:
            try:
                response = ORIGINAL_REQUESTS_GET(clean_url, timeout=timeout, **retry_kwargs)
                if response.status_code < 400:
                    print(f"RSS fetch recovered using {label}: {clean_url}")
                    return response
                last_response = response
                print(f"RSS fetch retry with {label} got HTTP {response.status_code}: {clean_url}")
            except requests.exceptions.RequestException as error:
                print(f"RSS fetch retry with {label} failed for {clean_url}: {error}")

        crossref_response = crossref_science_response(clean_url)
        if crossref_response is not None:
            return crossref_response

        if last_response is not None:
            return last_response
        return ORIGINAL_REQUESTS_GET(url, *args, **kwargs)

    rss.requests.get = resilient_get


def persist_partial(email_content, briefing_records, pending_queue, new_pending_queue, journal_name):
    with open("partial_email_content.txt", "w", encoding="utf-8") as handle:
        handle.write(email_content)
    rss.save_json_file("partial_briefing_records.json", briefing_records)

    merged_pending = dict(pending_queue)
    processed = list(rss.JOURNAL_URLS.keys())[:list(rss.JOURNAL_URLS.keys()).index(journal_name) + 1]
    for done_journal in processed:
        merged_pending.pop(done_journal, None)
    merged_pending.update(new_pending_queue)
    rss.save_json_file("pending_classification_queue.json", merged_pending)


def minimal_fallback_feed(journal_name, error):
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<rss version='2.0'><channel>"
        f"<title>{rss.safe_text(journal_name)} unavailable</title>"
        f"<description>Source fetch failed: {rss.safe_text(str(error))}</description>"
        "</channel></rss>"
    ).encode("utf-8")


def run():
    install_resilient_feed_fetch()

    output_file_base = "filtered_feed"
    state_file = "last_failed_journal.txt"
    pending_file = "pending_classification_queue.json"
    email_content = ""
    briefing_records = []
    pending_queue = rss.load_json_file(pending_file, {})
    new_pending_queue = {}
    journals_to_process = list(rss.JOURNAL_URLS.items())
    start_index = 0
    resume_mode = False

    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as handle:
            last_failed = handle.read().strip()
        if last_failed and last_failed != "SUCCESS":
            names = list(rss.JOURNAL_URLS.keys())
            if last_failed in names:
                start_index = names.index(last_failed)
                resume_mode = True

    if resume_mode:
        if os.path.exists("partial_email_content.txt"):
            with open("partial_email_content.txt", "r", encoding="utf-8") as handle:
                email_content = handle.read()
        briefing_records = rss.load_json_file("partial_briefing_records.json", [])
        email_content += f"\n\n--- RESUME ---\nResuming from journal: {journals_to_process[start_index][0]}\n\n"
    else:
        rss.clear_partial_state()

    try:
        for journal_name, feed_url in journals_to_process[start_index:]:
            output_filename = f"{output_file_base}_{journal_name}.xml"
            try:
                pending_records_for_journal = pending_queue.get(journal_name, [])
                result = rss.filter_rss_for_journal(journal_name, feed_url, pending_records_for_journal)
                filtered_xml, keyword_passed, gemini_passed, keyword_removed, gemini_removed, gemini_pending, meta = result
                if gemini_pending:
                    new_pending_queue[journal_name] = [rss.serialize_entry_for_pending(e) for e in gemini_pending]
                with open(output_filename, "wb") as handle:
                    handle.write(filtered_xml)

                email_content += f"--- {journal_name} ---\n\nPASSED PAPERS:\n"
                if not keyword_passed and not gemini_passed:
                    email_content += "No papers found matching your filters.\n\n"
                else:
                    for entry in keyword_passed:
                        email_content += f"  OK {rss.display_title_for_entry(entry, journal_name)} ({rss.get_entry_link(entry) or 'No link'})\n"
                        reason = (meta.get(rss.get_entry_link(entry), {}) or {}).get("reason", "")
                        source = "author whitelist" if reason.startswith("author whitelist:") else "keyword"
                        briefing_records.append(rss.paper_record(entry, journal_name, source, meta))
                    for entry in gemini_passed:
                        email_content += f"  GEMINI OK {rss.display_title_for_entry(entry, journal_name)} ({rss.get_entry_link(entry) or 'No link'})\n"
                        briefing_records.append(rss.paper_record(entry, journal_name, "Gemini", meta))
                    email_content += "\n"

                email_content += "REMOVED PAPERS:\n"
                if not keyword_removed and not gemini_removed:
                    email_content += "No papers were filtered out.\n\n"
                else:
                    for entry in keyword_removed:
                        email_content += f"  REMOVED {entry.get('title', 'No title')} ({rss.get_entry_link(entry) or 'No link'})\n"
                    for entry in gemini_removed:
                        email_content += f"  GEMINI REMOVED {entry.get('title', 'No title')} ({rss.get_entry_link(entry) or 'No link'})\n"
                    email_content += "\n"

                email_content += "PENDING RETRY PAPERS:\n"
                if not gemini_pending:
                    email_content += "No papers pending retry.\n\n"
                else:
                    for entry in gemini_pending:
                        email_content += f"  PENDING {entry.get('title', 'No title')} ({rss.get_entry_link(entry) or 'No link'})\n"
                    email_content += "\n"

                persist_partial(email_content, briefing_records, pending_queue, new_pending_queue, journal_name)

            except requests.exceptions.RequestException as error:
                pending_records_for_journal = pending_queue.get(journal_name, [])
                if pending_records_for_journal:
                    new_pending_queue[journal_name] = pending_records_for_journal

                email_content += f"--- {journal_name} ---\n\nSOURCE FETCH SKIPPED:\n"
                email_content += f"  WARNING {error}\n"
                if os.path.exists(output_filename):
                    email_content += f"  Kept cached RSS output: {output_filename}\n\n"
                    print(f"Source fetch failed for {journal_name}; kept cached {output_filename}.")
                else:
                    with open(output_filename, "wb") as handle:
                        handle.write(minimal_fallback_feed(journal_name, error))
                    email_content += f"  Wrote minimal fallback RSS output: {output_filename}\n\n"
                    print(f"Source fetch failed for {journal_name}; wrote minimal fallback feed.")

                persist_partial(email_content, briefing_records, pending_queue, new_pending_queue, journal_name)
                continue

            except Exception as error:
                with open(state_file, "w", encoding="utf-8") as handle:
                    handle.write(journal_name)
                email_content += f"\n\nAn error occurred while running the filter script for '{journal_name}':\n{error}\nPlease check workflow logs.\n"
                raise

        with open(state_file, "w", encoding="utf-8") as handle:
            handle.write("SUCCESS")

        final_pending = dict(pending_queue)
        processed_journals = list(rss.JOURNAL_URLS.keys())[start_index:]
        for journal_name in processed_journals:
            final_pending.pop(journal_name, None)
        final_pending.update(new_pending_queue)
        rss.save_json_file(pending_file, final_pending)
        rss.create_index_html(rss.JOURNAL_URLS, output_file_base)
        rss.create_results_html_file(email_content)
        rss.create_briefing_html(briefing_records, email_content)
        rss.create_slideshow_html(briefing_records)
        rss.clear_partial_state()
    finally:
        github_server_url = os.getenv("GITHUB_SERVER_URL")
        github_repository = os.getenv("GITHUB_REPOSITORY")
        github_run_id = os.getenv("GITHUB_RUN_ID")
        if github_server_url and github_repository and github_run_id:
            email_content += f"\n\n---\n\nCheck GitHub Actions run for details:\n{github_server_url}/{github_repository}/actions/runs/{github_run_id}\n"
        rss.create_email_body_file(email_content)


if __name__ == "__main__":
    run()
