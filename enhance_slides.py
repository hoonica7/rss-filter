"""Post-process slides.html into a figure-rich Korean reading briefing.

Filter_RSS.py creates the filtered paper list and a basic slideshow. This
script runs after it, parses the generated slide JSON, adds source-page images,
asks Gemini for Korean mini-briefings, and rewrites slides.html. It intentionally
keeps personal ranking values in GitHub Actions secrets rather than source code.
"""

from functools import lru_cache
from urllib.parse import quote, urljoin
import html
import json
import math
import os
import re
import sys

import requests
from google import genai
from google.genai import types


COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_END = "\033[0m"

_DEFAULT_MODELS = "gemini-3-flash-preview,gemini-3.1-flash-lite-preview,gemini-2.5-flash"
_models_env = (os.getenv("GEMINI_MODELS") or "").strip()
MODEL_CANDIDATES = [m.strip() for m in (_models_env or _DEFAULT_MODELS).split(",") if m.strip()]
if not MODEL_CANDIDATES:
    MODEL_CANDIDATES = ["gemini-2.5-flash"]

GOOGLE_API_KEYS = []
for _i in (1, 2, 3):
    _key = os.getenv(f"GOOGLE_API_KEY{_i}")
    if _key:
        GOOGLE_API_KEYS.append((f"KEY{_i}", _key))
if not GOOGLE_API_KEYS and os.getenv("GOOGLE_API_KEY"):
    GOOGLE_API_KEYS.append(("LEGACY", os.getenv("GOOGLE_API_KEY")))

gemini_clients = []
for label, key in GOOGLE_API_KEYS:
    try:
        gemini_clients.append((label, genai.Client(api_key=key)))
    except Exception as exc:
        print(f"{COLOR_YELLOW}Slide briefing Gemini init failed for {label}: {exc}{COLOR_END}", file=sys.stderr)

current_api_index = 0
current_model_index = 0

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def xml_compatible_text(text):
    if text is None:
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        if ch in "\t\n\r" or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF:
            out.append(ch)
    return "".join(out)


