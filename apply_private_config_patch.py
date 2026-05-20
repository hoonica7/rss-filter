"""Move private RSS ranking configuration out of public code.

This script is intentionally value-free: it adds environment-variable hooks
and UI helpers, but it does not contain the reader's private profile, author
whitelist, or interest keywords. Those values are supplied through GitHub
Actions secrets.
"""

from pathlib import Path
import re


TARGET = Path("Filter_RSS.py")


PRIVATE_CONFIG_BLOCK = r'''def load_secret_lines(env_name, default=None):
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return list(default or [])
    lines = []
    for piece in re.split(r'[\n;]+', raw):
        item = piece.strip()
        if item and not item.startswith("#"):
            lines.append(item)
    return lines or list(default or [])


def load_secret_combo_rules(env_name, default=None):
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return list(default or [])
    try:
        loaded = json.loads(raw)
        rules = []
        if isinstance(loaded, dict):
            loaded = loaded.items()
        for item in loaded:
            if isinstance(item, dict):
                anchor = item.get("anchor")
                partners = item.get("partners", [])
            else:
                anchor, partners = item
            if anchor and isinstance(partners, list):
                rules.append((str(anchor).lower(), [str(p).lower() for p in partners if str(p).strip()]))
        return rules
    except Exception as e:
        print(f"{COLOR_YELLOW}⚠ Could not parse {env_name}; using generic defaults: {e}{COLOR_END}", file=sys.stderr)
        return list(default or [])


NARROW_TITLE_AUTOPASS = load_secret_lines("RSS_NARROW_TITLE_AUTOPASS")
IMPORTANT_AUTHOR_WHITELIST = load_secret_lines("RSS_AUTHOR_WHITELIST")

DIRECT_RELEVANCE_KEYWORDS = load_secret_lines("RSS_DIRECT_RELEVANCE_KEYWORDS", [
    "condensed matter", "quantum materials", "electronic structure", "spectroscopy",
    "Weyl", "Dirac", "Berry curvature", "anomalous Hall", "altermagnet",
    "topological", "superconduct", "quantum Hall", "spin liquid", "van der Waals",
])

A_MUST_TRIGGER_KEYWORDS = load_secret_lines("RSS_A_MUST_TRIGGER_KEYWORDS")

A_MUST_COMBO_RULES = load_secret_combo_rules("RSS_A_MUST_COMBO_RULES_JSON")
'''


AUTHOR_MATCHING_BLOCK = r'''
def normalize_author_name(name):
    """Stable key for matching author whitelist aliases across feed formats."""
    clean = strip_html(name or "")
    clean = re.sub(r'\([^)]*\)', ' ', clean)
    clean = clean.replace('-', ' ')
    clean = re.sub(r'[^A-Za-z0-9]+', '', clean).lower()
    return clean


def author_name_keys(name):
    """Return keys for both 'First Last' and 'Last, First' author spellings."""
    clean = strip_html(name or "")
    keys = {normalize_author_name(clean)}
    if ',' in clean:
        last, rest = clean.split(',', 1)
        keys.add(normalize_author_name(f"{rest} {last}"))
    return {k for k in keys if k}


@lru_cache(maxsize=1)
def important_author_lookup():
    lookup = {}
    for name in IMPORTANT_AUTHOR_WHITELIST:
        for key in author_name_keys(name):
            lookup[key] = name
    return lookup


def find_whitelisted_author(authors):
    lookup = important_author_lookup()
    for author in authors:
        for key in author_name_keys(author):
            if key in lookup:
                return author
    return None

'''


AUTHOR_PASS_BLOCK = r'''
        whitelisted_author = find_whitelisted_author(get_authors(entry))
        if whitelisted_author:
            tags = tag_keywords(title, summary)
            score = 10 if has_a_must_trigger(entry) else 9
            keyword_passed_entries.append(entry)
            meta_by_link[link] = {
                "tier": score_to_tier(score),
                "score": score,
                "reason": f"author whitelist: {whitelisted_author}",
                "tags": tags or ["authorWhitelist"],
            }
            print(f"  ✅ [{score}] {title} (author whitelist: {whitelisted_author})", file=sys.stderr)
            continue

'''


