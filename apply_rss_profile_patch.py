"""Apply the user-profile RSS customizations before the CI run.

The GitHub app can update small files directly, while the main RSS script is
large enough that applying this focused patch in the workflow is more reliable
from Codex. The patch is idempotent and exits cleanly once the target markers
are already present.
"""

from pathlib import Path
import subprocess
import sys


PATCH = r'''diff --git a/Filter_RSS.py b/Filter_RSS.py
index 988f147..127269b 100644
--- a/Filter_RSS.py
+++ b/Filter_RSS.py
@@ -68,6 +68,51 @@ NARROW_TITLE_AUTOPASS = [
     "TaNiTe2", "NbNiTe2", "112 telluride"
 ]
 
+# If any author matches one of these normalized names/aliases, the paper is
+# kept without a Gemini call. This is deliberately conservative: mostly ARPES
+# group leaders and beamline/synchrotron scientists whose papers the user is
+# likely to want in the morning skim.
+IMPORTANT_AUTHOR_WHITELIST = [
+    "Takeshi Kondo",
+    "Philip D. C. King", "Phil King", "P. D. C. King",
+    "Zhi-Xun Shen", "Z.-X. Shen", "Z X Shen", "ZX Shen",
+    "Ming Yi",
+    "Yeongkwan Kim", "Yeokngkwan Kim", "Y. K. Kim",
+    "Hong Ding",
+    "Donghui Lu", "D. H. Lu",
+    "Makoto Hashimoto",
+    "Eli Rotenberg",
+    "Aaron Bostwick",
+    "Chris Jozwiak",
+    "Jonathan D. Denlinger", "J. D. Denlinger",
+    "Changyoung Kim",
+    "Shuyun Zhou",
+    "Yulin Chen", "Y. L. Chen",
+    "M. Zahid Hasan", "M. Z. Hasan", "Zahid Hasan",
+    "Ming Shi",
+    "Takafumi Sato",
+    "Takeshi Mizokawa",
+    "Kozo Okazaki",
+    "Daisuke Shiga",
+    "Sung-Kwan Mo",
+    "Robert J. Birgeneau",
+    "Andrea Damascelli",
+    "Nuh Gedik",
+    "Riccardo Comin",
+    "Kyle M. Shen",
+    "J. C. Seamus Davis", "Seamus Davis",
+    "Ali Yazdani",
+    "Peter D. Johnson", "P. D. Johnson",
+    "Thomas Valla", "T. Valla",
+    "Daniel S. Dessau", "D. S. Dessau",
+    "Adam Kaminski",
+    "J. C. Campuzano", "Juan Carlos Campuzano",
+    "Rui-Hua He", "R. H. He",
+    "Xiangjun Zhou", "X. J. Zhou",
+    "Donglai Feng", "D. L. Feng",
+    "Shik Shin",
+]
+
 DIRECT_RELEVANCE_KEYWORDS = [
     # Spectroscopy techniques
     "ARPES", "angle-resolved photoemission", "photoemission", "magnetoARPES", "CD-ARPES",
@@ -483,6 +528,43 @@ def author_metadata_text(authors):
     return f"Last authors: {last_authors}; Authors: {compact}"
 
 
+def normalize_author_name(name):
+    """Stable key for matching author whitelist aliases across feed formats."""
+    clean = strip_html(name or "")
+    clean = re.sub(r'\([^)]*\)', ' ', clean)
+    clean = clean.replace('-', ' ')
+    clean = re.sub(r'[^A-Za-z0-9]+', '', clean).lower()
+    return clean
+
+
+def author_name_keys(name):
+    """Return keys for both 'First Last' and 'Last, First' author spellings."""
+    clean = strip_html(name or "")
+    keys = {normalize_author_name(clean)}
+    if ',' in clean:
+        last, rest = clean.split(',', 1)
+        keys.add(normalize_author_name(f"{rest} {last}"))
+    return {k for k in keys if k}
+
+
+@lru_cache(maxsize=1)
+def important_author_lookup():
+    lookup = {}
+    for name in IMPORTANT_AUTHOR_WHITELIST:
+        for key in author_name_keys(name):
+            lookup[key] = name
+    return lookup
+
+
+def find_whitelisted_author(authors):
+    lookup = important_author_lookup()
+    for author in authors:
+        for key in author_name_keys(author):
+            if key in lookup:
+                return author
+    return None
+
+
 
 def score_to_tier(score):
     try:
@@ -1611,6 +1693,20 @@ def filter_rss_for_journal(journal_name, feed_url, pending_records=None):
             print(f"  ✅ [{score}] {title} (title strong match: {autopass_kw})", file=sys.stderr)
             continue
 
+        whitelisted_author = find_whitelisted_author(get_authors(entry))
+        if whitelisted_author:
+            tags = tag_keywords(title, summary)
+            score = 10 if has_a_must_trigger(entry) else 9
+            keyword_passed_entries.append(entry)
+            meta_by_link[link] = {
+                "tier": score_to_tier(score),
+                "score": score,
+                "reason": f"author whitelist: {whitelisted_author}",
+                "tags": tags or ["authorWhitelist"],
+            }
+            print(f"  ✅ [{score}] {title} (author whitelist: {whitelisted_author})", file=sys.stderr)
+            continue
+
         # Hard pre-filter: kill biology/medicine/climate/cosmology before
         # spending Gemini API calls on them. This list is curated to avoid
         # blocking physics terms — see HARD_REJECT_KEYWORDS comment.
@@ -1728,6 +1824,223 @@ def create_results_html_file(email_body_content):
         f.write('\n'.join(html_parts))
 
 
+def create_slideshow_html(records):
+    """Create a slide-by-slide reading view for today's passed papers."""
+    def score_value(r):
+        try:
+            return float(r.get('score', 0) or 0)
+        except Exception:
+            return 0
+
+    sorted_records = sorted(
+        records,
+        key=lambda r: (tier_rank(r.get('tier', '')), -score_value(r), r.get('journal', ''), r.get('title', '')),
+    )
+    slides = []
+    for r in sorted_records:
+        slides.append({
+            "title": strip_html(r.get('title', 'No title')),
+            "journal": strip_html(r.get('journal', '')),
+            "source": strip_html(r.get('source', '')),
+            "link": r.get('link', ''),
+            "authors": strip_html(r.get('authors', '')),
+            "last_authors": strip_html(r.get('last_authors', '')),
+            "summary": strip_html(r.get('summary', '')),
+            "tier": strip_html(r.get('tier', '')),
+            "score": str(r.get('score', '')),
+            "reason": strip_html(r.get('reason') or 'keyword/Gemini passed'),
+            "tags": [strip_html(str(t)) for t in (r.get('tags') or [])[:8]],
+        })
+    slides_json = json.dumps(slides, ensure_ascii=False).replace('</', '<\\/')
+
+    html_doc = f"""<!DOCTYPE html>
+<html lang='en'>
+<head>
+<meta charset='UTF-8'>
+<meta name='viewport' content='width=device-width, initial-scale=1.0'>
+<title>Daily Paper Slideshow</title>
+<style>
+  :root {{
+    color-scheme: light;
+    --bg: #eef2f7;
+    --paper: #ffffff;
+    --ink: #111827;
+    --muted: #64748b;
+    --line: #dbe3ef;
+    --accent: #2563eb;
+  }}
+  * {{ box-sizing: border-box; }}
+  body {{
+    margin: 0;
+    min-height: 100vh;
+    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
+    background: var(--bg);
+    color: var(--ink);
+  }}
+  .shell {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
+  .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }}
+  .topbar a, button {{
+    border: 1px solid var(--line);
+    border-radius: 8px;
+    background: var(--paper);
+    color: var(--ink);
+    padding: 10px 14px;
+    font-weight: 700;
+    text-decoration: none;
+    cursor: pointer;
+  }}
+  button.primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
+  button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
+  .counter {{ color: var(--muted); font-weight: 700; }}
+  .slide {{
+    min-height: calc(100vh - 154px);
+    background: var(--paper);
+    border: 1px solid var(--line);
+    border-radius: 12px;
+    padding: clamp(22px, 4vw, 48px);
+    box-shadow: 0 20px 50px rgba(15, 23, 42, 0.10);
+    display: flex;
+    flex-direction: column;
+    gap: 18px;
+  }}
+  .badges {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
+  .badge {{ border-radius: 999px; padding: 6px 10px; background: #eef2ff; color: #3730a3; font-size: 13px; font-weight: 800; }}
+  .score {{ background: #fee2e2; color: #991b1b; }}
+  h1 {{ margin: 0; font-size: clamp(30px, 5vw, 58px); line-height: 1.05; letter-spacing: 0; max-width: 18ch; }}
+  .meta, .authors, .why, .abstract {{ font-size: clamp(15px, 1.8vw, 19px); line-height: 1.55; }}
+  .meta, .authors {{ color: var(--muted); }}
+  .why strong, .abstract strong, .authors strong {{ color: var(--ink); }}
+  .abstract {{
+    border-top: 1px solid var(--line);
+    padding-top: 18px;
+    max-width: 88ch;
+  }}
+  .tags {{ display: flex; flex-wrap: wrap; gap: 8px; }}
+  .tag {{ border-radius: 999px; padding: 5px 9px; background: #f1f5f9; color: #475569; font-size: 13px; font-weight: 700; }}
+  .link {{ color: var(--accent); font-weight: 800; text-decoration: none; }}
+  .controls {{ display: flex; justify-content: space-between; gap: 12px; margin-top: 16px; }}
+  .empty {{ padding: 48px; background: var(--paper); border: 1px solid var(--line); border-radius: 12px; color: var(--muted); }}
+  @media (max-width: 720px) {{
+    .shell {{ padding: 14px; }}
+    .topbar, .controls {{ align-items: stretch; flex-direction: column; }}
+    h1 {{ max-width: none; }}
+    .slide {{ min-height: auto; }}
+  }}
+</style>
+</head>
+<body>
+<main class='shell'>
+  <div class='topbar'>
+    <a href='briefing.html'>Back to Briefing</a>
+    <div class='counter' id='counter'></div>
+  </div>
+  <section class='slide' id='slide' aria-live='polite'></section>
+  <div class='controls'>
+    <button id='prev'>Prev</button>
+    <button class='primary' id='next'>Next</button>
+  </div>
+</main>
+<script>
+const slides = {slides_json};
+let index = 0;
+const slideEl = document.getElementById('slide');
+const counterEl = document.getElementById('counter');
+const prevBtn = document.getElementById('prev');
+const nextBtn = document.getElementById('next');
+
+function textEl(tag, className, text) {{
+  const el = document.createElement(tag);
+  if (className) el.className = className;
+  el.textContent = text || '';
+  return el;
+}}
+
+function render() {{
+  slideEl.replaceChildren();
+  if (!slides.length) {{
+    slideEl.className = 'empty';
+    slideEl.textContent = 'No papers passed the filters in this run.';
+    counterEl.textContent = '0 / 0';
+    prevBtn.disabled = true;
+    nextBtn.disabled = true;
+    return;
+  }}
+  slideEl.className = 'slide';
+  const r = slides[index];
+  const badges = document.createElement('div');
+  badges.className = 'badges';
+  if (r.score) badges.appendChild(textEl('span', 'badge score', `${{r.score}}/10`));
+  if (r.tier) badges.appendChild(textEl('span', 'badge', r.tier));
+  if (r.journal) badges.appendChild(textEl('span', 'badge', r.journal));
+  if (r.source) badges.appendChild(textEl('span', 'badge', r.source));
+  slideEl.appendChild(badges);
+
+  slideEl.appendChild(textEl('h1', '', r.title));
+  slideEl.appendChild(textEl('p', 'meta', [r.journal, r.source].filter(Boolean).join(' | ')));
+
+  if (r.last_authors || r.authors) {{
+    const authors = document.createElement('p');
+    authors.className = 'authors';
+    authors.textContent = '';
+    if (r.last_authors) authors.append('Last authors: ' + r.last_authors);
+    if (r.authors) authors.append((r.last_authors ? ' | ' : '') + 'Authors: ' + r.authors);
+    slideEl.appendChild(authors);
+  }}
+
+  if (r.reason) {{
+    const why = document.createElement('p');
+    why.className = 'why';
+    const strong = textEl('strong', '', 'Why: ');
+    why.appendChild(strong);
+    why.append(r.reason);
+    slideEl.appendChild(why);
+  }}
+
+  if (r.tags && r.tags.length) {{
+    const tags = document.createElement('div');
+    tags.className = 'tags';
+    r.tags.forEach(tag => tags.appendChild(textEl('span', 'tag', '#' + tag)));
+    slideEl.appendChild(tags);
+  }}
+
+  if (r.summary) {{
+    const abstract = document.createElement('p');
+    abstract.className = 'abstract';
+    const strong = textEl('strong', '', 'Abstract: ');
+    abstract.appendChild(strong);
+    abstract.append(r.summary);
+    slideEl.appendChild(abstract);
+  }}
+
+  if (r.link) {{
+    const link = document.createElement('a');
+    link.className = 'link';
+    link.href = r.link;
+    link.target = '_blank';
+    link.rel = 'noopener noreferrer';
+    link.textContent = 'Open paper';
+    slideEl.appendChild(link);
+  }}
+
+  counterEl.textContent = `${{index + 1}} / ${{slides.length}}`;
+  prevBtn.disabled = index === 0;
+  nextBtn.disabled = index === slides.length - 1;
+}}
+
+prevBtn.addEventListener('click', () => {{ index = Math.max(0, index - 1); render(); }});
+nextBtn.addEventListener('click', () => {{ index = Math.min(slides.length - 1, index + 1); render(); }});
+document.addEventListener('keydown', event => {{
+  if (event.key === 'ArrowLeft') prevBtn.click();
+  if (event.key === 'ArrowRight') nextBtn.click();
+}});
+render();
+</script>
+</body>
+</html>"""
+    with open('slides.html', 'w', encoding='utf-8') as f:
+        f.write(html_doc)
+
+
 def tier_rank(tier):
     order = {'A_MUST_READ': 0, 'B_IMPORTANT_CONDMAT': 1, 'C_MAYBE': 2, 'C_MAYBE_UNCLASSIFIED': 3, '': 4}
     return order.get(tier, 4)
@@ -1801,6 +2114,10 @@ def create_briefing_html(records, email_body_content=''):
   <div class='bg-white rounded-2xl shadow-xl p-8'>
     <h1 class='text-3xl font-bold text-slate-900'>[hoonica RSS] Morning Paper Briefing</h1>
     <p class='text-slate-600 mt-2'>Fast skim page: A/B papers are listed; C/D are summarized. Full pass/fail archive remains in the audit page.</p>
+    <div class='flex flex-wrap gap-3 mt-4'>
+      <a href='slides.html' target='_blank' class='inline-flex items-center px-4 py-2 bg-blue-600 text-white font-semibold rounded hover:bg-blue-700'>Open Slideshow</a>
+      <a href='filtered_results.html' target='_blank' class='inline-flex items-center px-4 py-2 bg-slate-700 text-white font-semibold rounded hover:bg-slate-800'>Audit Page</a>
+    </div>
     <div class='grid grid-cols-1 md:grid-cols-4 gap-4 my-6'>
       <div class='p-4 rounded-xl bg-red-50'><div class='text-2xl font-bold'>{len(a_items)}</div><div class='text-sm text-slate-600'>A Must Read</div></div>
       <div class='p-4 rounded-xl bg-amber-50'><div class='text-2xl font-bold'>{len(b_items)}</div><div class='text-sm text-slate-600'>B Important CM</div></div>
@@ -1847,6 +2164,7 @@ def create_index_html(journal_urls, rss_base_filename):
 <p class='text-gray-600 mb-8'>Journal-specific RSS feeds filtered for condensed matter / ARPES relevance.</p>
 <div class='space-y-4'>
 <a href='briefing.html' target='_blank' class='block w-full px-6 py-4 bg-rose-600 text-white font-semibold rounded-lg shadow-md hover:bg-rose-700'>Daily Briefing</a>
+<a href='slides.html' target='_blank' class='block w-full px-6 py-4 bg-blue-600 text-white font-semibold rounded-lg shadow-md hover:bg-blue-700'>Daily Slideshow</a>
 """
     for journal_name in journal_urls.keys():
         filename = f"{rss_base_filename}_{journal_name}.xml"
@@ -1934,7 +2252,9 @@ if __name__ == '__main__':
                 else:
                     for entry in keyword_passed:
                         email_content += f"  ✅ {display_title_for_entry(entry, journal_name)} ({get_entry_link(entry) or 'No link'})\n"
-                        briefing_records.append(paper_record(entry, journal_name, 'keyword', meta))
+                        reason = (meta.get(get_entry_link(entry), {}) or {}).get('reason', '')
+                        source = 'author whitelist' if reason.startswith('author whitelist:') else 'keyword'
+                        briefing_records.append(paper_record(entry, journal_name, source, meta))
                     for entry in gemini_passed:
                         email_content += f"  🤖✅ {display_title_for_entry(entry, journal_name)} ({get_entry_link(entry) or 'No link'})\n"
                         briefing_records.append(paper_record(entry, journal_name, 'Gemini', meta))
@@ -1990,6 +2310,7 @@ if __name__ == '__main__':
         create_index_html(JOURNAL_URLS, OUTPUT_FILE_BASE)
         create_results_html_file(email_content)
         create_briefing_html(briefing_records, email_content)
+        create_slideshow_html(briefing_records)
         clear_partial_state()
     finally:
         github_server_url = os.getenv('GITHUB_SERVER_URL')
'''


def main():
    target = Path("Filter_RSS.py")
    text = target.read_text(encoding="utf-8")
    if "IMPORTANT_AUTHOR_WHITELIST" in text and "create_slideshow_html" in text:
        print("RSS profile patch already applied.")
        return

    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn"],
        input=PATCH,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    print("Applied RSS profile patch.")


if __name__ == "__main__":
    main()
