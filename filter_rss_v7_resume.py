# RSS filter v7
# Journal-by-journal RSS feeds + scored LLM filtering + daily briefing.
# Adds robust checkpoint/resume support via partial state files restored by GitHub Actions cache.

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
import math
import html
from urllib.parse import urljoin

COLOR_GREEN = '\033[92m'
COLOR_RED = '\033[91m'
COLOR_YELLOW = '\033[93m'
COLOR_ORANGE = '\033[38;5;208m'
COLOR_BLUE = '\033[94m'
COLOR_BOLD = '\033[1m'
COLOR_END = '\033[0m'

# RSS filter v6 policy:
# - Only a narrow title whitelist bypasses Gemini.
# - Broad condensed-matter keywords only add hints; they do not auto-pass.
# - Output is score-based, not binary-only.
NARROW_TITLE_AUTOPASS = [
    "ARPES", "angle-resolved photoemission", "photoemission", "magnetoARPES", "CD-ARPES",
    "CsV3Sb5", "RbV3Sb5", "KV3Sb5", "AV3Sb5", "V3Sb5",
    "kagome metal", "kagome superconductor", "kagome CDW",
    "NbSe3", "spin-charge separation", "Luttinger liquid",
    "TaNiTe2", "NbNiTe2", "112 telluride"
]

DIRECT_RELEVANCE_KEYWORDS = [
    "ARPES", "angle-resolved photoemission", "photoemission", "magnetoARPES", "CD-ARPES", "circular dichroism",
    "kagome", "AV3Sb5", "CsV3Sb5", "RbV3Sb5", "KV3Sb5", "V3Sb5", "charge density wave", "CDW", "nematic",
    "loop current", "time-reversal symmetry breaking", "TRSB", "Weyl", "Dirac", "Berry curvature", "anomalous Hall",
    "altermagnet", "spin-charge separation", "Luttinger", "NbSe3", "TaNiTe2", "NbNiTe2", "112 telluride"
]
BROAD_CONDMAT_KEYWORDS = [
    "superconduct", "correlated", "Mott", "Hubbard", "moiré", "twisted", "graphene", "topological", "Chern", "flat band",
    "spin liquid", "magnon", "phonon", "quantum critical", "Kondo", "van der Waals", "ferromagnet", "antiferromagnet",
    "quantum geometry", "Hall effect", "transport", "Fermi surface", "band structure", "Landau level"
]

STRONG_NEGATIVE_KEYWORDS = [
    "congress", "forest", "climate", "lava", "protein", "archeologist", "mummy", "cancer", "tumor", "immune",
    "immunology", "inflammation", "antibody", "cytokine", "genome", "genetic", "transcriptome", "rna", "mrna",
    "mirna", "crispr", "mutation", "mouse", "zebrafish", "neuron", "neural", "brain", "synapse", "microbiome",
    "gut", "pathogen", "bacteria", "virus", "viral", "infection", "epidemiology", "clinical", "therapy", "therapeutic",
    "disease", "patient", "biopsy", "in vivo", "in vitro", "drug", "pharmacology", "oncology"
]

JOURNAL_THRESHOLDS = {
    "Nature": 6,
    "Nature_Physics": 6,
    "Nature_Materials": 6,
    "Nature_Communications": 6,
    "npj_QuantumMaterials": 5,
    "Science": 6,
    "Science_Advances": 7,
    "PRL_Recent": 7,
    "PRB_Recent": 7,
    "arXiv_CondMat": 7,
}

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

primary_model = 'gemini-3-flash-preview'
fallback_model = 'gemini-2.5-flash'
current_model = None
using_primary_model = True
try:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        current_model = genai.GenerativeModel(primary_model)
        print(f"{COLOR_GREEN}{COLOR_BOLD}✓ Gemini API configured{COLOR_END}", file=sys.stderr)
    else:
        print(f"{COLOR_YELLOW}⚠ GOOGLE_API_KEY not found. Gemini filter skipped.{COLOR_END}", file=sys.stderr)
except Exception as e:
    print(f"{COLOR_RED}✗ Error configuring Gemini API: {e}{COLOR_END}", file=sys.stderr)
    using_primary_model = False


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    return html.unescape(re.sub(r'\s+', ' ', text)).strip()


def safe_text(text):
    return html.escape(strip_html(text or ""), quote=False)


def get_entry_link(entry):
    if entry.get('link'):
        return entry.get('link')
    for lnk in entry.get('links', []):
        if lnk.get('href'):
            return lnk.get('href')
    return ""


def get_authors(entry):
    authors = []
    if entry.get('authors'):
        for a in entry.get('authors', []):
            name = a.get('name') if isinstance(a, dict) else str(a)
            if name:
                authors.append(strip_html(name))
    if not authors and entry.get('author'):
        raw = strip_html(entry.get('author'))
        # Split cautiously; arXiv often uses comma-separated authors.
        parts = re.split(r'\s*(?:;|, and | and )\s*', raw)
        authors = [p.strip() for p in parts if p.strip()]
    # Some feeds expose dc:creator as dc_creator or creator.
    for key in ['dc_creator', 'creator']:
        if not authors and entry.get(key):
            raw = strip_html(entry.get(key))
            authors = [p.strip() for p in re.split(r'\s*;\s*', raw) if p.strip()]
    # De-duplicate while preserving order.
    seen, deduped = set(), []
    for a in authors:
        if a not in seen:
            deduped.append(a); seen.add(a)
    return deduped