GEMINI_PROMPT_FUNCTION = r'''def build_gemini_prompt(journal_name):
    threshold = get_threshold(journal_name)
    classifier_role = (os.getenv("RSS_CLASSIFIER_ROLE") or "You are ranking scientific papers for a researcher reading a scientific RSS feed.").strip()
    user_profile = (os.getenv("RSS_USER_PROFILE") or """
- Prioritize papers that match the private reader profile supplied in repository secrets.
- If no private profile is configured, keep broadly relevant condensed-matter and quantum-materials papers.
- Goal: morning skim feed. Missing a relevant paper is worse than keeping a few extras.
""").strip()
    scoring_policy = (os.getenv("RSS_SCORING_POLICY") or """
10 = direct hit for the private reader profile or current projects.
9 = very direct but not perfect: close to the private profile with clear experimental or materials relevance.
7-8 = important condensed-matter/quantum-materials paper worth keeping. NOT A-level unless directly connected to the private profile.
4-6 = adjacent condensed matter or theory watch; keep if uncertainty is meaningful. Includes: theory of real materials with experimental implications, methodology/instrumentation papers, computational materials discovery.
1-3 = mostly unrelated formal theory, generic quantum information, toy models without material context, soft matter, photonics without CM/materials relevance.
0 = clearly unrelated biology, medicine, climate, astronomy, chemistry synthesis without CM physics, news/editorial/correction.

THEORY POLICY:
- Do not over-score theory just because it says topological, Majorana, Floquet, Krylov, Kitaev, Chern, quantum, or graphene.
- A_MUST_READ requires direct private-profile/project relevance, not merely being a good condensed-matter paper.
- Put broad but interesting condensed-matter papers in B_IMPORTANT_CONDMAT, not A_MUST_READ.
- Theory scores 7-8 only if it is closely tied to a real material system or experimental observable.
- Theory scores 4-6 if it is plausibly relevant to interpreting CM experiments.
- Theory scores 1-3 if it is purely formal.
""").strip()
    if journal_name in ["PRL_Recent", "PRB_Recent", "arXiv_CondMat"]:
        scope = (
            "This source is noisy because it contains many formal theory papers. "
            "Be selective. Generic quantum information, high-energy, cosmology, cold atom, generic Majorana, "
            "abstract Krylov/Floquet/SYK/tensor-network papers should usually score 0-3 unless they connect clearly "
            "to the private reader profile, real condensed-matter materials, experimental observables, or electronic structure."
        )
    else:
        scope = (
            "This source is a broad high-impact journal feed. Remove biology/medicine/climate/astronomy/news, "
            "but keep significant condensed-matter/materials/quantum materials papers."
        )

    return f"""
{classifier_role}

USER PROFILE:
{user_profile}

SOURCE POLICY:
{scope}
Journal/source: {journal_name}
RSS pass threshold for this source: score >= {threshold}/10.

SCORING RUBRIC AND POLICY:
{scoring_policy}

OUTPUT:
Return a JSON array only. One object per article:
{{
  "title": "exact input title",
  "score": integer 0-10,
  "decision": "YES" or "NO",
  "tier": "A_MUST_READ" | "B_IMPORTANT_CONDMAT" | "C_MAYBE" | "D_ARCHIVE",
  "reason": "one short phrase under 18 words",
  "tags": ["method", "material", "topic"]
}}
Use decision YES iff score >= {threshold}. If unsure but plausibly relevant, give 4-6 rather than 0-3. Use A_MUST_READ only for direct private-profile/project relevance; otherwise use B_IMPORTANT_CONDMAT even for excellent broad condensed-matter papers.

Articles:
"""
'''


