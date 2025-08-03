## Filtering multiple journals at once.
## Filtering items based on whitelist & blacklist, and then batch filter remainings at once using gemini (to reduce RPM of Gemini API)

import feedparser
import lxml.etree as ET
import requests
from io import BytesIO
import sys
import os
import time
import json
import google.generativeai as genai

# ANSI 색상 코드 정의
COLOR_GREEN = '\033[92m'
COLOR_RED = '\033[91m'
COLOR_YELLOW = '\033[93m'
COLOR_ORANGE = '\033[38;5;208m'
COLOR_BLUE = '\033[94m'
COLOR_END = '\033[0m'

# ✅ 설정: 필터 기준 (여기만 수정하면 됨)
WHITELIST = ["condensed matter", "solid state", "ARPES", "photoemission", "band structure", "Fermi surface", "Brillouin zone", "spin-orbit", "quantum oscillation", "quantum Hall", "Landau level", "topological", "topology", "Weyl", "Dirac", "Chern", "Berry phase", "Kondo", "Mott", "Hubbard", "Heisenberg model", "spin liquid", "spin ice", "skyrmion", "nematic", "stripe order", "charge density wave", "CDW", "spin density wave", "SDW", "magnetism", "magnetic order", "antiferromagnetic", "ferromagnetic", "superconductivity", "superconductor", "Meissner", "quasiparticle", "phonon", "magnon", "exciton", "polariton", "crystal field", "lattice", "moiré", "twisted bilayer", "graphene", "2D material", "van der Waals", "correlated electrons", "quantum critical", "metal-insulator", "quantum phase transition", "susceptibility", "neutron scattering", "x-ray diffraction", "STM", "STS", "Kagome", "photon"]
BLACKLIST = ["archeologist","mummy","cancer", "tumor", "immune", "immunology", "inflammation", "antibody", "cytokine", "gene","tissue, "genome", "genetic", "transcriptome", "rna", "mrna", "mirna", "crisper", "mutation", "cell", "mouse", "zebrafish", "neuron", "neural", "brain", "synapse", "microbiome", "gut", "pathogen", "bacteria", "virus", "viral", "infection", "epidemiology", "clinical", "therapy", "therapeutic", "disease", "patient", "biopsy", "in vivo", "in vitro", "drug", "pharmacology", "oncology"]

# ✅ 여러 저널 URL 설정
JOURNAL_URLS = {
    "Nature": "https://www.nature.com/nature.rss",
    "Nature_Physics": "https://feeds.nature.com/nphys/rss/current",
    "Nature_Materials": "https://feeds.nature.com/nmat/rss/current",
    "Nature_Communications": "https://www.nature.com/subjects/publishing/ncomms.rss",
    "npj_QuantumMaterials": "https://www.nature.com/npjquantmats.rss",
    "Science": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
    "Science_Advances": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv"
}

# ✅ Gemini 모델 초기화
primary_model = 'gemini-1.5-flash-latest'
fallback_model = 'gemini-1.0-pro'
current_model = None
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

def filter_rss_for_journal(journal_name, feed_url):
    """
    주어진 RSS 피드 URL의 내용을 필터링하고 결과를 반환합니다.
    """
    global current_model
    target_url = feed_url.strip('<> ')
    print(f"{COLOR_GREEN}Processing journal: {journal_name}, URL: {target_url}{COLOR_END}", file=sys.stderr)

    response = requests.get(target_url)
    raw_xml = response.content
    parsed_feed = feedparser.parse(raw_xml)
    
    gemini_pending_entries = []
    passed_links = set()
    removed_links = set()
    passed_entries_for_email = []
    removed_entries_for_email = []

    for entry in parsed_feed.entries:
        title = entry.get('title', '').lower()
        summary = entry.get('summary', '').lower()
        content = f"{title} {summary}"

        is_in_blacklist = any(b.lower() in content for b in BLACKLIST)
        is_in_whitelist = any(w.lower() in content for w in WHITELIST)

        if is_in_blacklist: # blacklist 먼저.
            removed_links.add(entry.link)
            removed_entries_for_email.append(entry)
            print(f"❌ {title}", file=sys.stderr)
        elif is_in_whitelist:
            passed_links.add(entry.link)
            passed_entries_for_email.append(entry)
            print(f"✅ {title}", file=sys.stderr)
        else:
            gemini_pending_entries.append(entry)

    if current_model and gemini_pending_entries:
        print(f"🤖 {COLOR_GREEN}Batch processing{COLOR_END} {len(gemini_pending_entries)} items from {journal_name} with Gemini...", file=sys.stderr)
        
        items_to_review = []
        for entry in gemini_pending_entries:
            items_to_review.append({
                "title": entry.get('title', ''),
                "summary": entry.get('summary', '')
            })

        # ✅ 수정된 프롬프트: JSON 형식 응답을 더 명확하게 지시
        prompt = f"""
        I have a list of scientific articles. For each article, please classify if it is related to "condensed matter physics".
        You MUST provide the output as a JSON array of objects. Do not include any text, conversation, or explanations before or after the JSON array.
        Each object in the JSON array should have a "title" and a "decision" key. The decision should be "YES" if it is related to the specified fields, or "NO" if it is not.
        Here is the list of articles:
        {json.dumps(items_to_review, indent=2)}
        """
            
        retries = 3
        api_success = False
        for i in range(retries):
            try:
                response = current_model.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(
                        response_mime_type="application/json"
                    )
                )
                gemini_decisions = json.loads(response.text)
                
                # ✅ 수정된 부분: 응답에 문제가 있으면 예외를 발생시켜 API 재시도
                if not isinstance(gemini_decisions, list):
                    raise TypeError("Gemini response is not a list.")
                
                for decision_item in gemini_decisions:
                    if not isinstance(decision_item, dict):
                         raise TypeError("Gemini response list contains non-dictionary items.")
                    
                    title = decision_item.get('title', '')
                    decision = decision_item.get('decision', '').upper()
                    
                    original_entry = next((e for e in gemini_pending_entries if e.get('title', '') == title), None)
                    if original_entry:
                        if decision == 'YES':
                            passed_links.add(original_entry.link)
                            passed_entries_for_email.append(original_entry)
                            print(f"🤖✅ {title}", file=sys.stderr)
                        else:
                            removed_links.add(original_entry.link)
                            removed_entries_for_email.append(original_entry)
                            print(f"🤖❌ {title}", file=sys.stderr)
                api_success = True
                break
            except Exception as e:
                error_message = str(e)
                print(f"🤖 {COLOR_RED}Gemini Batch Error{COLOR_END} for {journal_name} (Attempt {i+1}/{retries}): {error_message}", file=sys.stderr)
                
                # 429 에러 발생 시 fallback 모델로 전환
                if "429" in error_message and current_model.model_name == primary_model:
                    print(f"🚨 {COLOR_ORANGE}Quota exceeded. Switching to fallback model: {fallback_model}{COLOR_END}", file=sys.stderr)
                    try:
                        current_model = genai.GenerativeModel(fallback_model)
                        retries += 1 # fallback 시도 횟수 추가
                    except Exception as fallback_e:
                        print(f"Error switching to fallback model: {fallback_e}", file=sys.stderr)
                        current_model = None
                
                if i < retries - 1 and current_model:
                    time.sleep(5)
                else:
                    break
        
        if not api_success:
            print(f"🤖 Final Gemini batch API call for {journal_name} failed. All pending items will be removed.", file=sys.stderr)
            removed_links.update(entry.link for entry in gemini_pending_entries)
            removed_entries_for_email.extend(gemini_pending_entries)
            
    print(f"Total passed links for {journal_name}: {len(passed_links)}", file=sys.stderr)
    print(f"Total removed links for {journal_name}: {len(removed_links)}", file=sys.stderr)
            
    # XML 파싱 및 필터링
    root = ET.fromstring(raw_xml)
    namespaces = {
        'atom': 'http://www.w3.org/2005/Atom',
        'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
        'rss1': 'http://purl.org/rss/1.0/',
        'dc': 'http://purl.org/dc/elements/1.1/',
        'content': 'http://purl.org/rss/1.0/modules/content/'
    }

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
                    for li in list(rdf_seq.findall('rdf:li', namespaces=namespaces)):
                        link_resource = li.get(f"{{{namespaces['rdf']}}}resource")
                        if link_resource in removed_links:
                            rdf_seq.remove(li)
    else:
        print(f"Warning: Unknown feed type for {journal_name}: {root.tag}", file=sys.stderr)

    buffer = BytesIO()
    tree = ET.ElementTree(root)
    tree.write(buffer, encoding='utf-8', xml_declaration=True, pretty_print=True)
    return buffer.getvalue(), passed_entries_for_email, removed_entries_for_email