def strip_html(text):
    if not text:
        return ""
    text = xml_compatible_text(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return xml_compatible_text(html.unescape(re.sub(r"\s+", " ", text))).strip()


def arxiv_html_url(link):
    if not link:
        return None
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#]+)", link)
    if not match:
        return None
    arxiv_id = match.group(1).replace(".pdf", "")
    return f"https://arxiv.org/html/{arxiv_id}"


@lru_cache(maxsize=512)
def fetch_first_image_from_html(url, timeout=15):
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=timeout, headers=_BROWSER_HEADERS, allow_redirects=True)
        if resp.status_code >= 400:
            return ""
        html_text = resp.text
        patterns = [
            r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
            r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image[\"']",
            r"<meta[^>]+name=[\"']twitter:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
        ]
        for pattern in patterns:
            match = re.search(pattern, html_text, flags=re.I)
            if match:
                return urljoin(url, html.unescape(match.group(1)))
        for match in re.finditer(r"<img[^>]+(?:src|data-src)=[\"']([^\"']+)[\"']", html_text, flags=re.I):
            src = html.unescape(match.group(1))
            low = src.lower()
            if any(skip in low for skip in ("logo", "icon", "favicon", "avatar", "branding")):
                continue
            return urljoin(url, src)
    except Exception as exc:
        print(f"{COLOR_YELLOW}Slide image skipped for {url}: {exc}{COLOR_END}", file=sys.stderr)
    return ""


def meta_contents(html_text):
    values = []
    patterns = [
        r"<meta[^>]+(?:name|property)=[\"']([^\"']+)[\"'][^>]+content=[\"']([^\"']*)[\"']",
        r"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]+(?:name|property)=[\"']([^\"']+)[\"']",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html_text, flags=re.I | re.S):
            if len(match.groups()) != 2:
                continue
            first, second = match.group(1), match.group(2)
            if " " in first or len(first) > 80:
                name, content = second, first
            else:
                name, content = first, second
            values.append((html.unescape(name).strip().lower(), html.unescape(content).strip()))
    return values


def abstract_candidates_from_html(html_text):
    candidates = []
    for name, content in meta_contents(html_text):
        if name in {
            "citation_abstract",
            "dc.description",
            "dcterms.description",
            "description",
            "og:description",
            "twitter:description",
        }:
            cleaned = strip_html(content)
            if cleaned:
                candidates.append(cleaned)

    html_patterns = [
        r"<blockquote[^>]+class=[\"'][^\"']*abstract[^\"']*[\"'][^>]*>.*?(?:<span[^>]*>Abstract:\s*</span>)?(.*?)</blockquote>",
        r"<section[^>]+class=[\"'][^\"']*abstract[^\"']*[\"'][^>]*>(.*?)</section>",
        r"<div[^>]+class=[\"'][^\"']*abstract[^\"']*[\"'][^>]*>(.*?)</div>",
        r"<h2[^>]*>\s*Abstract\s*</h2>\s*<p[^>]*>(.*?)</p>",
    ]
    for pattern in html_patterns:
        for match in re.finditer(pattern, html_text, flags=re.I | re.S):
            cleaned = strip_html(match.group(1))
            cleaned = re.sub(r"^Abstract\s*:?\s*", "", cleaned, flags=re.I).strip()
            if cleaned:
                candidates.append(cleaned)
    return candidates


def doi_from_link(link):
    if not link:
        return ""
    match = re.search(r"(10\.\d{4,9}/[^?#\s]+)", link, flags=re.I)
    if not match:
        return ""
    return html.unescape(match.group(1)).rstrip(".")


def looks_truncated_summary(summary):
    text = (summary or "").strip()
    if not text:
        return True
    return bool(re.search(r"(\.\.\.|…)\s*(?:\[[^\]]+\])?(?:Published\s+\w+.*)?$", text, flags=re.I))


def choose_better_abstract(current, candidates):
    current = strip_html(current)
    best = current
    for candidate in candidates:
        candidate = strip_html(candidate)
        if not candidate:
            continue
        low = candidate.lower()
        if low.startswith(("author(s):", "published ", "doi:", "abstracts are invited")):
            continue
        if len(candidate) > len(best) + 60 or (looks_truncated_summary(best) and len(candidate) > len(best)):
            best = candidate
    return best


@lru_cache(maxsize=512)
def fetch_full_abstract_from_url(url, timeout=15):
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=timeout, headers=_BROWSER_HEADERS, allow_redirects=True)
        if resp.status_code >= 400:
            return ""
        return choose_better_abstract("", abstract_candidates_from_html(resp.text))
    except Exception as exc:
        print(f"{COLOR_YELLOW}Slide abstract fetch skipped for {url}: {exc}{COLOR_END}", file=sys.stderr)
    return ""


@lru_cache(maxsize=512)
def fetch_crossref_abstract(doi, timeout=15):
    if not doi:
        return ""
    url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "hoonica-rss-filter/1.0 (slide abstract lookup; mailto:actions@github.com)"},
        )
        if resp.status_code >= 400:
            return ""
        abstract = ((resp.json() or {}).get("message") or {}).get("abstract", "")
        return strip_html(abstract)
    except Exception as exc:
        print(f"{COLOR_YELLOW}Crossref abstract fetch skipped for {doi}: {exc}{COLOR_END}", file=sys.stderr)
    return ""


def full_abstract_for_slide(slide):
    current = strip_html(slide.get("summary", ""))
    link = slide.get("link", "")
    should_try = (
        looks_truncated_summary(current)
        or "journals.aps.org" in link
        or "doi.org/10.1103" in link.lower()
        or bool(arxiv_html_url(link))
    )
    if not should_try:
        return current

    candidates = []
    arxiv_html = arxiv_html_url(link)
    if arxiv_html:
        candidates.append(fetch_full_abstract_from_url(arxiv_html))
    candidates.append(fetch_full_abstract_from_url(link))
    doi = doi_from_link(link)
    if doi:
        candidates.append(fetch_crossref_abstract(doi))
    return choose_better_abstract(current, candidates)


def image_for_slide(slide):
    if slide.get("image"):
        return slide.get("image")
    link = slide.get("link", "")
    arxiv_html = arxiv_html_url(link)
    if arxiv_html:
        image = fetch_first_image_from_html(arxiv_html)
        if image:
            return image
    return fetch_first_image_from_html(link)


def extract_slides(path="slides.html"):
    if not os.path.exists(path):
        return []
    text = open(path, "r", encoding="utf-8").read()
    match = re.search(r"const\s+slides\s*=\s*(.*?);\s*let\s+index\s*=", text, flags=re.S)
    if not match:
        return []
    try:
        slides = json.loads(match.group(1))
    except Exception as exc:
        print(f"{COLOR_YELLOW}Could not parse existing slides.html JSON: {exc}{COLOR_END}", file=sys.stderr)
        return []
    return slides if isinstance(slides, list) else []