def compact_authors(authors, front=3, back=2):
    if not authors:
        return "Authors not available in source RSS"
    if len(authors) <= front + back + 1:
        return ", ".join(authors)
    return ", ".join(authors[:front]) + ", et al.; last authors: " + ", ".join(authors[-back:])


def last_author_line(authors):
    if not authors:
        return "Last/corresponding-author proxy: not available"
    tail = authors[-2:] if len(authors) >= 2 else authors
    return "Last/corresponding-author proxy: " + ", ".join(tail)



def score_to_tier(score):
    try:
        score = int(score)
    except Exception:
        score = 0
    if score >= 9:
        return "A_MUST_READ"
    if score >= 7:
        return "B_IMPORTANT_CONDMAT"
    if score >= 4:
        return "C_MAYBE"
    return "D_ARCHIVE"


def get_threshold(journal_name):
    return JOURNAL_THRESHOLDS.get(journal_name, 6)


def tag_keywords(title, summary):
    text = (strip_html(title) + " " + strip_html(summary)).lower()
    tags = []
    for kw in DIRECT_RELEVANCE_KEYWORDS + BROAD_CONDMAT_KEYWORDS:
        if kw.lower() in text:
            clean = re.sub(r'\s+', '', kw)
            clean = re.sub(r'[^A-Za-z0-9_+-]', '', clean)
            if clean and clean not in tags:
                tags.append(clean)
    return tags[:8]


def find_title_autopass(title):
    for kw in NARROW_TITLE_AUTOPASS:
        if re.search(r'\b' + re.escape(kw) + r'\b', title or "", flags=re.IGNORECASE):
            return kw
    return None


def find_negative_hints(title, summary):
    text = (strip_html(title) + " " + strip_html(summary)).lower()
    return [kw for kw in STRONG_NEGATIVE_KEYWORDS if kw.lower() in text][:5]


def arxiv_html_url(link):
    if not link:
        return None
    m = re.search(r'arxiv\.org/(?:abs|pdf)/([^?#]+)', link)
    if not m:
        return None
    arxiv_id = m.group(1).replace('.pdf', '')
    return f"https://arxiv.org/html/{arxiv_id}"


def fetch_first_image_from_html(url, timeout=15):
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 400:
            return None
        html_text = resp.text
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html_text, flags=re.I)
        if m:
            return urljoin(url, html.unescape(m.group(1)))
        for m in re.finditer(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']', html_text, flags=re.I):
            src = html.unescape(m.group(1))
            if any(skip in src.lower() for skip in ["logo", "icon", "favicon", "avatar"]):
                continue
            return urljoin(url, src)
    except Exception as e:
        print(f"      {COLOR_YELLOW}Image fetch skipped for {url}: {e}{COLOR_END}", file=sys.stderr)
    return None


def get_article_image(entry, journal_name):
    for key in ["media_thumbnail", "media_content"]:
        vals = entry.get(key)
        if vals:
            for v in vals:
                if isinstance(v, dict) and v.get("url"):
                    return v.get("url")
    link = get_entry_link(entry)
    if journal_name == "arXiv_CondMat":
        img = fetch_first_image_from_html(arxiv_html_url(link))
        if img:
            return img
    return fetch_first_image_from_html(link)

def find_and_highlight_keyword(title, summary, keywords, color_code):
    for keyword in keywords:
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, title, re.IGNORECASE):
            highlighted = re.sub(pattern, f"{color_code}{COLOR_BOLD}{keyword}{COLOR_END}", title, flags=re.IGNORECASE, count=1)
            return highlighted, keyword, "Title"
    for keyword in keywords:
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, summary, re.IGNORECASE):
            return title, keyword, "Abstract"
    return title, None, None


def keyword_score(entry):
    text = (entry.get('title', '') + ' ' + strip_html(entry.get('summary', ''))).lower()
    direct = [kw for kw in DIRECT_RELEVANCE_KEYWORDS if kw.lower() in text]
    broad = [kw for kw in BROAD_CONDMAT_KEYWORDS if kw.lower() in text]
    if direct:
        return 3, direct[:5]
    if broad:
        return 2, broad[:5]
    return 0, []


