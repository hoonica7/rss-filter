#
# This script filters RSS feeds from multiple scientific journals,
# selects papers that match specific keywords, and performs a secondary
# verification using the Gemini API.
#
# Key Features:
# 1. Batch processes multiple journal RSS feeds.
# 2. Performs a primary filter using WHITELIST and BLACKLIST keywords.
# 3. Executes a secondary filter on remaining items using the Gemini API (batch processing to minimize API calls).
# 4. Automatically switches to a fallback model and retries if a Gemini API quota error occurs.
# 5. If an error occurs during execution, the name of the failed journal is recorded in a state file to resume from that point on the next run.
# 6. If all journals are processed successfully, 'SUCCESS' is written to the state file to start from the beginning on the next run.
# 7. Generates an email body file containing the filtered and removed results.
# 8. Creates an index.html page for the filtered RSS feeds and individual .xml files.
# 9. **(Added)** Automatically includes the current GitHub Action run link at the bottom of the email.
# 10. **(Added)** Separates the email body content by journal.
# 11. **(Added)** Adds emoticons to the email body to distinguish between keyword-based and Gemini-based filtering.
# 12. **(Added)** Adds a button to index.html to navigate to the filtered results page.
# 13. **(Added)** In the email body, it specifies whether a removed paper was filtered by keyword or Gemini.
# 14. **(Added)** Pressing the 'View Filtered Results' button opens an HTML page identical to the email body format, with clickable links.
# 15. **(Added)** Displays the last update time on index.html in both Texas and Korea time.
# 16. **(Added)** Applies separate keyword and Gemini filter rules for arXiv and PRB journals.
# 17. **(Added)** Highlights the specific keyword that triggered the filtering in the console log.
# 18. **(Added)** Prints the specific keyword and its location (title or abstract) that triggered the filtering.
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

# Define ANSI color codes for console output
COLOR_GREEN = '\033[92m'
COLOR_RED = '\033[91m'
COLOR_YELLOW = '\033[93m'
COLOR_ORANGE = '\033[38;5;208m'
COLOR_BLUE = '\033[94m'
COLOR_BOLD = '\033[1m'
COLOR_END = '\033[0m'

# General journal filter criteria
GENERAL_WHITELIST = ["condensed matter", "solid state", "ARPES", "photoemission", "band structure", "Fermi surface", "Brillouin zone", "spin-orbit", "quantum oscillation", "quantum Hall", "Landau level", "topological", "topology", "Weyl", "Dirac", "Chern", "Berry phase", "Kondo", "Mott", "Hubbard", "Heisenberg model", "spin liquid", "spin ice", "skyrmion", "nematic", "stripe order", "charge density wave", "CDW", "spin density wave", "SDW", "magnetism", "magnetic order", "antiferromagnetic", "ferromagnetic", "superconductivity", "superconductor", "Meissner", "quasiparticle", "phonon", "magnon", "exciton", "polariton", "crystal field", "lattice", "moiré", "twisted bilayer", "graphene", "2D material", "van der Waals", "correlated electrons", "quantum critical", "metal-insulator", "quantum phase transition", "susceptibility", "neutron scattering", "x-ray diffraction", "STM", "STS", "Kagome", "photon"]
GENERAL_BLACKLIST = ["congress", "forest", "climate", "lava", "protein", "archeologist", "mummy", "cancer", "tumor", "immune", "immunology", "inflammation", "antibody", "cytokine", "gene", "tissue", "genome", "genetic", "transcriptome", "rna", "mrna", "mirna", "crisper", "mutation", "cell", "mouse", "zebrafish", "neuron", "neural", "brain", "synapse", "microbiome", "gut", "pathogen", "bacteria", "virus", "viral", "infection", "epidemiology", "clinical", "therapy", "therapeutic", "disease", "patient", "biopsy", "in vivo", "in vitro", "drug", "pharmacology", "oncology"]

# arXiv and PRB journal filter criteria (for stricter filtering)
ARXIV_PRB_WHITELIST = ["ARPES", "angle-resolved", "Berry phase", "Kondo", "Mott", "Hubbard", "moiré", "twisted", "graphene", "Kagome", "CsV3Sb5", "V3Sb5", "Ti3Sb5", "magneto", "Luttinger", "NbSe3", "TaSe3", "Spin-charge", "Spin charge separation", "altermagnet", "CD-ARPES", "Circular dichroic", "Circular dichroism", "Quantum geometry", "Quantum geometric"]
ARXIV_PRB_BLACKLIST = ["cancer"]