def generate_json_with_gemini(prompt, schema=None, max_output_tokens=8192, task_label="Gemini JSON"):
    global current_api_index, current_model_index
    if not gemini_clients:
        print(f"{COLOR_YELLOW}{task_label} skipped: Gemini unavailable.{COLOR_END}", file=sys.stderr)
        return None

    n_apis = len(gemini_clients)
    n_models = len(MODEL_CANDIDATES)
    api_attempts = [(current_api_index + i) % n_apis for i in range(n_apis)]
    model_attempts = [(current_model_index + j) % n_models for j in range(n_models)]

    for api_idx in api_attempts:
        key_label, client = gemini_clients[api_idx]
        for model_idx in model_attempts:
            model_name = MODEL_CANDIDATES[model_idx]
            try:
                kwargs = {
                    "response_mime_type": "application/json",
                    "max_output_tokens": max_output_tokens,
                }
                if schema:
                    kwargs["response_schema"] = schema
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(**kwargs),
                    )
                except Exception as schema_exc:
                    msg = str(schema_exc).lower()
                    if schema and ("schema" in msg or "response_schema" in msg or "not supported" in msg):
                        kwargs.pop("response_schema", None)
                        response = client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config=types.GenerateContentConfig(**kwargs),
                        )
                    else:
                        raise
                parsed = json.loads(response.text)
                current_api_index = api_idx
                current_model_index = model_idx
                return parsed
            except Exception as exc:
                print(f"{COLOR_YELLOW}{task_label} failed on {key_label} + {model_name}: {exc}{COLOR_END}", file=sys.stderr)
    current_api_index = 0
    return None


def coerce_briefings_list(parsed_json):
    if isinstance(parsed_json, list):
        return [item for item in parsed_json if isinstance(item, dict)]
    if isinstance(parsed_json, dict):
        for key in ("briefings", "slides", "papers", "items", "results", "data"):
            value = parsed_json.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if parsed_json.get("id") or parsed_json.get("background"):
            return [parsed_json]
    return []


def fallback_briefing(slide):
    summary = strip_html(slide.get("summary", ""))
    reason = strip_html(slide.get("reason") or "keyword/Gemini passed")
    return {
        "background": "자동 브리핑을 생성하지 못했습니다. 초록과 원문 링크를 기준으로 빠르게 확인하세요.",
        "motivation": "초록 정보만으로는 분야 내부의 구체적 동기를 안정적으로 요약하기 어렵습니다.",
        "result": summary[:900] if summary else "초록이 RSS에 포함되어 있지 않습니다.",
        "implication": "필터가 이 논문을 통과시킨 이유는 관련 키워드, 저자, 또는 Gemini relevance score 때문입니다.",
        "novelty": reason,
        "unresolved": "원문 확인 후 열린 질문을 판단해야 합니다.",
        "future_work": "후속 실험, 독립 재현, 관련 물질/조건 확장이 가능한지 확인하세요.",
        "concepts": [],
        "english_summary": [
            "The detailed AI briefing was unavailable for this paper.",
            "Use the abstract, score reason, and original link for a quick first pass.",
        ],
    }


def normalize_briefing(item, slide):
    base = fallback_briefing(slide)
    if not isinstance(item, dict):
        return base
    for key in ("background", "motivation", "result", "implication", "novelty", "unresolved", "future_work"):
        value = item.get(key)
        if value:
            base[key] = strip_html(str(value))

    english = item.get("english_summary") or []
    if isinstance(english, str):
        english = [line.strip(" -") for line in english.splitlines() if line.strip(" -")]
    if isinstance(english, list):
        cleaned = [strip_html(str(line)) for line in english if strip_html(str(line))]
        if cleaned:
            base["english_summary"] = cleaned[:5]

    concepts = item.get("concepts") or []
    cleaned_concepts = []
    if isinstance(concepts, list):
        for concept in concepts[:4]:
            if not isinstance(concept, dict):
                continue
            term = strip_html(str(concept.get("term", "")))
            if not term:
                continue
            cleaned_concepts.append({
                "term": term,
                "why_emerged": strip_html(str(concept.get("why_emerged", ""))),
                "origin": strip_html(str(concept.get("origin", ""))),
                "connections": strip_html(str(concept.get("connections", ""))),
            })
    base["concepts"] = cleaned_concepts
    return base