def build_gemini_prompt(journal_name):
    threshold = get_threshold(journal_name)
    if journal_name in ["PRL_Recent", "PRB_Recent", "arXiv_CondMat"]:
        scope = (
            "This source is noisy for the user because it contains many formal theory papers. "
            "Be selective. Generic quantum information, high-energy, cosmology, cold atom, generic Majorana, "
            "abstract Krylov/Floquet/SYK/tensor-network papers should usually score 0-3 unless they connect clearly "
            "to real condensed-matter materials, spectroscopy, kagome/CDW/nematicity/topology, or electronic structure."
        )
    else:
        scope = (
            "This source is a broad high-impact journal feed. Remove biology/medicine/climate/astronomy/news, "
            "but keep significant condensed-matter/materials/quantum materials papers."
        )

    return f"""
You are ranking scientific papers for a postdoctoral experimental condensed-matter physicist specializing in ARPES.

USER PROFILE:
- Strongest direct interests: ARPES, magneto-ARPES, CD-ARPES, RIXS, STM/STS when electronic-structure related.
- Materials/projects: kagome metals AV3Sb5/CsV3Sb5/RbV3Sb5, CDW, nematicity, loop current, TRSB.
- Also important: Weyl/Dirac/topological semimetals, Berry curvature, anomalous Hall, altermagnetism, magnetic topological materials.
- Also relevant: quasi-1D materials, Luttinger liquid, spin-charge separation, NbSe3, TaNiTe2/NbNiTe2 112 tellurides.
- Goal: morning skim feed. Missing a relevant paper is worse than keeping a few extras.

SOURCE POLICY:
{scope}
Journal/source: {journal_name}
RSS pass threshold for this source: score >= {threshold}/10.

SCORING RUBRIC:
10 = direct hit for user's current projects: ARPES/magnetoARPES/CD-ARPES, AV3Sb5/CsV3Sb5/RbV3Sb5, kagome CDW/nematicity/loop current/TRSB.
8-9 = very relevant quantum materials paper: kagome/topological semimetal/Berry curvature/anomalous Hall/altermagnetism/electronic structure with experimental relevance.
6-7 = broad important condensed matter / materials physics paper that a CM experimentalist should know.
4-5 = adjacent condensed matter or theory watch; keep if uncertainty is meaningful.
1-3 = mostly unrelated formal theory, generic quantum information, generic Majorana wires, generic Krylov/Floquet/SYK/tensor network, soft matter, photonics without CM/materials relevance.
0 = clearly unrelated biology, medicine, climate, astronomy, chemistry synthesis without CM physics, news/editorial/correction.

THEORY POLICY:
- Do not over-score theory just because it says topological, Majorana, Floquet, Krylov, Kitaev, Chern, quantum, or graphene.
- Theory scores high only if it is likely useful for interpreting real quantum materials, ARPES spectra, kagome/CDW/nematicity, magnetic topology, Berry-curvature transport, or spectroscopy.
- Examples:
  * "Krylov dynamics in ergodic Floquet systems" => score 1, D_ARCHIVE.
  * "Majorana zero modes in semiconductor wires" => score 3, D_ARCHIVE unless direct experimental/topological-material relevance is clear.
  * "Higher-dimensional generalization of the Kitaev spin liquid" => score 3, D_ARCHIVE.
  * "Berry curvature induced giant anomalous Hall responses in layered kagome antiferromagnet GdTi3Bi4" => score 8, B_IMPORTANT_CONDMAT.

OUTPUT:
Return a JSON array only. One object per article:
{{
  "title": "exact input title",
  "score": integer 0-10,
  "decision": "YES" or "NO",
  "tier": "A_MUST_READ" | "B_IMPORTANT_CONDMAT" | "C_MAYBE" | "D_ARCHIVE",
  "reason": "one short phrase under 18 words",
  "tags": ["ARPES", "kagome", "CDW"]
}}
Use decision YES iff score >= {threshold}. If unsure but plausibly relevant, give 4-6 rather than 0-3.

Articles:
"""


