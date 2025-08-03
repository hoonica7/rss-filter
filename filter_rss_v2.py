import feedparser
import lxml.etree as ET
import requests
from io import BytesIO
import sys
import os
import time
import json
import google.generativeai as genai

COLOR_GREEN = '\033[92m'
COLOR_RED = '\033[91m'
COLOR_YELLOW = '\033[93m'
COLOR_ORANGE = '\033[38;5;208m'
COLOR_BLUE = '\033[94m'
COLOR_END = '\033[0m'

# ✅ 설정: 필터 기준 (여기만 수정하면 됨)
WHITELIST = ["condensed matter", "solid state", "ARPES", "photoemission", "band structure", "Fermi surface", "Brillouin zone", "spin-orbit", "quantum oscillation", "quantum Hall", "Landau level", "topological", "topology", "Weyl", "Dirac", "Chern", "Berry phase", "Kondo", "Mott", "Hubbard", "Heisenberg model", "Ising", "spin liquid", "spin ice", "skyrmion", "nematic", "stripe order", "charge density wave", "CDW", "spin density wave", "SDW", "magnetism", "magnetic order", "antiferromagnetic", "ferromagnetic", "superconductivity", "superconductor", "Meissner", "vortex", "quasiparticle", "phonon", "magnon", "exciton", "polariton", "crystal field", "lattice", "strain", "valley", "moiré", "twisted bilayer", "graphene", "2D material", "van der Waals", "thin film", "interface", "correlated electrons", "quantum critical", "metal-insulator", "quantum phase transition", "resistivity", "transport", "susceptibility", "neutron scattering", "x-ray diffraction", "STM", "STS", "Kagome"]
BLACKLIST = ["cancer", "tumor", "immune", "immunology", "inflammation", "antibody", "cytokine", "gene expression", "genome", "genetic", "transcriptome", "rna", "mrna", "mirna", "crisper", "mutation", "cell line", "mouse model", "zebrafish", "neuron", "neural", "brain", "synapse", "microbiome", "gut", "pathogen", "bacteria", "virus", "viral", "infection", "epidemiology", "clinical", "therapy", "therapeutic", "disease", "patient", "biopsy", "in vivo", "in vitro", "drug", "pharmacology", "oncology"]

# ✅ Gemini 모델 초기화
try:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.5-flash-latest')
        print("Gemini API configured successfully using gemini-2.5-flash-latest.", file=sys.stderr)
    else:
        print("GOOGLE_API_KEY not found. Gemini filter will be skipped.", file=sys.stderr)
        gemini_model = None
except Exception as e:
    print(f"Error configuring Gemini API: {e}", file=sys.stderr)
    gemini_model = None

def filter_rss(feed_url):
    """
    주어진 RSS 피드 URL의 내용을 필터링하여 수정된 XML을 반환합니다.
    """
    target_url = feed_url.strip('<> ')
    print(f"Target URL: {target_url}", file=sys.stderr)

    try:
        response = requests.get(target_url)
        raw_xml = response.content
        parsed_feed = feedparser.parse(raw_xml)
        
        # 1차 필터링: 키워드 기반으로 항목을 분류
        gemini_pending_entries = []
        passed_links = set()
        removed_links = set()

        for entry in parsed_feed.entries:
            title = entry.get('title', '').lower()
            summary = entry.get('summary', '').lower()
            content = f"{title} {summary}"

            is_in_whitelist = any(w.lower() in content for w in WHITELIST)
            is_in_blacklist = any(b.lower() in content for b in BLACKLIST)

            if is_in_whitelist:
                passed_links.add(entry.link)
                print(f"✅ {COLOR_GREEN}Keyword passed{COLOR_END}: {title}", file=sys.stderr)
            elif is_in_blacklist:
                removed_links.add(entry.link)
                print(f"❌ {COLOR_RED}Keyword filtered{COLOR_END}: {title}", file=sys.stderr)
            else:
                gemini_pending_entries.append(entry)

        # 2차 필터링: Gemini API로 남은 항목을 배치 처리
        if gemini_model and gemini_pending_entries:
            print(f"🤖 Batch processing {len(gemini_pending_entries)} items with Gemini...", file=sys.stderr)
            
            items_to_review = []
            for entry in gemini_pending_entries:
                items_to_review.append({
                    "title": entry.get('title', ''),
                    "summary": entry.get('summary', '')
                })

            prompt = f"""
I have a list of scientific articles. For each article, please classify if it is related to "condensed matter physics" or "research ethics/researcher life".

Provide the output as a JSON array of objects. Each object should have a "title" and a "decision" key. The decision should be "YES" if it is related to the specified fields, or "NO" if it is not.

Here is the list of articles:
{json.dumps(items_to_review, indent=2)}
"""
            retries = 3
            api_success = False
            for i in range(retries):
                try:
                    response = gemini_model.generate_content(
                        prompt,
                        generation_config=genai.types.GenerationConfig(
                            response_mime_type="application/json"
                        )
                    )
                    gemini_decisions = json.loads(response.text)
                    for decision_item in gemini_decisions:
                        title = decision_item.get('title', '')
                        decision = decision_item.get('decision', '').upper()
                        
                        original_entry = next((e for e in gemini_pending_entries if e.get('title', '') == title), None)
                        if original_entry:
                            if decision == 'YES':
                                passed_links.add(original_entry.link)
                                print(f"🤖✅ {COLOR_GREEN}Gemini passed{COLOR_END} : {title}", file=sys.stderr)
                            else:
                                removed_links.add(original_entry.link)
                                print(f"🤖❌ {COLOR_RED}Gemini filtered{COLOR_END} : {title}", file=sys.stderr)
                    api_success = True
                    break
                except Exception as e:
                    print(f"🤖 Gemini Batch Error (Attempt {COLOR_RED}{i+1}/{retries}{COLOR_END}): {e}", file=sys.stderr)
                    if i < retries - 1:
                        time.sleep(5)
            
            if not api_success:
                print("🤖 Final Gemini batch API call failed. All pending items will be removed.", file=sys.stderr)
                removed_links.update(entry.link for entry in gemini_pending_entries)
        
        print(f"Total passed links: {len(passed_links)}", file=sys.stderr)
        print(f"Total removed links: {len(removed_links)}", file=sys.stderr)

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
            print(f"Warning: Unknown feed type: {root.tag}", file=sys.stderr)

        buffer = BytesIO()
        tree = ET.ElementTree(root)
        tree.write(buffer, encoding='utf-8', xml_declaration=True, pretty_print=True)
        return buffer.getvalue()

    except Exception as e:
        print(f"Error in filter_rss: {e}", file=sys.stderr)
        raise

if __name__ == '__main__':
    FEED_URL = "https://feeds.nature.com/nphys/rss/current"
    OUTPUT_FILE = "filtered_feed.xml"

    try:
        filtered_xml = filter_rss(FEED_URL)
        with open(OUTPUT_FILE, 'wb') as f:
            f.write(filtered_xml)
        print(f"Successfully wrote filtered RSS feed to {OUTPUT_FILE}", file=sys.stderr)
    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
