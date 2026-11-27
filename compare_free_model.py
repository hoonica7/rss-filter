import contextlib
import json
import io
import os
import re
import sys
import time
from collections import defaultdict

import requests

MODEL = "gemini-4-hydrogen"
url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
def smoke():
    return requests.post(url, headers={"x-goog-api-key": os.environ["GOOGLE_API_KEY1"]},
                  json={"contents": [{"parts": [{"text": "Reply only OK"}]}],
                        "generationConfig": {"maxOutputTokens": 256, "thinkingConfig": {"thinkingLevel":"LOW"}}},
                  timeout=100)
r = smoke()
if r.status_code == 503:
    time.sleep(8)
    r = smoke()
print("Hydrogen smoke HTTP", r.status_code, flush=True)
if not r.ok:
    err = r.json().get("error", {})
    print("API status", err.get("status"))
    for detail in err.get("details", []):
        for violation in detail.get("violations", []):
            print("Limit", violation.get("quotaId"), violation.get("quotaValue"))
    sys.exit(1)
data = r.json()
answer = [p.get("text", "").strip().upper() for c in data.get("candidates", [])
          for p in c.get("content", {}).get("parts", []) if not p.get("thought")]
if not any("OK" in a for a in answer):
    sys.exit("No smoke answer")
print("Smoke answer confirmed; model", data.get("modelVersion"), flush=True)
with io.StringIO() as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    import run_filter_resilient as runner
    rss = runner.rss
    runner.install_resilient_feed_fetch()
    assert rss.MODEL_CANDIDATES == [MODEL]
    groups = defaultdict(list)
    provided = {}
    for i,(journal,link,*fields) in enumerate(json.loads(os.environ["SAMPLE"]),1):
        if journal == "skip":
            continue
        if fields:
            title,summary = fields
            assert title and summary
            provided[link] = dict(link=link,title=title,summary=summary)
        groups[journal].append((i, link))
    results = []
    for journal, sample in groups.items():
        by_link = dict(provided)
        if any(u not in by_link for _,u in sample):
            response = requests.get(rss.JOURNAL_URLS[journal], timeout=30)
            response.raise_for_status()
            entries = rss.feedparser.parse(response.content).entries
            by_link.update({rss.get_entry_link(e): e for e in entries})
        selected = [by_link[u] for _,u in sample if u in by_link]
        time.sleep(15)
        passed, removed, pending, meta = rss.classify_entries_with_gemini(journal, selected)
        kept = {rss.get_entry_link(e) for e in passed}
        for i,u in sample:
            if u in meta:
                results.append((i,meta[u]["score"],"pass" if u in kept else "remove"))
            else:
                results.append((i,-1,"pending" if u in by_link else "absent"))
    codes = re.findall(r"HTTP (\d{3})",sink.getvalue())
print("Classification error codes:",",".join(codes))
for i,score,status in sorted(results):
    print("SHADOW", i, score, status)