def classify_entries_with_gemini(journal_name, entries):
    global current_model, using_primary_model
    passed, removed, metadata = [], [], {}
    if not entries:
        return passed, removed, metadata
    if not current_model:
        for entry in entries:
            metadata[get_entry_link(entry)] = {
                "tier": "C_MAYBE_UNCLASSIFIED",
                "score": 5,
                "reason": "Gemini unavailable; kept to avoid false negative",
                "tags": tag_keywords(entry.get('title',''), entry.get('summary','')),
            }
            passed.append(entry)
        return passed, removed, metadata

    threshold = get_threshold(journal_name)
    base_prompt = build_gemini_prompt(journal_name)
    batch_size = 60
    for start in range(0, len(entries), batch_size):
        batch_entries = entries[start:start+batch_size]
        batch_num = start//batch_size + 1
        total_batches = math.ceil(len(entries) / batch_size)
        print(f"    {COLOR_BLUE}📦 Gemini scoring batch {batch_num}/{total_batches}{COLOR_END}", file=sys.stderr)
        payload = []
        for e in batch_entries:
            title = e.get('title','')
            summary = strip_html(e.get('summary',''))
            payload.append({
                "title": title,
                "summary": summary[:4500],
                "keyword_hints": tag_keywords(title, summary),
                "negative_hints": find_negative_hints(title, summary),
            })
        full_prompt = base_prompt + json.dumps(payload, ensure_ascii=False, indent=2)
        max_attempts = 3
        api_success = False
        attempt = 0
        while not api_success:
            try:
                response = current_model.generate_content(
                    full_prompt,
                    generation_config=genai.types.GenerationConfig(response_mime_type="application/json"),
                    request_options={'timeout': 120}
                )
                decisions = json.loads(response.text)
                by_title = {e.get('title',''): e for e in batch_entries}
                used_titles = set()
                for d in decisions:
                    title = d.get('title','')
                    entry = by_title.get(title)
                    if not entry:
                        continue
                    used_titles.add(title)
                    try:
                        score = int(d.get('score', 0))
                    except Exception:
                        score = 0
                    score = max(0, min(10, score))
                    tier = d.get('tier') or score_to_tier(score)
                    reason = strip_html(d.get('reason',''))[:180]
                    tags = d.get('tags') or tag_keywords(entry.get('title',''), entry.get('summary',''))
                    if not isinstance(tags, list):
                        tags = tag_keywords(entry.get('title',''), entry.get('summary',''))
                    tags = [strip_html(str(t)).replace(" ", "") for t in tags if strip_html(str(t))][:8]
                    link = get_entry_link(entry)
                    metadata[link] = {"tier": tier, "score": score, "reason": reason, "tags": tags}
                    if score >= threshold:
                        passed.append(entry)
                        print(f"      🤖✅ [{score}] {title} [{tier}]", file=sys.stderr)
                    else:
                        removed.append(entry)
                        print(f"      🤖❌ [{score}] {title} [{tier}]", file=sys.stderr)

                for entry in batch_entries:
                    if entry.get('title','') not in used_titles:
                        metadata[get_entry_link(entry)] = {
                            "tier": "C_MAYBE_UNCLASSIFIED",
                            "score": 5,
                            "reason": "Gemini response missing item; kept to avoid false negative",
                            "tags": tag_keywords(entry.get('title',''), entry.get('summary','')),
                        }
                        passed.append(entry)
                api_success = True
            except exceptions.ResourceExhausted as e:
                print(f"      {COLOR_ORANGE}Quota issue: {e}{COLOR_END}", file=sys.stderr)
                if using_primary_model:
                    current_model = genai.GenerativeModel(fallback_model)
                    using_primary_model = False
                    attempt = 0
                    continue
                break
            except Exception as e:
                print(f"      {COLOR_RED}Gemini error attempt {attempt+1}: {e}{COLOR_END}", file=sys.stderr)
                attempt += 1
                if attempt >= max_attempts:
                    break
                time.sleep(30)
        if not api_success:
            print(f"      {COLOR_YELLOW}Gemini batch failed. Keeping items as C_MAYBE_UNCLASSIFIED.{COLOR_END}", file=sys.stderr)
            for entry in batch_entries:
                passed.append(entry)
                metadata[get_entry_link(entry)] = {
                    "tier": "C_MAYBE_UNCLASSIFIED",
                    "score": 5,
                    "reason": "Gemini failed; kept to avoid false negative",
                    "tags": tag_keywords(entry.get('title',''), entry.get('summary','')),
                }
    return passed, removed, metadata


def find_xml_items(root):
    namespaces = {
        'atom': 'http://www.w3.org/2005/Atom',
        'rss1': 'http://purl.org/rss/1.0/',
        'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
        'dc': 'http://purl.org/dc/elements/1.1/',
        'content': 'http://purl.org/rss/1.0/modules/content/'
    }
    items = []
    if root.tag == 'rss':
        channel = root.find('channel')
        if channel is not None:
            for item in list(channel.findall('item')):
                link_el = item.find('link')
                link = link_el.text.strip() if link_el is not None and link_el.text else ''
                items.append((item, link, channel, 'rss2'))
    elif root.tag == '{http://www.w3.org/2005/Atom}feed':
        for item in list(root.findall('atom:entry', namespaces=namespaces)):
            link = ''
            link_el = item.find('atom:link', namespaces=namespaces)
            if link_el is not None:
                link = link_el.get('href', '')
            items.append((item, link, root, 'atom'))
    elif root.tag == '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF':
        for item in list(root.findall('rss1:item', namespaces=namespaces)):
            link = item.get(f"{{{namespaces['rdf']}}}about") or ''
            if not link:
                link_el = item.find('rss1:link', namespaces=namespaces)
                link = link_el.text.strip() if link_el is not None and link_el.text else ''
            items.append((item, link, root, 'rss1'))
    return items, namespaces


def entry_by_link(parsed_entries):
    return {get_entry_link(e): e for e in parsed_entries if get_entry_link(e)}