# Set multiple journal URLs
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
        print(f"Gemini API configured successfully using primary model: {primary_model}{COLOR_END}", file=sys.stderr)
    else:
        print("GOOGLE_API_KEY not found. Gemini filter will be skipped.", file=sys.stderr)
except Exception as e:
    print(f"Error configuring Gemini API: {e}", file=sys.stderr)
    using_primary_model = False


def find_and_highlight_keyword(title, summary, keywords, color_code):
    """
    Finds the first matching keyword in the title or summary and highlights it with ANSI codes.
    Returns the modified title string, the matched keyword, and the location of the match.
    """
    # Search in the title first
    for keyword in keywords:
        if re.search(r'\b' + re.escape(keyword) + r'\b', title, re.IGNORECASE):
            highlighted_title = re.sub(
                r'\b' + re.escape(keyword) + r'\b',
                f"{color_code}{COLOR_BOLD}{keyword}{COLOR_END}",
                title,
                flags=re.IGNORECASE,
                count=1
            )
            return highlighted_title, keyword, "Title"
    
    # If not found in the title, search in the summary
    for keyword in keywords:
        if re.search(r'\b' + re.escape(keyword) + r'\b', summary, re.IGNORECASE):
            return title, keyword, "Abstract"
        
    return title, None, None


def filter_rss_for_journal(journal_name, feed_url):
    """
    Filters the content of a given RSS feed URL and returns the results.
    Applies different filtering rules based on the journal.
    """
    global current_model, using_primary_model
    target_url = feed_url.strip('<> ')
    print(f"{COLOR_GREEN}Processing journal: {journal_name}, URL: {target_url}{COLOR_END}", file=sys.stderr)

    response = requests.get(target_url)
    raw_xml = response.content
    parsed_feed = feedparser.parse(raw_xml)
    
    # Dynamically set filtering criteria based on the journal.
    if journal_name in ["PRL_Recent", "PRB_Recent", "arXiv_CondMat"]:
        whitelist = ARXIV_PRB_WHITELIST
        blacklist = ARXIV_PRB_BLACKLIST
        gemini_prompt = """
        I have a list of scientific articles. For each article, please classify if it is a research paper in condensed matter physics.
        Unconditionally include articles if they are directly related to Kagome, Luttinger liquid, or experimental techniques such as ARPES, neutron scattering, or x-ray scattering. Include theoretical articles only if they are related to me, a postdoc at ARPES lab studying Kagome and topological materials.
        You MUST provide the output as a JSON array of objects. Do not include any text, conversation, or explanations before or after the JSON array.
        Each object in the JSON array should have a "title" and a "decision" key. The decision should be "YES" if it meets the criteria, or "NO" if it does not.
        Here is the list of articles:
        """
    else:
        whitelist = GENERAL_WHITELIST
        blacklist = GENERAL_BLACKLIST
        gemini_prompt = """
        I have a list of scientific articles. For each article, please classify if it is related to "condensed matter physics".
        You MUST provide the output as a JSON array of objects. Do not include any text, conversation, or explanations before or after the JSON array.
        Each object in the JSON array should have a "title" and a "decision" key. The decision should be "YES" if it is related to the specified fields, or "NO" if it is not.
        Here is the list of articles:
        """

    gemini_pending_entries = []
    
    keyword_passed_entries = []
    gemini_passed_entries = []
    keyword_removed_entries = []
    gemini_removed_entries = []

    # Iterate through all RSS feed items and perform the primary filtering.
    for entry in parsed_feed.entries:
        title = entry.get('title', '')
        summary = entry.get('summary', '')

        # Check for blacklist keywords first
        highlighted_title, matched_keyword, location = find_and_highlight_keyword(title, summary, blacklist, COLOR_RED)
        if matched_keyword:
            keyword_removed_entries.append(entry)
            print(f"  ❌ {highlighted_title} (Filtered by keyword: '{matched_keyword}' in {location})", file=sys.stderr)
            continue # Skip to the next entry

        # Check for whitelist keywords
        highlighted_title, matched_keyword, location = find_and_highlight_keyword(title, summary, whitelist, COLOR_GREEN)
        if matched_keyword:
            keyword_passed_entries.append(entry)
            print(f"  ✅ {highlighted_title} (Filtered by keyword: '{matched_keyword}' in {location})", file=sys.stderr)
            continue # Skip to the next entry

        # If neither, classify for secondary filtering by the Gemini API.
        gemini_pending_entries.append(entry)

    # Use the Gemini API to review the items that didn't pass the primary filter.
    if current_model and gemini_pending_entries:
        print(f"🤖 {COLOR_GREEN}Batch processing{COLOR_END} {len(gemini_pending_entries)} items from {journal_name} with Gemini...", file=sys.stderr)
        
        items_to_review = []
        for entry in gemini_pending_entries:
            items_to_review.append({
                "title": entry.get('title', ''),
                "summary": entry.get('summary', '')
            })

        full_prompt = gemini_prompt + json.dumps(items_to_review, indent=2)
        
        max_attempts = 3
        api_success = False
        attempt = 0
        # Attempt Gemini API call up to 3 times, switching to the fallback model on quota errors.
        while attempt < max_attempts and not api_success:
            try:
                print(f"🤖 Attempt {attempt+1}/{max_attempts} using model: {current_model.model_name}{COLOR_END}", file=sys.stderr)

                response = current_model.generate_content(
                    full_prompt,
                    generation_config=genai.types.GenerationConfig(
                        response_mime_type="application/json"
                    )
                )
                gemini_decisions = json.loads(response.text)
                
                if not isinstance(gemini_decisions, list):
                    raise TypeError("Gemini response is not a list.")
                
                # Add each paper to the passed or removed list based on the Gemini API's response.
                for decision_item in gemini_decisions:
                    if not isinstance(decision_item, dict):
                        raise TypeError("Gemini response list contains non-dictionary items.")
                    
                    title = decision_item.get('title', '')
                    decision = decision_item.get('decision', '').upper()
                    
                    original_entry = next((e for e in gemini_pending_entries if e.get('title', '') == title), None)
                    if original_entry:
                        if decision == 'YES':
                            gemini_passed_entries.append(original_entry)
                            print(f"  🤖✅ {title}", file=sys.stderr)
                        else:
                            gemini_removed_entries.append(original_entry)
                            print(f"  🤖❌ {title}", file=sys.stderr)
                api_success = True
            except Exception as e:
                error_type = type(e).__name__
                print(f"🤖 {COLOR_RED}Gemini Batch Error{COLOR_END} for {journal_name} ({error_type}, Attempt {attempt+1}/{max_attempts}): {e}", file=sys.stderr)
                
                if isinstance(e, exceptions.ResourceExhausted) and using_primary_model:
                    print(f"🚨 {COLOR_ORANGE}Quota exceeded. Switching to fallback model: {fallback_model}{COLOR_END}", file=sys.stderr)
                    try:
                        current_model = genai.GenerativeModel(fallback_model)
                        using_primary_model = False
                        # Increase retry attempts when switching to the fallback model.
                        max_attempts += 1
                    except Exception as fallback_e:
                        print(f"Error switching to fallback model: {fallback_e}", file=sys.stderr)
                        current_model = None
                
                attempt += 1
                if not api_success and attempt < max_attempts:
                    print("Retrying in 60 seconds...", file=sys.stderr)
                    time.sleep(60)
        
        if not api_success:
            print(f"🤖 Final Gemini batch API call for {journal_name} failed. All pending items will be removed.", file=sys.stderr)
            gemini_removed_entries.extend(gemini_pending_entries)
            raise RuntimeError(f"Gemini API call failed for journal: {journal_name}")
            
    print(f"Total keyword-passed links for {journal_name}: {len(keyword_passed_entries)}{COLOR_END}", file=sys.stderr)
    print(f"Total Gemini-passed links for {journal_name}: {len(gemini_passed_entries)}{COLOR_END}", file=sys.stderr)
    print(f"Total keyword-removed links for {journal_name}: {len(keyword_removed_entries)}{COLOR_END}", file=sys.stderr)
    print(f"Total Gemini-removed links for {journal_name}: {len(gemini_removed_entries)}{COLOR_END}", file=sys.stderr)
            
    # Gather all passed paper links for XML parsing and filtering.
    passed_links = set(entry.link for entry in keyword_passed_entries + gemini_passed_entries)

    root = ET.fromstring(raw_xml)
    namespaces = {
        'atom': 'http://www.w3.org/2005/Atom',
        'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
        'rss1': 'http://purl.org/rss/1.0/',
        'dc': 'http://purl.org/dc/elements/1.1/',
        'content': 'http://purl.org/rss/1.0/modules/content/'
    }

    # Iterate through the XML items based on feed type and keep only the filtered papers.
    if root.tag == 'rss':
        channel = root.find('channel')
        if channel is not None:
            for item in list(channel.findall('item')):
                link_el = item.find('link')
                if link_el is not None and link_el.text not in passed_links:
                    channel.remove(item)
    elif root.tag == '{http://www.w3.org/2005/Atom}feed':
        for entry in list(root.findall('atom:entry', namespaces=namespaces)):
            link_el = entry.find('atom:link', namespaces=namespaces)
            if link_el is not None:
                link_href = link_el.get('href')
                if link_href is not None and link_href not in passed_links:
                    root.remove(entry)
    elif root.tag == '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF':
        for item in list(root.findall('rss1:item', namespaces=namespaces)):
            item_link = item.get(f"{{{namespaces['rdf']}}}about")
            if item_link is not None and item_link not in passed_links:
                root.remove(item)

        for channel in root.findall('rss1:channel', namespaces=namespaces):
            items = channel.find('rss1:items', namespaces=namespaces)
            if items is not None:
                rdf_seq = items.find('rdf:Seq', namespaces=namespaces)
                if rdf_seq is not None:
                    # Use `passed_links` to remove list items.
                    for li in list(rdf_seq.findall('rdf:li', namespaces=namespaces)):
                        link_resource = li.get(f"{{{namespaces['rdf']}}}resource")
                        if link_resource not in passed_links:
                            rdf_seq.remove(li)
    else:
        print(f"Warning: Unknown feed type for {journal_name}: {root.tag}", file=sys.stderr)

    buffer = BytesIO()
    tree = ET.ElementTree(root)
    tree.write(buffer, encoding='utf-8', xml_declaration=True, pretty_print=True)
    return buffer.getvalue(), keyword_passed_entries, gemini_passed_entries, keyword_removed_entries, gemini_removed_entries