SLIDESHOW_FUNCTION = r'''def create_slideshow_html(records):
    """Create a slide-by-slide reading view for today's passed papers."""
    def score_value(r):
        try:
            return float(r.get('score', 0) or 0)
        except Exception:
            return 0

    sorted_records = sorted(
        records,
        key=lambda r: (tier_rank(r.get('tier', '')), -score_value(r), r.get('journal', ''), r.get('title', '')),
    )
    slides = []
    for r in sorted_records:
        slides.append({
            "title": strip_html(r.get('title', 'No title')),
            "journal": strip_html(r.get('journal', '')),
            "source": strip_html(r.get('source', '')),
            "link": r.get('link', ''),
            "authors": strip_html(r.get('authors', '')),
            "last_authors": strip_html(r.get('last_authors', '')),
            "summary": strip_html(r.get('summary', '')),
            "tier": strip_html(r.get('tier', '')),
            "score": str(r.get('score', '')),
            "reason": strip_html(r.get('reason') or 'keyword/Gemini passed'),
            "tags": [strip_html(str(t)) for t in (r.get('tags') or [])[:8]],
        })
    slides_json = json.dumps(slides, ensure_ascii=False).replace('</', '<\\/')

    html_doc = f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>Daily Paper Slideshow</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #eef2f7;
    --paper: #ffffff;
    --ink: #111827;
    --muted: #64748b;
    --line: #dbe3ef;
    --accent: #2563eb;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--ink);
  }}
  .shell {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
  .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }}
  .topbar a, button {{
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--paper);
    color: var(--ink);
    padding: 10px 14px;
    font-weight: 700;
    text-decoration: none;
    cursor: pointer;
  }}
  button.primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
  button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  .counter {{ color: var(--muted); font-weight: 700; }}
  .slide {{
    min-height: calc(100vh - 154px);
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: clamp(22px, 4vw, 48px);
    box-shadow: 0 20px 50px rgba(15, 23, 42, 0.10);
    display: flex;
    flex-direction: column;
    gap: 18px;
  }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  .badge {{ border-radius: 999px; padding: 6px 10px; background: #eef2ff; color: #3730a3; font-size: 13px; font-weight: 800; }}
  .score {{ background: #fee2e2; color: #991b1b; }}
  h1 {{ margin: 0; font-size: clamp(30px, 5vw, 58px); line-height: 1.05; letter-spacing: 0; max-width: 18ch; }}
  .meta, .authors, .why, .abstract {{ font-size: clamp(15px, 1.8vw, 19px); line-height: 1.55; }}
  .meta, .authors {{ color: var(--muted); }}
  .why strong, .abstract strong, .authors strong {{ color: var(--ink); }}
  .abstract {{
    border-top: 1px solid var(--line);
    padding-top: 18px;
    max-width: 88ch;
  }}
  .tags {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .tag {{ border-radius: 999px; padding: 5px 9px; background: #f1f5f9; color: #475569; font-size: 13px; font-weight: 700; }}
  .link {{ color: var(--accent); font-weight: 800; text-decoration: none; }}
  .controls {{ display: flex; justify-content: space-between; gap: 12px; margin-top: 16px; }}
  .empty {{ padding: 48px; background: var(--paper); border: 1px solid var(--line); border-radius: 12px; color: var(--muted); }}
  @media (max-width: 720px) {{
    .shell {{ padding: 14px; }}
    .topbar, .controls {{ align-items: stretch; flex-direction: column; }}
    h1 {{ max-width: none; }}
    .slide {{ min-height: auto; }}
  }}
</style>
</head>
<body>
<main class='shell'>
  <div class='topbar'>
    <a href='briefing.html'>Back to Briefing</a>
    <div class='counter' id='counter'></div>
  </div>
  <section class='slide' id='slide' aria-live='polite'></section>
  <div class='controls'>
    <button id='prev'>Prev</button>
    <button class='primary' id='next'>Next</button>
  </div>
</main>
<script>
const slides = {slides_json};
let index = 0;
const slideEl = document.getElementById('slide');
const counterEl = document.getElementById('counter');
const prevBtn = document.getElementById('prev');
const nextBtn = document.getElementById('next');

function textEl(tag, className, text) {{
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text || '';
  return el;
}}

function render() {{
  slideEl.replaceChildren();
  if (!slides.length) {{
    slideEl.className = 'empty';
    slideEl.textContent = 'No papers passed the filters in this run.';
    counterEl.textContent = '0 / 0';
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    return;
  }}
  slideEl.className = 'slide';
  const r = slides[index];
  const badges = document.createElement('div');
  badges.className = 'badges';
  if (r.score) badges.appendChild(textEl('span', 'badge score', `${{r.score}}/10`));
  if (r.tier) badges.appendChild(textEl('span', 'badge', r.tier));
  if (r.journal) badges.appendChild(textEl('span', 'badge', r.journal));
  if (r.source) badges.appendChild(textEl('span', 'badge', r.source));
  slideEl.appendChild(badges);

  slideEl.appendChild(textEl('h1', '', r.title));
  slideEl.appendChild(textEl('p', 'meta', [r.journal, r.source].filter(Boolean).join(' | ')));

  if (r.last_authors || r.authors) {{
    const authors = document.createElement('p');
    authors.className = 'authors';
    authors.textContent = '';
    if (r.last_authors) authors.append('Last authors: ' + r.last_authors);
    if (r.authors) authors.append((r.last_authors ? ' | ' : '') + 'Authors: ' + r.authors);
    slideEl.appendChild(authors);
  }}

  if (r.reason) {{
    const why = document.createElement('p');
    why.className = 'why';
    const strong = textEl('strong', '', 'Why: ');
    why.appendChild(strong);
    why.append(r.reason);
    slideEl.appendChild(why);
  }}

  if (r.tags && r.tags.length) {{
    const tags = document.createElement('div');
    tags.className = 'tags';
    r.tags.forEach(tag => tags.appendChild(textEl('span', 'tag', '#' + tag)));
    slideEl.appendChild(tags);
  }}

  if (r.summary) {{
    const abstract = document.createElement('p');
    abstract.className = 'abstract';
    const strong = textEl('strong', '', 'Abstract: ');
    abstract.appendChild(strong);
    abstract.append(r.summary);
    slideEl.appendChild(abstract);
  }}

  if (r.link) {{
    const link = document.createElement('a');
    link.className = 'link';
    link.href = r.link;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'Open paper';
    slideEl.appendChild(link);
  }}

  counterEl.textContent = `${{index + 1}} / ${{slides.length}}`;
  prevBtn.disabled = index === 0;
  nextBtn.disabled = index === slides.length - 1;
}}

prevBtn.addEventListener('click', () => {{ index = Math.max(0, index - 1); render(); }});
nextBtn.addEventListener('click', () => {{ index = Math.min(slides.length - 1, index + 1); render(); }});
document.addEventListener('keydown', event => {{
  if (event.key === 'ArrowLeft') prevBtn.click();
  if (event.key === 'ArrowRight') nextBtn.click();
}});
render();
</script>
</body>
</html>"""
    with open('slides.html', 'w', encoding='utf-8') as f:
        f.write(html_doc)


'''