def ensure_description_prefix(xml_item, feed_type, entry, meta, journal_name):
    authors = get_authors(entry)
    author_compact = compact_authors(authors)
    last_line = last_author_line(authors)
    tier = meta.get('tier', '')
    score = meta.get('score', '')
    reason = meta.get('reason', '')
    tags = meta.get('tags', []) or tag_keywords(entry.get('title',''), entry.get('summary',''))
    abstract = strip_html(entry.get('summary', ''))
    image_url = get_article_image(entry, journal_name)

    tag_html = " ".join([f"<span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#{safe_text(t)}</span>" for t in tags])
    score_badge = f"<span style='display:inline-block;padding:4px 9px;border-radius:999px;background:#fee2e2;color:#991b1b;font-weight:700;'>Score {safe_text(str(score))}/10</span>" if score != '' else ""

    # Put authors first so Reeder's article view shows the research group immediately under the title.
    prefix_html = f"<p><b>Authors:</b> {safe_text(author_compact)}</p>"
    prefix_html += f"<p><b>{safe_text(last_line)}</b></p>"
    if score_badge:
        prefix_html += f"<p>{score_badge} <b>{safe_text(tier)}</b></p>"
    elif tier:
        prefix_html += f"<p><b>Tier:</b> {safe_text(tier)}</p>"
    if reason:
        prefix_html += f"<p><b>Why:</b> {safe_text(reason)}</p>"
    if tag_html:
        prefix_html += f"<p><b>Tags:</b> {tag_html}</p>"
    if image_url:
        prefix_html += f"<p><img src='{html.escape(image_url, quote=True)}' style='max-width:100%;height:auto;border-radius:10px;' /></p>"
    if abstract:
        prefix_html += f"<hr/><p><b>Abstract:</b> {safe_text(abstract)}</p>"

    ns_atom = 'http://www.w3.org/2005/Atom'
    ns_dc = 'http://purl.org/dc/elements/1.1/'
    if feed_type == 'atom':
        summary_el = xml_item.find(f'{{{ns_atom}}}summary')
        if summary_el is None:
            summary_el = ET.SubElement(xml_item, f'{{{ns_atom}}}summary')
        summary_el.set('type', 'html')
        summary_el.text = prefix_html
        if authors:
            for old in xml_item.findall(f'{{{ns_atom}}}author'):
                xml_item.remove(old)
            author_el = ET.SubElement(xml_item, f'{{{ns_atom}}}author')
            name_el = ET.SubElement(author_el, f'{{{ns_atom}}}name')
            name_el.text = author_compact
    else:
        desc_el = xml_item.find('description')
        if desc_el is None:
            desc_el = ET.SubElement(xml_item, 'description')
        desc_el.text = ET.CDATA(prefix_html)
        if authors:
            dc_el = xml_item.find(f'{{{ns_dc}}}creator')
            if dc_el is None:
                dc_el = ET.SubElement(xml_item, f'{{{ns_dc}}}creator')
            dc_el.text = author_compact

    # Prefix title with score for fast Reeder list triage.
    if score != '':
        title_el = xml_item.find('title') if feed_type != 'atom' else xml_item.find(f'{{{ns_atom}}}title')
        if title_el is not None and title_el.text and not re.match(r'^\[\d{1,2}\]', strip_html(title_el.text)):
            title_el.text = f"[{score}] {strip_html(title_el.text)}"


def filter_rss_for_journal(journal_name, feed_url):
    target_url = feed_url.strip('<> ')
    print(f"\n{'='*80}\n{COLOR_BOLD}{COLOR_BLUE}📚 {journal_name}{COLOR_END}\n{target_url}\n{'='*80}", file=sys.stderr)
    response = requests.get(target_url, timeout=30)
    response.raise_for_status()
    raw_xml = response.content
    parsed_feed = feedparser.parse(raw_xml)

    threshold = get_threshold(journal_name)

    keyword_passed_entries, gemini_pending_entries = [], []
    keyword_removed_entries = []
    meta_by_link = {}

    for entry in parsed_feed.entries:
        title = entry.get('title', '')
        summary = entry.get('summary', '')
        link = get_entry_link(entry)
        autopass_kw = find_title_autopass(title)
        if autopass_kw:
            tags = tag_keywords(title, summary)
            score = 10 if any(k in autopass_kw.lower() for k in ["arpes", "csv3sb5", "rbv3sb5", "v3sb5"]) else 9
            keyword_passed_entries.append(entry)
            meta_by_link[link] = {
                "tier": score_to_tier(score),
                "score": score,
                "reason": f"title strong match: {autopass_kw}",
                "tags": tags or [autopass_kw.replace(" ", "")],
            }
            print(f"  ✅ [{score}] {title} (title strong match: {autopass_kw})", file=sys.stderr)
        else:
            gemini_pending_entries.append(entry)

    gemini_passed_entries, gemini_removed_entries, gemini_meta = classify_entries_with_gemini(journal_name, gemini_pending_entries)
    meta_by_link.update(gemini_meta)

    passed_entries = keyword_passed_entries + gemini_passed_entries
    passed_links = set(get_entry_link(e) for e in passed_entries)

    root = ET.fromstring(raw_xml)
    xml_items, namespaces = find_xml_items(root)
    parsed_map = entry_by_link(parsed_feed.entries)

    if root.tag == 'rss':
        channel = root.find('channel')
        for item, link, parent, feed_type in xml_items:
            if link not in passed_links:
                channel.remove(item)
            else:
                ensure_description_prefix(item, feed_type, parsed_map.get(link, {}), meta_by_link.get(link, {}), journal_name)
    elif root.tag == '{http://www.w3.org/2005/Atom}feed':
        for item, link, parent, feed_type in xml_items:
            if link not in passed_links:
                root.remove(item)
            else:
                ensure_description_prefix(item, feed_type, parsed_map.get(link, {}), meta_by_link.get(link, {}), journal_name)
    elif root.tag == '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF':
        for item, link, parent, feed_type in xml_items:
            if link not in passed_links:
                root.remove(item)
            else:
                ensure_description_prefix(item, feed_type, parsed_map.get(link, {}), meta_by_link.get(link, {}), journal_name)
        for channel in root.findall('rss1:channel', namespaces=namespaces):
            items = channel.find('rss1:items', namespaces=namespaces)
            if items is not None:
                rdf_seq = items.find('rdf:Seq', namespaces=namespaces)
                if rdf_seq is not None:
                    for li in list(rdf_seq.findall('rdf:li', namespaces=namespaces)):
                        if li.get(f"{{{namespaces['rdf']}}}resource") not in passed_links:
                            rdf_seq.remove(li)

    buffer = BytesIO()
    ET.ElementTree(root).write(buffer, encoding='utf-8', xml_declaration=True, pretty_print=True)
    all_entries = keyword_passed_entries + gemini_passed_entries + keyword_removed_entries + gemini_removed_entries
    return buffer.getvalue(), keyword_passed_entries, gemini_passed_entries, keyword_removed_entries, gemini_removed_entries, meta_by_link