def create_email_body_file(email_body_content):
    """
    Function to create the email body file.
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
    Creates an HTML file with clickable links in the same format as the email body.
    """
    print("--- Generating HTML results page: filtered_results.html ---", file=sys.stderr)

    # Start of the HTML template
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
    
    # Convert email body content to HTML format
    email_lines = email_body_content.strip().split('\n')
    for line in email_lines:
        line = line.strip()
        if not line:
            continue
        
        # Process journal separator
        if line.startswith("---"):
            journal_name = line.replace("---", "").strip()
            html_content += f'<h2 class="text-xl font-bold text-indigo-700 mt-6 mb-2">{journal_name}</h2>'
        # Process section titles (PASSED PAPERS:, REMOVED PAPERS:)
        elif line.endswith(":"):
            html_content += f'<p class="text-lg font-semibold text-gray-800 mt-4">{line}</p>'
        # Process paper link lines
        else:
            # Use a regular expression to extract the emoticon, title, and link
            match = re.match(r'^(.*?)\s(.+)\s\((http[s]?://.+)\)$', line)
            if match:
                emoticon = match.group(1).strip()
                title = match.group(2).strip()
                link = match.group(3).strip()

                # Convert to a clickable link
                html_content += f"""
                <div class="p-2 bg-gray-50 rounded-lg shadow-sm hover:bg-gray-100 transition duration-300">
                    <p class="text-gray-700 text-sm font-medium whitespace-nowrap overflow-hidden overflow-ellipsis">
                        {emoticon} <a href="{link}" target="_blank" class="text-blue-600 hover:text-blue-800 hover:underline">{title}</a>
                    </p>
                </div>
"""
            # Other text (e.g., 'No papers found...')
            else:
                # Process GitHub Actions link
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
    
    # End of the HTML template
    html_content += """
        </div>
    </div>
</body>
</html>
"""

    try:
        with open('filtered_results.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        print("--- HTML results page successfully generated: filtered_results.html ---", file=sys.stderr)
    except Exception as e:
        print(f"Error generating HTML results page: {e}", file=sys.stderr)


def create_index_html(journal_urls, rss_base_filename):
    """
    Creates an index.html page that shows links to the filtered RSS feeds for each journal.
    """
    print("--- Generating HTML page: index.html ---", file=sys.stderr)
    
    # Get the current UTC time and convert it to Korea (KST) and Texas (CDT) time.
    # KST is UTC+9, CDT is UTC-5.
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
            These RSS feeds are filtered using keywords and AI to include only ARPES and condensed matter physics papers, primarily experimental.<br>
            Use the links below to subscribe using a feed reader like Reeder.<br>
        </p>
        <div class="space-y-4">
"""
    # Iterate through the list of journals and create an RSS feed link for each.
    for journal_name in journal_urls.keys():
        safe_journal_name = journal_name.replace(" ", "_").replace("/", "_")
        filename = f"{rss_base_filename}_{safe_journal_name}.xml"
        html_content += f"""
            <a href="{filename}" target="_blank" class="block w-full px-6 py-4 bg-indigo-600 text-white font-semibold rounded-lg shadow-md hover:bg-indigo-700 transition duration-300">
                {journal_name} RSS Feed
            </a>
"""
    html_content += """
            <a href="filtered_results.html" target="_blank" class="block w-full px-6 py-4 bg-green-600 text-white font-semibold rounded-lg shadow-md hover:bg-green-700 transition duration-300">
                Passed / Filtered list
            </a>
        </div>
        <div class="mt-8 text-sm text-gray-500">
            <p>Last Updated (Korea): """ + korea_time_str + """</p>
            <p>Last Updated (Texas): """ + texas_time_str + """</p>
            <p>Updates daily at 08:00 and 19:00 CDT</p>
        </div>
        <div class="mt-8 text-center text-sm text-gray-500">
            <a href="https://yilab.rice.edu/people/" target="_blank" class="text-gray-500 hover:text-gray-700 hover:underline">
                Created by Jounghoon Hyun
            </a>
        </div>
    </div>
</body>
</html>
"""
    try:
        with open('index.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        print("--- HTML page successfully generated: index.html ---", file=sys.stderr)
    except Exception as e:
        print(f"Error generating HTML page: {e}", file=sys.stderr)


if __name__ == '__main__':
    OUTPUT_FILE_BASE = "filtered_feed"
    STATE_FILE = "last_failed_journal.txt"
    
    email_content = ""
    
    journals_to_process = list(JOURNAL_URLS.items())
    start_index = 0
    # Check the state file to resume processing from the last failed journal.
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
        # Iterate through all journals and perform filtering.
        for journal_name, feed_url in journals_to_process[start_index:]:
            try:
                filtered_xml, keyword_passed_entries, gemini_passed_entries, keyword_removed_entries, gemini_removed_entries = filter_rss_for_journal(journal_name, feed_url)
                
                output_filename = f"{OUTPUT_FILE_BASE}_{journal_name}.xml"
                with open(output_filename, 'wb') as f:
                    f.write(filtered_xml)
                print(f"Successfully wrote filtered RSS feed to {output_filename}", file=sys.stderr)

                # Add email content for each journal
                email_content += f"--- {journal_name} ---\n\n"
                
                email_content += f"PASSED PAPERS:\n"
                if not keyword_passed_entries and not gemini_passed_entries:
                    email_content += 'No papers found matching your filters.\n\n'
                else:
                    # Add a list of keyword-passed papers to the email content.
                    for entry in keyword_passed_entries:
                        email_content += f"  ✅ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    # Add a list of Gemini-passed papers to the email content.
                    for entry in gemini_passed_entries:
                        email_content += f"  🤖✅ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    email_content += "\n"
                
                email_content += f"REMOVED PAPERS:\n"
                if not keyword_removed_entries and not gemini_removed_entries:
                    email_content += 'No papers were filtered out.\n\n'
                else:
                    # Add a list of keyword-removed papers to the email content.
                    for entry in keyword_removed_entries:
                        email_content += f"  ❌ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    # Add a list of Gemini-removed papers to the email content.
                    for entry in gemini_removed_entries:
                        email_content += f"  🤖❌ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    email_content += "\n"

            except Exception as e:
                print(f"An error occurred while processing journal '{journal_name}': {e}", file=sys.stderr)
                with open(STATE_FILE, 'w') as f:
                    f.write(journal_name)
                # Compose the email content in case of an error.
                email_content += f"\n\nAn error occurred while running the filter script for '{journal_name}':\n{e}\nPlease check the workflow logs for more details.\n"
                raise # Re-raise the exception to stop script execution.

        try:
            # If all journals were processed successfully, update the state file to 'SUCCESS'.
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
        # Construct the GitHub Actions link.
        github_server_url = os.getenv("GITHUB_SERVER_URL")
        github_repository = os.getenv("GITHUB_REPOSITORY")
        github_run_id = os.getenv("GITHUB_RUN_ID")

        if github_server_url and github_repository and github_run_id:
            action_url = f"{github_server_url}/{github_repository}/actions/runs/{github_run_id}"
            email_content += f"\n\n---\n\nCheck GitHub Actions run for details:\n{action_url}\n"
        
        create_email_body_file(email_content)
