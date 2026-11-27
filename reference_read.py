"""Select reference material without changing the main classifier's decisions."""
import html
import json
import os
import re
import unicodedata
from functools import lru_cache


def normalize(text):
    plain = re.sub(r'<[^>]+>', ' ', html.unescape(text or ''))
    plain = unicodedata.normalize('NFKC', plain).casefold()
    return ' '.join(plain.replace('\u2013', '-').replace('\u2212', '-').split())


@lru_cache(maxsize=1)
def private_patterns(raw):
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
        return tuple(re.compile(data[key]) for key in ('topic', 'use')
                     if isinstance(data[key], str) and data[key].strip())
    except (ValueError, TypeError, KeyError, re.error):
        raise ValueError('Invalid RSS_REFERENCE_RULES_JSON') from None


def is_reference_read(title, summary):
    patterns = private_patterns(os.getenv('RSS_REFERENCE_RULES_JSON', ''))
    if patterns is None:
        return False
    if len(patterns) != 2:
        raise ValueError('Missing topic or use pattern in RSS_REFERENCE_RULES_JSON')
    title, abstract = normalize(title), normalize(summary)
    if re.match(r'(author correction|correction|erratum|retraction)\b', title):
        return False
    titled = (re.search(r'\b(colloquium|essay|tutorial|primer|practical guide|roadmap|review|overview)\b', title)
              or re.match(r'^(?:a |an )?perspective (?:on\b|:)', title))
    if re.search(r'\b(peer review|review process|under review)\b', title):
        titled = False
    declared = re.search(
        r'\b(in this (?:review|perspective|colloquium|tutorial|essay)|'
        r'(?:we|this (?:paper|article|work))\s+(?:briefly\s+)?review(?:s)?\b|'
        r'(?:pedagogical|comprehensive|topical|tutorial) review|'
        r'(?:methods|progress|advances|literature)\b[^.]{0,80}\b(?:are|is) reviewed)\b', abstract)
    return bool((titled or declared) and all(p.search(title + ' ' + abstract) for p in patterns))