def paper_record(entry, journal, source, meta):
    authors = get_authors(entry)
    m = meta.get(get_entry_link(entry), {})
    return {
        "journal": journal,
        "source": source,
        "title": entry.get('title', 'No title'),
        "link": get_entry_link(entry),
        "authors": compact_authors(authors),
        "last_authors": last_author_line(authors).replace('Last/corresponding-author proxy: ', ''),
        "summary": strip_html(entry.get('summary', '')),
        "tier": m.get('tier', ''),
        "score": m.get('score', ''),
        "reason": m.get('reason', ''),
        "tags": m.get('tags', []),
        "image": get_article_image(entry, journal),
    }


def create_email_body_file(content):
    with open('filtered_titles.txt', 'w', encoding='utf-8') as f:
        f.write(content)


def create_results_html_file(email_body_content):
    lines = email_body_content.strip().split('\n')
    html_parts = ["""<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Filtered Paper Results</title><script src='https://cdn.tailwindcss.com'></script></head><body class='bg-gray-100 p-8'><div class='mb-6'><a href='index.html' class='inline-flex items-center px-4 py-2 bg-indigo-600 text-white font-semibold rounded hover:bg-indigo-700'>← To Main</a></div><div class='max-w-7xl mx-auto bg-white rounded-xl shadow-2xl p-8'><h1 class='text-3xl font-bold text-gray-800 mb-6 text-center'>Filtered Paper Results</h1><div class='space-y-2'>"""]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('---'):
            html_parts.append(f"<h2 class='text-xl font-bold text-indigo-700 mt-6 mb-2'>{html.escape(line.replace('---','').strip())}</h2>")
        elif line.endswith(':'):
            html_parts.append(f"<p class='text-lg font-semibold text-gray-800 mt-4'>{html.escape(line)}</p>")
        else:
            m = re.match(r'^(.*?)\s(.+)\s\((http[s]?://.+)\)$', line)
            if m:
                emoticon, title, link = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
                html_parts.append(f"<div class='p-2 bg-gray-50 rounded-lg shadow-sm hover:bg-gray-100'><p class='text-gray-700 text-sm font-medium'>{html.escape(emoticon)} <a href='{html.escape(link)}' target='_blank' class='text-blue-600 hover:underline'>{html.escape(strip_html(title))}</a></p></div>")
            else:
                html_parts.append(f"<p class='text-gray-600 ml-6'>{html.escape(line)}</p>")
    html_parts.append("</div></div></body></html>")
    with open('filtered_results.html', 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_parts))


def tier_rank(tier):
    order = {'A_MUST_READ': 0, 'B_IMPORTANT_CONDMAT': 1, 'C_MAYBE': 2, 'C_MAYBE_UNCLASSIFIED': 3, '': 4}
    return order.get(tier, 4)


