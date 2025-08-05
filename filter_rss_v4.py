#
# This script filters RSS feeds from various scientific journals,
# selects only papers matching specific keywords, and performs additional validation using the Gemini API.
#
# Main features:
# 1. Batch process multiple journal RSS feeds.
# 2. First filtering based on WHITELIST and BLACKLIST keywords.
# 3. Batch Gemini API call for entries not filtered in the first step, minimizing the number of API calls.
# 4. Automatically switch to a backup Gemini model and retry if API quota error occurs.
# 5. If an error occurs during execution, save the failed journal name in a state file and resume from there in the next run.
# 6. If all journals are processed successfully, write 'SUCCESS' to the state file to start from the beginning on the next run.
# 7. Generate an email body file with both filtered and removed entries.
# 8. Create index.html and individual .xml files for the filtered RSS feeds.
# 9. **(Added)** Automatically append a link to the current GitHub Action run at the bottom of the email.
# 10. **(Added)** Separate email contents by journal.
# 11. **(Added)** Add emojis to indicate filtering method (keyword or Gemini) in the email body.
# 12. **(Added)** Add a button in index.html that leads to the HTML results page.
# 13. **(Added)** Mark removed papers by filtering method (keyword or Gemini) in the email.
# 14. **(Added)** The "Filter results" button opens an HTML page where you can click individual paper links just like the email.
# 15. **(Added)** Show last update time in both Texas (CDT) and Korea (KST) at the bottom of index.html.
# 16. **(Added)** Apply custom keyword and Gemini filter rules for arXiv and PRB journals.
#

import feedparser
import lxml.etree as ET
import requests
from io import BytesIO
import sys
import os
import time
import json
import google.generativeai as genai
import datetime
import google.api_core.exceptions as exceptions
import re

# ANSI color code definitions
COLOR_GREEN = '\033[92m'
COLOR_RED = '\033[91m'
COLOR_YELLOW = '\033[93m'
COLOR_ORANGE = '\033[38;5;208m'
COLOR_BLUE = '\033[94m'
COLOR_END = '\033[0m'

