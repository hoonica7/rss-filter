#
# 이 스크립트는 여러 과학 저널의 RSS 피드를 필터링하여,
# 특정 키워드에 맞는 논문만 골라내고 Gemini API를 사용하여 추가 검증을 수행합니다.
#
# 주요 기능:
# 1. 여러 저널 RSS 피드 일괄 처리.
# 2. WHITELIST 및 BLACKLIST 키워드를 사용한 1차 필터링.
# 3. 1차 필터링에 걸리지 않은 항목을 Gemini API를 통해 2차 필터링 (배치 처리로 API 호출 최소화).
# 4. Gemini API 할당량 오류 발생 시, 백업 모델로 자동 전환 후 재시도.
# 5. 실행 중 오류가 발생한 경우, 오류가 발생한 저널 이름을 상태 파일에 기록하여 다음 실행 시 해당 지점부터 다시 시작.
# 6. 모든 저널을 성공적으로 처리한 경우, 상태 파일에 'SUCCESS'를 기록하여 다음 실행 시 처음부터 시작.
# 7. 필터링된 결과와 제거된 결과를 담은 이메일 본문 파일 생성.
# 8. 필터링된 RSS 피드를 위한 index.html 페이지와 개별 .xml 파일 생성.
# 9. **(추가됨)** 이메일 최하단에 현재 GitHub Action 실행 링크를 자동으로 추가합니다.
# 10. **(추가됨)** 이메일 본문의 내용을 저널별로 구분하여 표시합니다.
# 11. **(추가됨)** 이메일 본문에 필터링 방식(키워드 또는 Gemini)에 따른 이모티콘을 추가합니다.
# 12. **(추가됨)** index.html에 필터링 결과 페이지로 이동하는 버튼을 추가합니다.
# 13. **(추가됨)** 이메일 본문에서 제거된 논문의 필터링 방식(키워드 또는 Gemini)을 구분하여 표시합니다.
# 14. **(추가됨)** 'Filter 결과' 버튼을 누르면 이메일 본문 형식 그대로 개별 논문 링크를 클릭할 수 있는 HTML 페이지가 열립니다.
# 15. **(추가됨)** index.html의 마지막 업데이트 시간을 텍사스 시간과 한국 시간으로 나누어 표시합니다.
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

# ANSI 색상 코드 정의
COLOR_GREEN = '\033[92m'
COLOR_RED = '\033[91m'
COLOR_YELLOW = '\033[93m'
COLOR_ORANGE = '\033[38;5;208m'
COLOR_BLUE = '\033[94m'
COLOR_END = '\033[0m'

# 필터 기준 설정 (여기만 수정하면 됨)
WHITELIST = ["condensed matter", "solid state", "ARPES", "photoemission", "band structure", "Fermi surface", "Brillouin zone", "spin-orbit", "quantum oscillation", "quantum Hall", "Landau level", "topological", "topology", "Weyl", "Dirac", "Chern", "Berry phase", "Kondo", "Mott", "Hubbard", "Heisenberg model", "spin liquid", "spin ice", "skyrmion", "nematic", "stripe order", "charge density wave", "CDW", "spin density wave", "SDW", "magnetism", "magnetic order", "antiferromagnetic", "ferromagnetic", "superconductivity", "superconductor", "Meissner", "quasiparticle", "phonon", "magnon", "exciton", "polariton", "crystal field", "lattice", "moiré", "twisted bilayer", "graphene", "2D material", "van der Waals", "correlated electrons", "quantum critical", "metal-insulator", "quantum phase transition", "susceptibility", "neutron scattering", "x-ray diffraction", "STM", "STS", "Kagome", "photon"]
BLACKLIST = ["congress", "forest", "climate", "lava", "protein", "archeologist", "mummy", "cancer", "tumor", "immune", "immunology", "inflammation", "antibody", "cytokine", "gene", "tissue", "genome", "genetic", "transcriptome", "rna", "mrna", "mirna", "crisper", "mutation", "cell", "mouse", "zebrafish", "neuron", "neural", "brain", "synapse", "microbiome", "gut", "pathogen", "bacteria", "virus", "viral", "infection", "epidemiology", "clinical", "therapy", "therapeutic", "disease", "patient", "biopsy", "in vivo", "in vitro", "drug", "pharmacology", "oncology"]