def create_briefing_html(records):
    # Keep journal grouping, but sort within each journal by tier.
    grouped = {}
    for r in records:
        grouped.setdefault(r['journal'], []).append(r)
    now_utc = datetime.datetime.utcnow()
    now_texas = now_utc - datetime.timedelta(hours=5)
    now_korea = now_utc + datetime.timedelta(hours=9)
    total_a = sum(1 for r in records if r['tier'] == 'A_MUST_READ')
    total_b = sum(1 for r in records if r['tier'] == 'B_IMPORTANT_CONDMAT')
    total_c = sum(1 for r in records if r['tier'].startswith('C_') or r['tier'] == '')
    html_parts = [f"""<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Daily Paper Briefing</title><script src='https://cdn.tailwindcss.com'></script></head><body class='bg-slate-100 p-6'><div class='max-w-6xl mx-auto'><div class='mb-6'><a href='index.html' class='inline-flex items-center px-4 py-2 bg-indigo-600 text-white font-semibold rounded hover:bg-indigo-700'>← To Main</a></div><div class='bg-white rounded-2xl shadow-xl p-8'><h1 class='text-3xl font-bold text-slate-900'>Daily Paper Briefing</h1><p class='text-slate-600 mt-2'>Journal-by-journal view. RSS feeds remain journal-specific; this page is only for fast morning triage.</p><div class='grid grid-cols-1 md:grid-cols-3 gap-4 my-6'><div class='p-4 rounded-xl bg-red-50'><div class='text-2xl font-bold'>{total_a}</div><div class='text-sm text-slate-600'>A Must Read</div></div><div class='p-4 rounded-xl bg-amber-50'><div class='text-2xl font-bold'>{total_b}</div><div class='text-sm text-slate-600'>B Important CM</div></div><div class='p-4 rounded-xl bg-slate-50'><div class='text-2xl font-bold'>{total_c}</div><div class='text-sm text-slate-600'>C Maybe / Unclassified</div></div></div><p class='text-xs text-slate-500 mb-8'>Last updated: {now_texas.strftime('%Y-%m-%d %H:%M')} Texas / {now_korea.strftime('%Y-%m-%d %H:%M')} Korea</p>"""]
    for journal, items in grouped.items():
        items = sorted(items, key=lambda r: (tier_rank(r['tier']), r['title']))
        html_parts.append(f"<section class='mt-8'><h2 class='text-2xl font-bold text-indigo-700 border-b pb-2'>{html.escape(journal)}</h2><div class='space-y-4 mt-4'>")
        if not items:
            html_parts.append("<p class='text-slate-500'>No passed papers.</p>")
        for r in items:
            tier = r['tier'] or 'UNCLASSIFIED'
            badge = 'bg-red-100 text-red-800' if tier == 'A_MUST_READ' else 'bg-amber-100 text-amber-800' if tier == 'B_IMPORTANT_CONDMAT' else 'bg-slate-100 text-slate-700'
            html_parts.append(f"""
<div class='p-4 rounded-xl border bg-white hover:bg-slate-50'>
  <div class='flex flex-wrap gap-2 items-center mb-1'><span class='text-xs px-2 py-1 rounded-full {badge}'>{html.escape(tier)}</span><span class='text-xs text-slate-500'>{html.escape(r['source'])}</span></div>
  <a href='{html.escape(r['link'])}' target='_blank' class='text-lg font-semibold text-blue-700 hover:underline'>{html.escape(strip_html(r['title']))}</a>
  <p class='text-sm text-slate-700 mt-1'><b>Authors:</b> {html.escape(r['authors'])}</p>
  <p class='text-sm text-slate-700'><b>Last/corresponding-author proxy:</b> {html.escape(r['last_authors'])}</p>
  <p class='text-sm text-slate-600 mt-1'><b>Why:</b> {html.escape(r['reason'] or 'keyword/Gemini passed')}</p>
  <details class='mt-2'><summary class='cursor-pointer text-sm text-slate-500'>Abstract</summary><p class='text-sm text-slate-700 mt-2'>{html.escape(r['summary'][:1600])}</p></details>
</div>""")
        html_parts.append("</div></section>")
    html_parts.append("</div></div></body></html>")
    with open('briefing.html', 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_parts))


def create_index_html(journal_urls, rss_base_filename):
    now_utc = datetime.datetime.utcnow()
    now_korea = now_utc + datetime.timedelta(hours=9)
    now_texas = now_utc - datetime.timedelta(hours=5)
    html_content = f"""
<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Filtered Paper RSS Feeds</title><script src='https://cdn.tailwindcss.com'></script></head>
<body class='bg-gray-100 flex items-center justify-center min-h-screen p-4'><div class='bg-white rounded-xl shadow-2xl p-8 max-w-lg w-full text-center'>
<h1 class='text-3xl font-bold text-gray-800 mb-2'>Filtered Paper RSS Feeds</h1>
<p class='text-gray-600 mb-8'>Journal-specific RSS feeds filtered for condensed matter / ARPES relevance.</p>
<div class='space-y-4'>
<a href='briefing.html' target='_blank' class='block w-full px-6 py-4 bg-rose-600 text-white font-semibold rounded-lg shadow-md hover:bg-rose-700'>Daily Briefing</a>
"""
    for journal_name in journal_urls.keys():
        filename = f"{rss_base_filename}_{journal_name}.xml"
        html_content += f"<a href='{filename}' target='_blank' class='block w-full px-6 py-4 bg-indigo-600 text-white font-semibold rounded-lg shadow-md hover:bg-indigo-700'>{journal_name} RSS Feed</a>\n"
    html_content += f"""
<a href='filtered_results.html' target='_blank' class='block w-full px-6 py-4 bg-green-600 text-white font-semibold rounded-lg shadow-md hover:bg-green-700'>Passed / Filtered Audit List</a>
</div><div class='mt-8 text-sm text-gray-500'><p>Last Updated (Korea): {now_korea.strftime('%Y-%m-%d %H:%M:%S')} KST</p><p>Last Updated (Texas): {now_texas.strftime('%Y-%m-%d %H:%M:%S')} CDT</p><p>Updates daily at 08:00 and 19:00 CDT</p></div>
<div class='mt-8 text-center text-sm text-gray-500'><a href='https://yilab.rice.edu/people/' target='_blank' class='text-gray-500 hover:text-gray-700 hover:underline'>Created by Jounghoon Hyun</a></div>
</div></body></html>
"""
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_content)