# General journal filter criteria
GENERAL_WHITELIST = ["condensed matter", "solid state", "ARPES", "photoemission", "band structure", "Fermi surface", "Brillouin zone", "spin-orbit", "quantum oscillation", "quantum Hall", "Landau leve[...]
GENERAL_BLACKLIST = ["congress", "forest", "climate", "lava", "protein", "archeologist", "mummy", "cancer", "tumor", "immune", "immunology", "inflammation", "antibody", "cytokine", "gene", "tissue", "[...]

# arXiv and PRB journal filter criteria (stricter filtering)
ARXIV_PRB_WHITELIST = ["ARPES", "angle-resolved", "Berry phase", "Kondo", "Mott", "Hubbard", "moiré", "twisted", "graphene", "Kagome", "CsV3Sb5", "V3Sb5", "Ti3Sb5", "magneto", "Luttinger", "NbSe3", "[...]
ARXIV_PRB_BLACKLIST = ["cancer"]

# Journal RSS feed URLs
JOURNAL_URLS = {
    "Nature": "https://www.nature.com/nature.rss",
    "Nature_Physics": "https://feeds.nature.com/nphys/rss/current",
    "Nature_Materials": "https://feeds.nature.com/nmat/rss/current",
    "Nature_Communications": "https://www.nature.com/ncomms.rss",
    "npj_QuantumMaterials": "https://www.nature.com/npjquantmats.rss",
    "Science": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
    "Science_Advances": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv",
    "PRL_Recent": "https://feeds.aps.org/rss/recent/prl.xml",
    "PRB_Recent": "https://feeds.aps.org/rss/recent/prb.xml",
    "arXiv_CondMat": "https://rss.arxiv.org/rss/cond-mat",
}

# Initialize Gemini model
primary_model = 'gemini-2.0-flash'
fallback_model = 'gemini-1.5-flash-latest'
current_model = None
using_primary_model = True
try:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        current_model = genai.GenerativeModel(primary_model)
        print(f"Gemini API configured successfully using primary model: {primary_model}", file=sys.stderr)
    else:
        print("GOOGLE_API_KEY not found. Gemini filter will be skipped.", file=sys.stderr)
except Exception as e:
    print(f"Error configuring Gemini API: {e}", file=sys.stderr)
    using_primary_model = False

def filter_rss_for_journal(journal_name, feed_url):
    """
    Filters the given RSS feed URL based on criteria and returns the results.
    Applies different filter rules depending on the journal.
    """

def create_email_body_file(email_body_content):
    """
    Creates the email body file
    """
    EMAIL_BODY_FILE = "filtered_titles.txt"
    try:
        with open(EMAIL_BODY_FILE, 'w', encoding='utf-8') as f:
            f.write(email_body_content)
        print(f"Successfully created {EMAIL_BODY_FILE} for email.", file=sys.stderr)
    except Exception as e:
        print(f"Error creating email body file: {e}", file=sys.stderr)

def create_results_html_file(email_body_content):
    """
    Generates a clickable HTML results page with the same format as the email body.
    """
    print("--- Generating HTML results page: filtered_results.html ---", file=sys.stderr)

    # HTML template (header)
    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Filtered Paper Results</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
        body {{
            font-family: 'Inter', sans-serif;
            background-color: #f3f4f6;
        }}
    </style>
</head>
<body class="bg-gray-100 p-8">
    <div class="max-w-7xl mx-auto bg-white rounded-xl shadow-2xl p-8">
        <h1 class="text-3xl font-bold text-gray-800 mb-6 text-center">Filtered Paper Results</h1>
        <div class="space-y-2">
"""
    # Convert email body lines to HTML
    email_lines = email_body_content.strip().split('\n')
    for line in email_lines:
        line = line.strip()
        if not line:
            continue
        # Journal separator
        if line.startswith("---"):
            journal_name = line.replace("---", "").strip()
            html_content += f'<h2 class="text-xl font-bold text-indigo-700 mt-6 mb-2">{journal_name}</h2>'
        # Section title (PASSED PAPERS:, REMOVED PAPERS:)
        elif line.endswith(":"):
            html_content += f'<p class="text-lg font-semibold text-gray-800 mt-4">{line}</p>'
        # Paper link line
        else:
            match = re.match(r'^(.*?)\s(.+)\s\((http[s]?://.+)\)$', line)
            if match:
                emoticon = match.group(1).strip()
                title = match.group(2).strip()
                link = match.group(3).strip()
                # Make clickable link
                html_content += f"""
                <div class="p-2 bg-gray-50 rounded-lg shadow-sm hover:bg-gray-100 transition duration-300">
                    <p class="text-gray-700 text-sm font-medium whitespace-nowrap overflow-hidden overflow-ellipsis">
                        {emoticon} <a href="{link}" target="_blank" class="text-blue-600 hover:text-blue-800 hover:underline">{title}</a>
                    </p>
                </div>
"""
            else:
                # GitHub Actions link
                if 'Check GitHub Actions run for details' in line:
                    action_url = line.split(":\n")[-1].strip()
                    html_content += f"""
                    <div class="mt-8 text-sm text-gray-500">
                        <p>Check GitHub Actions run for details:</p>
                        <a href="{action_url}" target="_blank" class="text-indigo-600 hover:underline">{action_url}</a>
                    </div>
                    """
                else:
                    html_content += f'<p class="text-gray-600 ml-6">{line}</p>'
    html_content += """
        </div>
    </div>
</body>
</html>
"""
    try:
        with open('filtered_results.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        print("--- Finished generating HTML results page: filtered_results.html ---", file=sys.stderr)
    except Exception as e:
        print(f"Error while generating HTML results page: {e}", file=sys.stderr)

def create_index_html(journal_urls, rss_base_filename):
    """
    Generates index.html showing filtered RSS feed links for each journal.
    """
    print("--- Generating HTML page: index.html ---", file=sys.stderr)
    
    # Get current UTC time, convert to KST and CDT
    now_utc = datetime.datetime.utcnow()
    now_korea = now_utc + datetime.timedelta(hours=9)
    now_texas = now_utc - datetime.timedelta(hours=5)

    korea_time_str = now_korea.strftime('%Y-%m-%d %H:%M:%S') + " (Korea, KST)"
    texas_time_str = now_texas.strftime('%Y-%m-%d %H:%M:%S') + " (Texas, CDT)"

    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Filtered Paper RSS Feeds</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
        body {{
            font-family: 'Inter', sans-serif;
            background-color: #f3f4f6;
        }}
    </style>
</head>
<body class="bg-gray-100 flex items-center justify-center min-h-screen p-4">
    <div class="bg-white rounded-xl shadow-2xl p-8 max-w-lg w-full text-center">
        <h1 class="text-3xl font-bold text-gray-800 mb-2">Filtered Paper RSS Feeds</h1>
        <p class="text-gray-600 mb-8">
            This page provides RSS feeds for selected journals filtered for ARPES and condensed matter physics papers only.
            Click the links below to subscribe via Reeder app or other RSS readers.
        </p>
        <div class="space-y-4">
"""
    for journal_name in journal_urls.keys():
        safe_journal_name = journal_name.replace(" ", "_").replace("/", "_")
        filename = f"{rss_base_filename}_{safe_journal_name}.xml"
        html_content += f"""
            <a href="{filename}" target="_blank" class="block w-full px-6 py-4 bg-indigo-600 text-white font-semibold rounded-lg shadow-md hover:bg-indigo-700 transition duration-300">
                View {journal_name} RSS Feed
            </a>
"""
    html_content += """
            <a href="filtered_results.html" target="_blank" class="block w-full px-6 py-4 bg-green-600 text-white font-semibold rounded-lg shadow-md hover:bg-green-700 transition duration-300">
                Filter Results
            </a>
        </div>
        <div class="mt-8 text-sm text-gray-500">
            <p>Last update (Korea): """ + korea_time_str + """</p>
            <p>Last update (Texas): """ + texas_time_str + """</p>
        </div>
        <div class="mt-4 text-sm text-gray-500">
            <a href="https://yilab.rice.edu/people/" target="_blank" class="hover:underline text-blue-700">Created by Jounghoon Hyun</a>
        </div>
    </div>
</body>
</html>
"""
    try:
        with open('index.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        print("--- Finished generating HTML page: index.html ---", file=sys.stderr)
    except Exception as e:
        print(f"Error while generating HTML page: {e}", file=sys.stderr)


if __name__ == '__main__':
    OUTPUT_FILE_BASE = "filtered_feed"
    STATE_FILE = "last_failed_journal.txt"
    
    email_content = ""
    
    journals_to_process = list(JOURNAL_URLS.items())
    start_index = 0
    # Check state file to resume from last failed journal
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            last_failed_journal = f.read().strip()
        
        if last_failed_journal == 'SUCCESS':
            print(f"{COLOR_GREEN}Previous workflow run was successful. Starting from the beginning.{COLOR_END}", file=sys.stderr)
            start_index = 0
        elif last_failed_journal:
            print(f"{COLOR_RED}Found state file. Continuing from journal: {last_failed_journal}{COLOR_END}", file=sys.stderr)
            try:
                journal_names = list(JOURNAL_URLS.keys())
                start_index = journal_names.index(last_failed_journal)
            except ValueError:
                print(f"Last failed journal '{last_failed_journal}' not found in JOURNAL_URLS. Starting from the beginning.", file=sys.stderr)
                start_index = 0
        else:
            print(f"{COLOR_GREEN}Found an empty state file. Starting from the beginning.{COLOR_END}", file=sys.stderr)
            start_index = 0
            
    try:
        # Process all journals and filter
        for journal_name, feed_url in journals_to_process[start_index:]:
            try:
                filtered_xml, keyword_passed_entries, gemini_passed_entries, keyword_removed_entries, gemini_removed_entries = filter_rss_for_journal(journal_name, feed_url)
                
                output_filename = f"{OUTPUT_FILE_BASE}_{journal_name}.xml"
                with open(output_filename, 'wb') as f:
                    f.write(filtered_xml)
                print(f"Successfully wrote filtered RSS feed to {output_filename}", file=sys.stderr)

                # Add to email content
                email_content += f"--- {journal_name} ---\n\n"
                
                email_content += f"PASSED PAPERS:\n"
                if not keyword_passed_entries and not gemini_passed_entries:
                    email_content += 'No papers found matching your filters.\n\n'
                else:
                    for entry in keyword_passed_entries:
                        email_content += f"  ✅ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    for entry in gemini_passed_entries:
                        email_content += f"  🤖✅ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    email_content += "\n"
                
                email_content += f"REMOVED PAPERS:\n"
                if not keyword_removed_entries and not gemini_removed_entries:
                    email_content += 'No papers were filtered out.\n\n'
                else:
                    for entry in keyword_removed_entries:
                        email_content += f"  ❌ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    for entry in gemini_removed_entries:
                        email_content += f"  🤖❌ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    email_content += "\n"

            except Exception as e:
                print(f"An error occurred while processing journal '{journal_name}': {e}", file=sys.stderr)
                with open(STATE_FILE, 'w') as f:
                    f.write(journal_name)
                # Compose email content for error
                email_content += f"\n\nAn error occurred while running the filter script for '{journal_name}':\n{e}\nPlease check the workflow logs for more details.\n"
                raise

        try:
            # Update state file to 'SUCCESS' if all journals processed
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'w') as f:
                    f.write('SUCCESS')
                print("Successfully processed all journals and updated the state file with 'SUCCESS'.", file=sys.stderr)
            else:
                with open(STATE_FILE, 'w') as f:
                    f.write('SUCCESS')
                print("Successfully processed all journals. Creating a new state file with 'SUCCESS'.", file=sys.stderr)
        except OSError as e:
            print(f"Warning: Could not create/reset state file '{STATE_FILE}': {e}", file=sys.stderr)
            
        create_index_html(JOURNAL_URLS, OUTPUT_FILE_BASE)
        create_results_html_file(email_content)

    finally:
        # Compose GitHub Actions link
        github_server_url = os.getenv("GITHUB_SERVER_URL")
        github_repository = os.getenv("GITHUB_REPOSITORY")
        github_run_id = os.getenv("GITHUB_RUN_ID")

        if github_server_url and github_repository and github_run_id:
            action_url = f"{github_server_url}/{github_repository}/actions/runs/{github_run_id}"
            email_content += f"\n\n---\n\nCheck GitHub Actions run for details:\n{action_url}\n"
        
        create_email_body_file(email_content)