# 여러 저널 URL 설정
JOURNAL_URLS = {
    "Nature": "https://www.nature.com/nature.rss",
    "Nature_Physics": "https://feeds.nature.com/nphys/rss/current",
    "Nature_Materials": "https://feeds.nature.com/nmat/rss/current",
    "Nature_Communications": "https://www.nature.com/ncomms.rss",
    "npj_QuantumMaterials": "https://www.nature.com/npjquantmats.rss",
    "Science": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
    "Science_Advances": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv"
}

# Gemini 모델 초기화
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
    주어진 RSS 피드 URL의 내용을 필터링하고 결과를 반환합니다.
    """
    global current_model, using_primary_model
    target_url = feed_url.strip('<> ')
    print(f"{COLOR_GREEN}Processing journal: {journal_name}, URL: {target_url}{COLOR_END}", file=sys.stderr)

    response = requests.get(target_url)
    raw_xml = response.content
    parsed_feed = feedparser.parse(raw_xml)
    
    gemini_pending_entries = []
    
    keyword_passed_entries = []
    gemini_passed_entries = []
    keyword_removed_entries = []
    gemini_removed_entries = []

    # 모든 RSS 피드 항목을 순회하며 1차 필터링을 수행합니다.
    for entry in parsed_feed.entries:
        title = entry.get('title', '').lower()
        summary = entry.get('summary', '').lower()
        content = f"{title} {summary}"

        is_in_blacklist = any(b.lower() in content for b in BLACKLIST)
        is_in_whitelist = any(w.lower() in content for w in WHITELIST)

        # 블랙리스트에 있으면 제거하고, 화이트리스트에 있으면 통과시킵니다.
        # 둘 다 아니면 Gemini API를 통한 2차 필터링 대상으로 분류합니다.
        if is_in_blacklist:
            keyword_removed_entries.append(entry)
            print(f"  ❌ {title}", file=sys.stderr)
        elif is_in_whitelist:
            keyword_passed_entries.append(entry)
            print(f"  ✅ {title}", file=sys.stderr)
        else:
            gemini_pending_entries.append(entry)

    # Gemini API를 사용하여 1차 필터링에 걸리지 않은 항목들을 검토합니다.
    if current_model and gemini_pending_entries:
        print(f"🤖 {COLOR_GREEN}Batch processing{COLOR_END} {len(gemini_pending_entries)} items from {journal_name} with Gemini...", file=sys.stderr)
        
        items_to_review = []
        for entry in gemini_pending_entries:
            items_to_review.append({
                "title": entry.get('title', ''),
                "summary": entry.get('summary', '')
            })

        prompt = f"""
        I have a list of scientific articles. For each article, please classify if it is related to "condensed matter physics".
        You MUST provide the output as a JSON array of objects. Do not include any text, conversation, or explanations before or after the JSON array.
        Each object in the JSON array should have a "title" and a "decision" key. The decision should be "YES" if it is related to the specified fields, or "NO" if it is not.
        Here is the list of articles:
        {json.dumps(items_to_review, indent=2)}
        """
        
        max_attempts = 3
        api_success = False
        attempt = 0
        # Gemini API 호출을 최대 3번 시도하고, 할당량 오류 시 백업 모델로 전환합니다.
        while attempt < max_attempts and not api_success:
            try:
                print(f"🤖 Attempt {attempt+1}/{max_attempts} using model: {current_model.model_name}", file=sys.stderr)

                response = current_model.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(
                        response_mime_type="application/json"
                    )
                )
                gemini_decisions = json.loads(response.text)
                
                if not isinstance(gemini_decisions, list):
                    raise TypeError("Gemini response is not a list.")
                
                # Gemini API의 응답을 바탕으로 각 논문을 통과 또는 제거 목록에 추가합니다.
                for decision_item in gemini_decisions:
                    if not isinstance(decision_item, dict):
                        raise TypeError("Gemini response list contains non-dictionary items.")
                    
                    title = decision_item.get('title', '')
                    decision = decision_item.get('decision', '').upper()
                    
                    original_entry = next((e for e in gemini_pending_entries if e.get('title', '') == title), None)
                    if original_entry:
                        if decision == 'YES':
                            gemini_passed_entries.append(original_entry)
                            print(f"  🤖✅ {title}", file=sys.stderr)
                        else:
                            gemini_removed_entries.append(original_entry)
                            print(f"  🤖❌ {title}", file=sys.stderr)
                api_success = True
            except Exception as e:
                error_type = type(e).__name__
                print(f"🤖 {COLOR_RED}Gemini Batch Error{COLOR_END} for {journal_name} ({error_type}, Attempt {attempt+1}/{max_attempts}): {e}", file=sys.stderr)
                
                if isinstance(e, exceptions.ResourceExhausted) and using_primary_model:
                    print(f"🚨 {COLOR_ORANGE}Quota exceeded. Switching to fallback model: {fallback_model}{COLOR_END}", file=sys.stderr)
                    try:
                        current_model = genai.GenerativeModel(fallback_model)
                        using_primary_model = False
                        # 백업 모델 전환 시 재시도 횟수를 늘려줍니다.
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
            
    print(f"Total keyword-passed links for {journal_name}: {len(keyword_passed_entries)}", file=sys.stderr)
    print(f"Total Gemini-passed links for {journal_name}: {len(gemini_passed_entries)}", file=sys.stderr)
    print(f"Total keyword-removed links for {journal_name}: {len(keyword_removed_entries)}", file=sys.stderr)
    print(f"Total Gemini-removed links for {journal_name}: {len(gemini_removed_entries)}", file=sys.stderr)
            
    # XML 파싱 및 필터링을 위해 통과된 모든 논문 링크를 모읍니다.
    passed_links = set(entry.link for entry in keyword_passed_entries + gemini_passed_entries)

    root = ET.fromstring(raw_xml)
    namespaces = {
        'atom': 'http://www.w3.org/2005/Atom',
        'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
        'rss1': 'http://purl.org/rss/1.0/',
        'dc': 'http://purl.org/dc/elements/1.1/',
        'content': 'http://purl.org/rss/1.0/modules/content/'
    }

    # 피드 유형에 따라 XML 항목을 순회하며 필터링된 논문만 남깁니다.
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
                    # `removed_links` 대신 `passed_links`를 사용하여 리스트 항목을 제거합니다.
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
    이메일 본문 파일을 생성하는 함수
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
    이메일 본문과 동일한 형식으로 클릭 가능한 링크를 포함하는 HTML 파일을 생성합니다.
    """
    print("--- HTML 결과 페이지 생성 중: filtered_results.html ---", file=sys.stderr)

    # HTML 템플릿 시작 부분
    html_content = f"""
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>필터링된 논문 결과</title>
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
    <div class="max-w-4xl mx-auto bg-white rounded-xl shadow-2xl p-8">
        <h1 class="text-3xl font-bold text-gray-800 mb-6 text-center">필터링된 논문 결과</h1>
        <div class="space-y-4">
"""
    
    # 이메일 본문 내용을 HTML 형식으로 변환
    email_lines = email_body_content.strip().split('\n')
    for line in email_lines:
        line = line.strip()
        if not line:
            continue
        
        # 저널 구분자 처리
        if line.startswith("---"):
            journal_name = line.replace("---", "").strip()
            html_content += f'<h2 class="text-xl font-bold text-indigo-700 mt-6 mb-2">{journal_name}</h2>'
        # 섹션 제목 처리 (PASSED PAPERS:, REMOVED PAPERS:)
        elif line.endswith(":"):
            html_content += f'<p class="text-lg font-semibold text-gray-800 mt-4">{line}</p>'
        # 논문 링크 라인 처리
        else:
            # 정규 표현식을 사용하여 이모티콘, 제목, 링크를 추출
            match = re.match(r'^(.*?)\s(.+)\s\((http[s]?://.+)\)$', line)
            if match:
                emoticon = match.group(1).strip()
                title = match.group(2).strip()
                link = match.group(3).strip()

                # 클릭 가능한 링크로 변환
                html_content += f"""
                <div class="p-3 bg-gray-50 rounded-lg shadow-sm hover:bg-gray-100 transition duration-300">
                    <p class="text-gray-700 text-base font-medium leading-snug">
                        {emoticon} <a href="{link}" target="_blank" class="text-blue-600 hover:text-blue-800 hover:underline">{title}</a>
                    </p>
                </div>
"""
            # 기타 텍스트 (예: 'No papers found...')
            else:
                # GitHub Actions 링크 처리
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
    
    # HTML 템플릿 끝 부분
    html_content += """
        </div>
    </div>
</body>
</html>
"""

    try:
        with open('filtered_results.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        print("--- HTML 결과 페이지 생성 완료: filtered_results.html ---", file=sys.stderr)
    except Exception as e:
        print(f"HTML 결과 페이지 생성 중 오류 발생: {e}", file=sys.stderr)


def create_index_html(journal_urls, rss_base_filename):
    """
    각 저널의 필터링된 RSS 피드 링크를 보여주는 index.html 페이지를 생성합니다.
    """
    print("--- HTML 페이지 생성 중: index.html ---", file=sys.stderr)
    
    # 현재 UTC 시간을 가져와서 한국 시간(KST)과 휴스턴 시간(CDT)으로 변환합니다.
    # KST는 UTC+9, CDT는 UTC-5 입니다.
    now_utc = datetime.datetime.utcnow()
    now_korea = now_utc + datetime.timedelta(hours=9)
    now_texas = now_utc - datetime.timedelta(hours=5)

    korea_time_str = now_korea.strftime('%Y-%m-%d %H:%M:%S') + " (한국, KST)"
    texas_time_str = now_texas.strftime('%Y-%m-%d %H:%M:%S') + " (휴스턴, CDT)"

    html_content = f"""
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>필터링된 논문 RSS 피드</title>
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
        <h1 class="text-3xl font-bold text-gray-800 mb-2">필터링된 논문 RSS 피드</h1>
        <p class="text-gray-600 mb-8">
            선택한 저널들의 ARPES 및 Condensed matter physics 논문들만 필터링한 RSS 피드입니다.
            아래 링크를 클릭하여 Reeder 앱 등에서 구독하세요.
        </p>
        <div class="space-y-4">
"""
    # 저널 목록을 순회하며 각각의 RSS 피드 링크를 생성합니다.
    for journal_name in journal_urls.keys():
        safe_journal_name = journal_name.replace(" ", "_").replace("/", "_")
        filename = f"{rss_base_filename}_{safe_journal_name}.xml"
        html_content += f"""
            <a href="{filename}" target="_blank" class="block w-full px-6 py-4 bg-indigo-600 text-white font-semibold rounded-lg shadow-md hover:bg-indigo-700 transition duration-300">
                {journal_name} RSS 피드 보기
            </a>
"""
    html_content += """
            <a href="filtered_results.html" target="_blank" class="block w-full px-6 py-4 bg-green-600 text-white font-semibold rounded-lg shadow-md hover:bg-green-700 transition duration-300">
                Filter 결과
            </a>
        </div>
        <div class="mt-8 text-sm text-gray-500">
            <p>마지막 업데이트 (한국): """ + korea_time_str + """</p>
            <p>마지막 업데이트 (휴스턴): """ + texas_time_str + """</p>
        </div>
    </div>
</body>
</html>
"""
    try:
        with open('index.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        print("--- HTML 페이지 생성 완료: index.html ---", file=sys.stderr)
    except Exception as e:
        print(f"HTML 페이지 생성 중 오류 발생: {e}", file=sys.stderr)


if __name__ == '__main__':
    OUTPUT_FILE_BASE = "filtered_feed"
    STATE_FILE = "last_failed_journal.txt"
    
    email_content = ""
    
    journals_to_process = list(JOURNAL_URLS.items())
    start_index = 0
    # 상태 파일을 확인하여 마지막으로 실패한 저널부터 처리를 재개합니다.
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
        # 모든 저널을 순회하며 필터링을 수행합니다.
        for journal_name, feed_url in journals_to_process[start_index:]:
            try:
                filtered_xml, keyword_passed_entries, gemini_passed_entries, keyword_removed_entries, gemini_removed_entries = filter_rss_for_journal(journal_name, feed_url)
                
                output_filename = f"{OUTPUT_FILE_BASE}_{journal_name}.xml"
                with open(output_filename, 'wb') as f:
                    f.write(filtered_xml)
                print(f"Successfully wrote filtered RSS feed to {output_filename}", file=sys.stderr)

                # 저널별로 이메일 내용 추가
                email_content += f"--- {journal_name} ---\n\n"
                
                email_content += f"PASSED PAPERS:\n"
                if not keyword_passed_entries and not gemini_passed_entries:
                    email_content += 'No papers found matching your filters.\n\n'
                else:
                    # 키워드 기반으로 통과된 논문 목록을 이메일 내용에 추가합니다.
                    for entry in keyword_passed_entries:
                        email_content += f"  ✅ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    # Gemini 기반으로 통과된 논문 목록을 이메일 내용에 추가합니다.
                    for entry in gemini_passed_entries:
                        email_content += f"  🤖✅ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    email_content += "\n"
                
                email_content += f"REMOVED PAPERS:\n"
                if not keyword_removed_entries and not gemini_removed_entries:
                    email_content += 'No papers were filtered out.\n\n'
                else:
                    # 키워드 기반으로 제거된 논문 목록을 이메일 내용에 추가합니다.
                    for entry in keyword_removed_entries:
                        email_content += f"  ❌ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    # Gemini 기반으로 제거된 논문 목록을 이메일 내용에 추가합니다.
                    for entry in gemini_removed_entries:
                        email_content += f"  🤖❌ {entry.get('title', 'No title')} ({entry.get('link', 'No link')})\n"
                    email_content += "\n"

            except Exception as e:
                print(f"An error occurred while processing journal '{journal_name}': {e}", file=sys.stderr)
                with open(STATE_FILE, 'w') as f:
                    f.write(journal_name)
                # 에러 발생 시 이메일 내용을 구성
                email_content += f"\n\nAn error occurred while running the filter script for '{journal_name}':\n{e}\nPlease check the workflow logs for more details.\n"
                raise # 기존 예외를 다시 발생시켜 스크립트 실행을 중단합니다.

        try:
            # 모든 저널 처리가 성공적으로 완료되면 상태 파일을 'SUCCESS'로 업데이트합니다.
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
        # GitHub Actions 링크를 구성합니다.
        github_server_url = os.getenv("GITHUB_SERVER_URL")
        github_repository = os.getenv("GITHUB_REPOSITORY")
        github_run_id = os.getenv("GITHUB_RUN_ID")

        if github_server_url and github_repository and github_run_id:
            action_url = f"{github_server_url}/{github_repository}/actions/runs/{github_run_id}"
            email_content += f"\n\n---\n\nCheck GitHub Actions run for details:\n{action_url}\n"
        
        create_email_body_file(email_content)