def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def clear_partial_state():
    for path in ['partial_briefing_records.json', 'partial_email_content.txt']:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


if __name__ == '__main__':
    OUTPUT_FILE_BASE = 'filtered_feed'
    STATE_FILE = 'last_failed_journal.txt'
    email_content = ''
    briefing_records = []
    journals_to_process = list(JOURNAL_URLS.items())
    start_index = 0
    resume_mode = False

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            last_failed = f.read().strip()
        if last_failed and last_failed != 'SUCCESS':
            names = list(JOURNAL_URLS.keys())
            if last_failed in names:
                start_index = names.index(last_failed)
                resume_mode = True

    # If we are resuming after a failed run, restore already processed audit/briefing state.
    # The workflow restores these files from cache before running this script.
    if resume_mode:
        if os.path.exists('partial_email_content.txt'):
            with open('partial_email_content.txt', 'r', encoding='utf-8') as f:
                email_content = f.read()
        briefing_records = load_json_file('partial_briefing_records.json', [])
        email_content += f"\n\n--- RESUME ---\nResuming from journal: {journals_to_process[start_index][0]}\n\n"
    else:
        clear_partial_state()

    try:
        for journal_name, feed_url in journals_to_process[start_index:]:
            try:
                filtered_xml, keyword_passed, gemini_passed, keyword_removed, gemini_removed, meta = filter_rss_for_journal(journal_name, feed_url)
                output_filename = f"{OUTPUT_FILE_BASE}_{journal_name}.xml"
                with open(output_filename, 'wb') as f:
                    f.write(filtered_xml)

                email_content += f"--- {journal_name} ---\n\nPASSED PAPERS:\n"
                if not keyword_passed and not gemini_passed:
                    email_content += 'No papers found matching your filters.\n\n'
                else:
                    for entry in keyword_passed:
                        email_content += f"  ✅ {entry.get('title', 'No title')} ({get_entry_link(entry) or 'No link'})\n"
                        briefing_records.append(paper_record(entry, journal_name, 'keyword', meta))
                    for entry in gemini_passed:
                        email_content += f"  🤖✅ {entry.get('title', 'No title')} ({get_entry_link(entry) or 'No link'})\n"
                        briefing_records.append(paper_record(entry, journal_name, 'Gemini', meta))
                    email_content += '\n'

                email_content += 'REMOVED PAPERS:\n'
                if not keyword_removed and not gemini_removed:
                    email_content += 'No papers were filtered out.\n\n'
                else:
                    for entry in keyword_removed:
                        email_content += f"  ❌ {entry.get('title', 'No title')} ({get_entry_link(entry) or 'No link'})\n"
                    for entry in gemini_removed:
                        email_content += f"  🤖❌ {entry.get('title', 'No title')} ({get_entry_link(entry) or 'No link'})\n"
                    email_content += '\n'

                # Persist partial progress after each successful journal. If a later journal fails,
                # the next workflow run can resume without losing already processed results.
                with open('partial_email_content.txt', 'w', encoding='utf-8') as f:
                    f.write(email_content)
                save_json_file('partial_briefing_records.json', briefing_records)
            except Exception as e:
                with open(STATE_FILE, 'w', encoding='utf-8') as f:
                    f.write(journal_name)
                email_content += f"\n\nAn error occurred while running the filter script for '{journal_name}':\n{e}\nPlease check workflow logs.\n"
                raise

        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            f.write('SUCCESS')
        create_index_html(JOURNAL_URLS, OUTPUT_FILE_BASE)
        create_results_html_file(email_content)
        create_briefing_html(briefing_records)
        clear_partial_state()
    finally:
        github_server_url = os.getenv('GITHUB_SERVER_URL')
        github_repository = os.getenv('GITHUB_REPOSITORY')
        github_run_id = os.getenv('GITHUB_RUN_ID')
        if github_server_url and github_repository and github_run_id:
            email_content += f"\n\n---\n\nCheck GitHub Actions run for details:\n{github_server_url}/{github_repository}/actions/runs/{github_run_id}\n"
        create_email_body_file(email_content)