def build_prompt(batch):
    private_profile = (os.getenv("RSS_USER_PROFILE") or "Private reader profile is not configured.").strip()
    payload = []
    for idx, slide in enumerate(batch, start=1):
        payload.append({
            "id": f"S{idx}",
            "title": strip_html(slide.get("title", "")),
            "journal": strip_html(slide.get("journal", "")),
            "authors": strip_html(slide.get("authors", "")),
            "last_authors": strip_html(slide.get("last_authors", "")),
            "score": str(slide.get("score", "")),
            "tier": strip_html(slide.get("tier", "")),
            "reason": strip_html(slide.get("reason", "")),
            "tags": [strip_html(str(t)) for t in (slide.get("tags") or [])[:8]],
            "abstract": strip_html(slide.get("summary", ""))[:5000],
            "link": slide.get("link", ""),
        })
    return f"""
You prepare a fast scientific reading briefing for one researcher.

Private reader profile, supplied through secrets:
{private_profile}

Task:
For each paper below, write a compact but useful Korean briefing that helps the reader understand the paper quickly.
Use the title, abstract, journal, tags, score reason, and your general scientific knowledge.
Do not invent paper-specific results that are not supported by the title/abstract. If evidence is thin, say so explicitly in Korean.

Audience:
The reader is a solid-state physicist, but may know nothing about this paper's subfield.

For every paper, return these fields:
- background: Korean. Explain why this research field exists and what broad problem it tries to solve.
- motivation: Korean. Explain this specific paper's motivation within that field.
- result: Korean. Focus on what previous problem/unknown this paper appears to solve or clarify.
- implication: Korean. Explain what follows if the result is right.
- novelty: Korean. Explain why this is important or new, and tie to the private reader profile when relevant.
- unresolved: Korean. Remaining open questions or caveats.
- future_work: Korean. Expected next experiments/calculations/applications.
- concepts: Korean explanations for up to 4 important concepts/terms from the title/abstract. For each concept include:
  * why_emerged: why the concept was needed historically or practically.
  * origin: who first proposed or experimentally established it when widely known; if uncertain, say "초록 정보만으로는 특정하기 어려움".
  * connections: related earlier/later theories, experiments, or concepts.
- english_summary: 3 to 5 concise English lines summarizing the whole paper.

Return JSON only. Shape:
[
  {{
    "id": "S1",
    "background": "...",
    "motivation": "...",
    "result": "...",
    "implication": "...",
    "novelty": "...",
    "unresolved": "...",
    "future_work": "...",
    "concepts": [
      {{"term": "...", "why_emerged": "...", "origin": "...", "connections": "..."}}
    ],
    "english_summary": ["...", "..."]
  }}
]

Papers:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def generate_briefings(slides):
    if not slides:
        return {}
    if (os.getenv("RSS_ENABLE_SLIDE_BRIEFINGS") or "1").strip().lower() in ("0", "false", "no"):
        return {}
    try:
        limit = int(os.getenv("RSS_SLIDE_BRIEFING_LIMIT", "40"))
    except Exception:
        limit = 40
    try:
        batch_size = max(1, min(5, int(os.getenv("RSS_SLIDE_BRIEFING_BATCH_SIZE", "3"))))
    except Exception:
        batch_size = 3

    target_slides = slides[:max(0, limit)]
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "id": {"type": "STRING"},
                "background": {"type": "STRING"},
                "motivation": {"type": "STRING"},
                "result": {"type": "STRING"},
                "implication": {"type": "STRING"},
                "novelty": {"type": "STRING"},
                "unresolved": {"type": "STRING"},
                "future_work": {"type": "STRING"},
                "concepts": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "term": {"type": "STRING"},
                            "why_emerged": {"type": "STRING"},
                            "origin": {"type": "STRING"},
                            "connections": {"type": "STRING"},
                        },
                    },
                },
                "english_summary": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        },
    }

    out = {}
    total_batches = math.ceil(len(target_slides) / batch_size) if target_slides else 0
    for start in range(0, len(target_slides), batch_size):
        batch = target_slides[start:start + batch_size]
        batch_num = start // batch_size + 1
        print(f"{COLOR_BLUE}Building slide briefing batch {batch_num}/{total_batches}{COLOR_END}", file=sys.stderr)
        parsed = generate_json_with_gemini(
            build_prompt(batch),
            schema=schema,
            max_output_tokens=12288,
            task_label=f"slide briefing batch {batch_num}",
        )
        items = coerce_briefings_list(parsed)
        by_id = {f"S{i}": slide for i, slide in enumerate(batch, start=1)}
        for item in items:
            slide = by_id.get(str(item.get("id", "")).strip())
            if slide:
                out[slide.get("link") or slide.get("title", "")] = normalize_briefing(item, slide)
        for slide in batch:
            key = slide.get("link") or slide.get("title", "")
            if key not in out:
                out[key] = fallback_briefing(slide)
    return out


def render_html(slides):
    slides_json = json.dumps(slides, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>Daily Paper Slideshow</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f6f7f4;
    --paper: #ffffff;
    --ink: #111827;
    --muted: #64748b;
    --line: #d8ddd2;
    --accent: #176b87;
    --accent-2: #9a3412;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; overflow: auto; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }}
  .shell {{ max-width: 1500px; min-height: 100vh; margin: 0 auto; padding: 14px 18px; display: flex; flex-direction: column; }}
  .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }}
  .top-actions {{ display: flex; align-items: center; gap: 10px; }}
  .topbar a, button {{ border: 1px solid var(--line); border-radius: 8px; background: var(--paper); color: var(--ink); padding: 10px 14px; font-weight: 700; text-decoration: none; cursor: pointer; }}
  button.primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
  button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  .counter {{ color: var(--muted); font-weight: 700; }}
  .slide {{ min-height: calc(100vh - 82px); background: var(--paper); border: 1px solid var(--line); border-radius: 10px; padding: 18px; box-shadow: 0 18px 42px rgba(15, 23, 42, 0.10); display: grid; grid-template-columns: minmax(310px, 0.86fr) minmax(0, 1.55fr); gap: 18px; overflow: visible; }}
  .paper-head {{ min-width: 0; min-height: 0; overflow: visible; display: flex; flex-direction: column; gap: 12px; }}
  .figure {{ margin: 0; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #f8fafc; }}
  .figure img {{ display: block; width: 100%; max-height: 41vh; object-fit: contain; background: white; }}
  .figure figcaption {{ padding: 7px 10px; color: var(--muted); font-size: 12px; line-height: 1.3; }}
  .head-text {{ display: flex; flex-direction: column; gap: 9px; min-width: 0; }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  .badge {{ border-radius: 999px; padding: 5px 9px; background: #eef2ff; color: #3730a3; font-size: 12px; font-weight: 800; }}
  .score {{ background: #fee2e2; color: #991b1b; }}
  h1 {{ margin: 0; font-size: 25px; line-height: 1.15; letter-spacing: 0; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }}
  h1 a {{ color: inherit; text-decoration-color: rgba(23, 107, 135, 0.35); text-decoration-thickness: 2px; text-underline-offset: 4px; }}
  h1 a:hover {{ color: var(--accent); text-decoration-color: var(--accent); }}
  .meta, .authors, .why, .abstract {{ font-size: 13px; line-height: 1.45; }}
  .meta, .authors {{ color: var(--muted); }}
  .why strong, .abstract strong, .authors strong {{ color: var(--ink); }}
  .authors {{ display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
  .why {{ border-left: 4px solid var(--accent-2); padding-left: 10px; color: #374151; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
  .abstract {{ border-top: 1px solid var(--line); padding-top: 14px; color: #374151; }}
  .original-abstract {{ border-top: 1px solid var(--line); padding-top: 10px; color: #374151; font-size: 11.5px; line-height: 1.35; overflow-wrap: anywhere; }}
  .original-abstract strong {{ display: block; margin-bottom: 4px; color: var(--ink); }}
  .tags {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .tag {{ border-radius: 999px; padding: 4px 8px; background: #f1f5f9; color: #475569; font-size: 12px; font-weight: 700; }}
  .head-text > .link {{ display: none; }}
  .briefing {{ min-width: 0; min-height: 0; overflow: visible; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); grid-auto-rows: minmax(0, auto); align-content: start; gap: 11px 18px; border-top: 0; padding-top: 0; }}
  .brief-section {{ min-width: 0; }}
  .brief-section.wide {{ grid-column: 1 / -1; }}
  .brief-section h2 {{ margin: 0 0 4px; font-size: 14px; line-height: 1.22; color: #0f766e; letter-spacing: 0; }}
  .brief-section p, .brief-section li {{ margin: 0; font-size: 12.5px; line-height: 1.42; color: #1f2937; overflow-wrap: anywhere; }}
  .brief-section ul {{ margin: 0; padding-left: 20px; }}
  .concepts {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 14px; }}
  .concept {{ border-top: 1px solid var(--line); padding-top: 7px; }}
  .concept h3 {{ margin: 0 0 4px; font-size: 13px; color: #7c2d12; letter-spacing: 0; }}
  .concept p {{ margin: 2px 0 0; font-size: 11.5px; line-height: 1.35; color: #374151; overflow-wrap: anywhere; }}
  .concept b {{ color: #111827; }}
  .link {{ color: var(--accent); font-weight: 800; text-decoration: none; }}
  .empty {{ padding: 48px; background: var(--paper); border: 1px solid var(--line); border-radius: 12px; color: var(--muted); }}
  @media (max-width: 860px) {{
    .shell {{ padding: 14px; }}
    .topbar, .top-actions {{ align-items: stretch; flex-direction: column; }}
    .slide, .briefing, .concepts {{ grid-template-columns: 1fr; }}
    .slide {{ min-height: auto; overflow: visible; display: grid; }}
    h1 {{ font-size: 28px; }}
  }}
</style>
</head>
<body>
<main class='shell'>
  <div class='topbar'>
    <a href='briefing.html'>Back to Briefing</a>
    <div class='top-actions'><button id='prev'>Prev</button><div class='counter' id='counter'></div><button class='primary' id='next'>Next</button></div>
  </div>
  <section class='slide' id='slide' aria-live='polite'></section>
</main>
<script>
const slides = {slides_json};
let index = 0;
const slideEl = document.getElementById('slide');
const counterEl = document.getElementById('counter');
const prevBtn = document.getElementById('prev');
const nextBtn = document.getElementById('next');
function textEl(tag, className, text) {{ const el = document.createElement(tag); if (className) el.className = className; el.textContent = text || ''; return el; }}
function appendLabeledText(parent, className, label, text) {{ if (!text) return; const el = document.createElement('p'); el.className = className; const strong = textEl('strong', '', label); el.appendChild(strong); el.append(text); parent.appendChild(el); }}
function appendBriefSection(parent, title, text, wide = false) {{ if (!text) return; const section = document.createElement('section'); section.className = 'brief-section' + (wide ? ' wide' : ''); section.appendChild(textEl('h2', '', title)); section.appendChild(textEl('p', '', text)); parent.appendChild(section); }}
function render() {{
  slideEl.replaceChildren();
  if (!slides.length) {{ slideEl.className = 'empty'; slideEl.textContent = 'No papers passed the filters in this run.'; counterEl.textContent = '0 / 0'; prevBtn.disabled = true; nextBtn.disabled = true; return; }}
  slideEl.className = 'slide';
  const r = slides[index];
  const b = r.briefing || {{}};
  const head = document.createElement('div');
  head.className = 'paper-head';
  if (r.image) {{
    const figure = document.createElement('figure');
    figure.className = 'figure';
    const img = document.createElement('img');
    img.src = r.image; img.alt = 'Figure or article preview image'; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.onerror = () => figure.remove();
    figure.appendChild(img);
    figure.appendChild(textEl('figcaption', '', 'Figure / article preview image from the source page'));
    head.appendChild(figure);
  }}
  const headText = document.createElement('div');
  headText.className = 'head-text';
  const badges = document.createElement('div');
  badges.className = 'badges';
  if (r.score) badges.appendChild(textEl('span', 'badge score', `${{r.score}}/10`));
  if (r.tier) badges.appendChild(textEl('span', 'badge', r.tier));
  if (r.journal) badges.appendChild(textEl('span', 'badge', r.journal));
  if (r.source) badges.appendChild(textEl('span', 'badge', r.source));
  headText.appendChild(badges);
  const title = document.createElement('h1');
  if (r.link) {{ const titleLink = document.createElement('a'); titleLink.href = r.link; titleLink.target = '_blank'; titleLink.rel = 'noopener noreferrer'; titleLink.textContent = r.title || ''; title.appendChild(titleLink); }}
  else {{ title.textContent = r.title || ''; }}
  headText.appendChild(title);
  headText.appendChild(textEl('p', 'meta', [r.journal, r.source].filter(Boolean).join(' | ')));
  if (r.last_authors || r.authors) {{ const authors = document.createElement('p'); authors.className = 'authors'; if (r.last_authors) authors.append('Last authors: ' + r.last_authors); if (r.authors) authors.append((r.last_authors ? ' | ' : '') + 'Authors: ' + r.authors); headText.appendChild(authors); }}
  appendLabeledText(headText, 'why', 'Filter reason: ', r.reason);
  if (r.tags && r.tags.length) {{ const tags = document.createElement('div'); tags.className = 'tags'; r.tags.forEach(tag => tags.appendChild(textEl('span', 'tag', '#' + tag))); headText.appendChild(tags); }}
  if (r.link) {{ const link = document.createElement('a'); link.className = 'link'; link.href = r.link; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Open paper'; headText.appendChild(link); }}
  head.appendChild(headText);
  if (r.summary) {{ const abstract = document.createElement('p'); abstract.className = 'original-abstract'; const strong = textEl('strong', '', 'Original abstract'); abstract.appendChild(strong); abstract.append(r.summary); head.appendChild(abstract); }}
  slideEl.appendChild(head);
  const briefing = document.createElement('div');
  briefing.className = 'briefing';
  appendBriefSection(briefing, 'Background', b.background);
  appendBriefSection(briefing, 'Motivation', b.motivation);
  appendBriefSection(briefing, 'Result', b.result);
  appendBriefSection(briefing, 'Implication', b.implication);
  appendBriefSection(briefing, "Why it's important / Novelty", b.novelty);
  appendBriefSection(briefing, 'Unresolved Questions', b.unresolved);
  appendBriefSection(briefing, 'Expected Future Work', b.future_work);
  if (b.concepts && b.concepts.length) {{
    const section = document.createElement('section');
    section.className = 'brief-section wide';
    section.appendChild(textEl('h2', '', 'Concepts / Terms'));
    const concepts = document.createElement('div');
    concepts.className = 'concepts';
    b.concepts.forEach(c => {{
      const item = document.createElement('div');
      item.className = 'concept';
      item.appendChild(textEl('h3', '', c.term || 'Concept'));
      [['왜 생겼나: ', c.why_emerged], ['누가 만들었나: ', c.origin], ['연결되는 흐름: ', c.connections]].forEach(([label, text]) => {{ if (!text) return; const p = document.createElement('p'); const bold = textEl('b', '', label); p.appendChild(bold); p.append(text); item.appendChild(p); }});
      concepts.appendChild(item);
    }});
    section.appendChild(concepts);
    briefing.appendChild(section);
  }}
  if (b.english_summary && b.english_summary.length) {{ const section = document.createElement('section'); section.className = 'brief-section wide'; section.appendChild(textEl('h2', '', 'English Summary')); const ul = document.createElement('ul'); b.english_summary.slice(0, 5).forEach(line => ul.appendChild(textEl('li', '', line))); section.appendChild(ul); briefing.appendChild(section); }}
  slideEl.appendChild(briefing);
  counterEl.textContent = `${{index + 1}} / ${{slides.length}}`;
  prevBtn.disabled = index === 0;
  nextBtn.disabled = index === slides.length - 1;
}}
prevBtn.addEventListener('click', () => {{ index = Math.max(0, index - 1); render(); }});
nextBtn.addEventListener('click', () => {{ index = Math.min(slides.length - 1, index + 1); render(); }});
document.addEventListener('keydown', event => {{ if (event.key === 'ArrowLeft') prevBtn.click(); if (event.key === 'ArrowRight') nextBtn.click(); }});
render();
</script>
</body>
</html>"""


def main():
    slides = extract_slides("slides.html")
    if not slides:
        print("No generated slides.html found to enhance.")
        return
    for slide in slides:
        slide["summary"] = full_abstract_for_slide(slide)
        slide["image"] = image_for_slide(slide)
    briefings = generate_briefings(slides)
    for slide in slides:
        key = slide.get("link") or slide.get("title", "")
        slide["briefing"] = briefings.get(key) or fallback_briefing(slide)
    with open("slides.html", "w", encoding="utf-8") as handle:
        handle.write(render_html(slides))
    print(f"Enhanced slides.html with {len(slides)} paper briefings.")


if __name__ == "__main__":
    main()