def replace_between(text, start, end, replacement):
    start_idx = text.index(start)
    end_idx = text.index(end, start_idx)
    return text[:start_idx] + replacement.rstrip() + "\n\n" + text[end_idx:]


def patch_text(text):
    if "RSS_PRIVATE_CONFIG_PATCH_APPLIED" in text:
        return text

    text = replace_between(text, "# RSS filter v6 policy:", "THEORY_OVERPROMOTION_HINTS = [", PRIVATE_CONFIG_BLOCK)

    if "def normalize_author_name(name):" not in text:
        text = text.replace("\n\ndef score_to_tier(score):", "\n" + AUTHOR_MATCHING_BLOCK + "\ndef score_to_tier(score):")

    if "whitelisted_author = find_whitelisted_author" not in text:
        text = text.replace("\n        # Hard pre-filter:", "\n" + AUTHOR_PASS_BLOCK + "        # Hard pre-filter:")

    text = re.sub(
        r"Decision rule \(validated.*?10 monitored journals\):",
        "Decision rule tuned against a private relevance library and the monitored journal mix:",
        text,
        flags=re.S,
    )

    text = replace_between(text, "def build_gemini_prompt(journal_name):", "\n\ndef serialize_entry_for_pending(entry):", GEMINI_PROMPT_FUNCTION + "\n\n")

    if "def create_slideshow_html(records):" not in text:
        text = text.replace("\n\ndef tier_rank(tier):", "\n\n" + SLIDESHOW_FUNCTION.rstrip() + "\n\ndef tier_rank(tier):")

    if "Open Slideshow" not in text:
        text = text.replace(
            "    <p class='text-slate-600 mt-2'>Fast skim page: A/B papers are listed; C/D are summarized. Full pass/fail archive remains in the audit page.</p>\n",
            "    <p class='text-slate-600 mt-2'>Fast skim page: A/B papers are listed; C/D are summarized. Full pass/fail archive remains in the audit page.</p>\n"
            "    <div class='flex flex-wrap gap-3 mt-4'>\n"
            "      <a href='slides.html' target='_blank' class='inline-flex items-center px-4 py-2 bg-blue-600 text-white font-semibold rounded hover:bg-blue-700'>Open Slideshow</a>\n"
            "      <a href='filtered_results.html' target='_blank' class='inline-flex items-center px-4 py-2 bg-slate-700 text-white font-semibold rounded hover:bg-slate-800'>Audit Page</a>\n"
            "    </div>\n",
        )

    text = re.sub(
        r"Journal-specific RSS feeds filtered for .*? relevance\.",
        "Journal-specific RSS feeds filtered for private research relevance.",
        text,
    )
    if "Daily Slideshow" not in text:
        text = text.replace(
            "<a href='briefing.html' target='_blank' class='block w-full px-6 py-4 bg-rose-600 text-white font-semibold rounded-lg shadow-md hover:bg-rose-700'>Daily Briefing</a>\n",
            "<a href='briefing.html' target='_blank' class='block w-full px-6 py-4 bg-rose-600 text-white font-semibold rounded-lg shadow-md hover:bg-rose-700'>Daily Briefing</a>\n"
            "<a href='slides.html' target='_blank' class='block w-full px-6 py-4 bg-blue-600 text-white font-semibold rounded-lg shadow-md hover:bg-blue-700'>Daily Slideshow</a>\n",
        )

    text = text.replace(
        "                        briefing_records.append(paper_record(entry, journal_name, 'keyword', meta))",
        "                        reason = (meta.get(get_entry_link(entry), {}) or {}).get('reason', '')\n"
        "                        source = 'author whitelist' if reason.startswith('author whitelist:') else 'keyword'\n"
        "                        briefing_records.append(paper_record(entry, journal_name, source, meta))",
    )
    if "create_slideshow_html(briefing_records)" not in text:
        text = text.replace(
            "        create_briefing_html(briefing_records, email_content)\n",
            "        create_briefing_html(briefing_records, email_content)\n"
            "        create_slideshow_html(briefing_records)\n",
        )

    marker = "# RSS_PRIVATE_CONFIG_PATCH_APPLIED\n"
    if marker not in text:
        text = text.replace("# Filter_RSS v12\n", "# Filter_RSS v12\n" + marker, 1)
    return text


def main():
    original = TARGET.read_text(encoding="utf-8")
    updated = patch_text(original)
    if updated == original:
        print("Private config patch already applied.")
        return
    TARGET.write_text(updated, encoding="utf-8")
    print("Applied private config patch.")


if __name__ == "__main__":
    main()
