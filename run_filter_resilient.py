"""Run Filter_RSS.py with publisher-feed fetch failures isolated per journal.

The main Filter_RSS module intentionally contains the filtering logic. This
runner mirrors its __main__ loop, but treats source RSS fetch failures such as
transient 403s from publisher sites as a skipped journal instead of a failed
workflow. Cached filtered_feed_*.xml files restored by actions/cache are kept.
"""

import os
import requests

import Filter_RSS as rss


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