def create_email_body_file(email_body_content):
    """
    이메일 본문 파일을 생성하는 함수
    """
    EMAIL_BODY_FILE = "filtered_titles.txt"
    try:
        with open(EMAIL_BODY_FILE, 'w', encoding='utf-8') as f:
            f.write(email_body_content)
        print(f"Successfully created {EMAIL_BODY_FILE} for email.", file=sys.stderr)
    except Exception as e:
        print(f"Error creating email body file: {e}", file=sys.stderr)

if __name__ == '__main__':
    OUTPUT_FILE_BASE = "filtered_feed"
    
    all_passed_entries = []
    all_removed_entries = []

    try:
        for journal_name, feed_url in JOURNAL_URLS.items():
            # --- 필터링 로직 실행 ---
            filtered_xml, passed_entries, removed_entries = filter_rss_for_journal(journal_name, feed_url)
            
            # 필터링된 XML 파일 쓰기
            output_filename = f"{OUTPUT_FILE_BASE}_{journal_name}.xml"
            with open(output_filename, 'wb') as f:
                f.write(filtered_xml)
            print(f"Successfully wrote filtered RSS feed to {output_filename}", file=sys.stderr)

            # 이메일 리스트에 추가
            all_passed_entries.extend(passed_entries)
            all_removed_entries.extend(removed_entries)

        # 모든 저널의 결과를 모아 하나의 이메일 본문 생성
        email_content = ""
        email_content += "--- PASSED PAPERS ---\n\n"
        if not all_passed_entries:
            email_content += 'No new papers found matching your filters.\n\n'
        else:
            for entry in all_passed_entries:
                email_content += f"‣ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
        
        email_content += "\n\n--- REMOVED PAPERS ---\n\n"
        if not all_removed_entries:
            email_content += 'No papers were filtered out.\n'
        else:
            for entry in all_removed_entries:
                email_content += f"‣ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"

    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
        email_content = f"An error occurred while running the filter script:\n{e}\nPlease check the workflow logs for more details."

    finally:
        # 오류 여부에 관계없이, 이메일 내용 파일을 항상 생성합니다.
        create_email_body_file(email_content)
