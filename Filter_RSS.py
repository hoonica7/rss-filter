# Filter_RSS v11
# Builds on v10 with seven correctness/perf fixes:
#   1. Curated HARD_REJECT_KEYWORDS pre-filter restored (saves Gemini calls).
#   2. postprocess_score_and_tier demotes tier only — never the score —
#      so above-threshold papers stay in RSS (recall preserved) while the
#      A_MUST_READ / briefing list is reserved for direct project hits.
#   3. Title normalization for Gemini response → input matching, so HTML
#      entities (<sub>, &amp; etc.) no longer cause papers to get stuck
#      in the pending queue forever.
#   4. lru_cache on og:image fetcher and a real Chrome User-Agent string,
#      so each article URL is fetched at most once per run and publishers
#      are less likely to 403.
#   5. Synthetic RSS-insert path now supports Atom and RDF feeds, not just
#      RSS 2.0. arXiv pending papers can now make it into the feed.
#   6. ensure_description_prefix uses the correct namespace for RSS 1.0
#      (arXiv) — previously the enrichment was written into a dangling
#      no-namespace <description> that readers ignored.
#   7. Pending queue is preserved across resume runs (was being clobbered
#      on a successful resume that did not re-process all journals).

import feedparser
import lxml.etree as ET
import requests
from io import BytesIO
import sys
import os
import time
import json
from google import genai
from google.genai import types
import datetime
import re
import math
import html
from functools import lru_cache
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

A_MUST_TRIGGER_KEYWORDS = [
    "ARPES", "angle-resolved photoemission", "photoemission", "magnetoARPES", "CD-ARPES",
    "momentum-resolved spectroscopy", "quantum twisting microscope",
    "AV3Sb5", "CsV3Sb5", "RbV3Sb5", "KV3Sb5", "V3Sb5",
    "NbSe3", "spin-charge separation", "Luttinger liquid",
    "TaNiTe2", "NbNiTe2", "112 telluride",
]

A_MUST_COMBO_RULES = [
    ("kagome", ["cdw", "charge density wave", "nematic", "loop current", "trsb", "time-reversal",
                "berry curvature", "anomalous hall", "flat band", "weyl", "dirac", "semimetal"]),
    ("altermagnet", ["arpes", "photoemission", "transport", "spin-orbit torque", "terahertz", "magneto", "band", "splitting"]),
    ("topological semimetal", ["anomalous hall", "berry curvature", "transport", "magnet", "arpes", "photoemission"]),
]

THEORY_OVERPROMOTION_HINTS = [
    "krylov", "syk", "tensor network", "holographic", "conformal field theory", "quantum information",
    "measurement-induced", "majorana wire", "majorana zero modes", "surface code", "gottesman",
    "kitaev chain", "non-hermitian", "exceptional point", "higher-dimensional", "abstract",
]

BROAD_CONDMAT_KEYWORDS = [
    "superconduct", "correlated", "Mott", "Hubbard", "moiré", "twisted", "graphene", "topological", "Chern", "flat band",
    "spin liquid", "magnon", "phonon", "quantum critical", "Kondo", "van der Waals", "ferromagnet", "antiferromagnet",
    "quantum geometry", "Hall effect", "transport", "Fermi surface", "band structure", "Landau level"
]

# =============================================================================
# HARD_REJECT_KEYWORDS — papers whose title or abstract contains any of these
# (whole-word, case-insensitive) are dropped BEFORE Gemini classification.
#
# Curated to avoid blocking physics papers:
#   * "neural" REMOVED → matches "neural network", "neural quantum states"
#     used in ML-for-physics work that the user may want to see.
#   * "genetic" REMOVED → matches "genetic algorithm".
#   * "forest" REMOVED → matches "random forest" ML method.
#   * "quark"/"gluon"/"hadron" NOT included → may appear in interdisciplinary
#     high-impact papers (e.g., muon g-2 in Nature).
#   * "dendrite" NOT included → matches "Li dendrite" battery papers.
#   * "axion"/"neutrino"/"holography" NOT included → can be CMP topics.
#   * "oxygen"/"cell" NOT included → "oxygen vacancy", "unit cell".
#
# Plural / adjectival variants are listed explicitly because the matcher
# uses word boundaries (\b...\b) — without these, "genome" would not match
# "genomes" or "genomic".
# =============================================================================
HARD_REJECT_KEYWORDS = [
    # ---- Cancer / oncology ----
    "cancer", "cancers", "carcinoma", "carcinomas", "carcinogen", "carcinogenic",
    "tumor", "tumors", "tumour", "tumours",
    "metastasis", "metastases", "metastatic",
    "leukemia", "leukaemia", "lymphoma",
    "oncology", "oncological", "chemotherapy",

    # ---- Infectious disease ----
    "virus", "viruses", "viral",
    "vaccine", "vaccines", "vaccination",
    "infection", "infections", "infectious",
    "pathogen", "pathogens", "pathogenic",
    "bacteria", "bacterial", "bacterium",
    "antibiotic", "antibiotics",
    "epidemic", "pandemic", "epidemiology", "epidemiological",

    # ---- Molecular biology ----
    "genome", "genomes", "genomic", "genomics",
    "transcriptome", "transcriptomes", "transcriptomic", "transcriptomics",
    "proteome", "proteomic", "proteomics",
    "mRNA", "miRNA", "lncRNA", "ncRNA", "tRNA", "siRNA",
    "CRISPR", "Cas9",
    "phenotype", "phenotypes", "phenotypic",
    "genotype", "genotypes",
    "epigenetic", "epigenetics", "epigenome",
    "methylation",
    "antibody", "antibodies", "antigen", "antigens",
    "cytokine", "cytokines", "interleukin", "chemokine",

    # ---- Cell types / anatomy / tissues ----
    # NB: bare "cell" / "neural" / "dendrite" omitted — would match physics terms.
    "neuron", "neurons", "neuronal",
    "synapse", "synapses", "synaptic",
    "astrocyte", "astrocytes",
    "glia", "glial", "microglia", "microglial",
    "hippocampus", "hippocampal",
    "neocortex", "neocortical",
    "embryo", "embryos", "embryonic",
    "fetus", "fetuses", "fetal",
    "myosin", "actin", "kinase", "cytoskeleton", "cytoskeletal", "ribosome",

    # ---- Organisms ----
    "mouse", "mice", "rat", "rats",
    "zebrafish", "drosophila", "yeast",
    "fungus", "fungi", "fungal",
    "Salmonella", "octopus", "octopuses",

    # ---- Medicine / clinical ----
    "patient", "patients", "clinical", "diagnosis", "diagnostic", "diagnoses",
    "therapy", "therapies", "therapeutic", "therapeutics",
    "drug", "drugs", "pharmacology", "pharmacological", "pharmaceutical",
    "biopsy", "biopsies", "disease", "diseases",
    "in vivo", "in-vivo", "in vitro", "in-vitro", "ex vivo", "ex-vivo",
    "Alzheimer", "Parkinson", "diabetes", "diabetic",
    "obesity", "obese", "asthma",
    "psoriasis", "schizophrenia",
    "cardiovascular", "myocardial", "ischemia", "ischemic", "stroke",

    # ---- Immune system ----
    "immune", "immunity", "immunology", "immunological",
    "autoimmune", "autoimmunity",
    "inflammation", "inflammatory",

    # ---- Cognitive / behavioral / social ----
    "psychology", "psychological", "psychiatric",
    "behavioral", "behavioural",

    # ---- Earth / climate / ecology ----
    "climate", "climatic",
    "ecosystem", "ecosystems",
    "biodiversity", "habitat", "habitats",
    "wildfire", "wildfires", "deforestation",
    "glacier", "glaciers", "glaciation", "permafrost",
    "volcano", "volcanoes", "volcanic",
    "earthquake", "earthquakes", "tsunami", "tectonic",
    "monsoon", "hurricane", "hurricanes", "cyclone", "cyclones",
    "pollinator", "pollinators", "pesticide", "pesticides",
    "agriculture", "agricultural",
    "lava", "archeology", "archaeology", "archaeological", "mummy",
    "fishery", "fisheries",

    # ---- Cosmology / astrophysics ----
    "exoplanet", "exoplanets",
    "galaxy", "galaxies", "galactic",
    "supernova", "supernovae",
    "interstellar", "asteroid", "asteroids", "comet", "comets",
    "dark matter", "dark energy",
    "AdS/CFT",
    "cosmic ray", "cosmic rays", "cosmological", "cosmology",
    "gravitational wave", "gravitational waves",

    # ---- Misc ----
    "microbiome", "microbiomes", "gut",
    "obituary",
]

# Backwards-compatible alias for the Gemini prompt's negative_hints field.
STRONG_NEGATIVE_KEYWORDS = HARD_REJECT_KEYWORDS

# Bio stems that show up as the tail half of compound words and therefore
# don't match the whole-word \b...\b pattern. Use sparingly — substring
# matching has higher false-positive risk than the curated word list above.
# Only include stems where any compound use is reliably bio/medical.
SUBSTRING_REJECT_STEMS = [
    "coronavirus",   # alphacoronavirus, betacoronaviruses, retrovirus-like usage
    "carcinoma",     # adenocarcinoma, hepatocarcinoma
    "sarcoma",       # osteosarcoma
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

# Model order requested by user. Override with repo secret if desired:
#   GEMINI_MODELS=gemini-3-flash-preview,gemini-3.1-flash-lite-preview,gemini-2.5-flash
# NOTE: os.getenv("GEMINI_MODELS", default) returns "" (not the default) if
# the secret exists but is empty, which produced models=[] in earlier runs.
# Treat empty/whitespace explicitly as "use default."
_DEFAULT_MODELS = "gemini-3-flash-preview,gemini-3.1-flash-lite-preview,gemini-2.5-flash"
_models_env = (os.getenv("GEMINI_MODELS") or "").strip()
MODEL_CANDIDATES = [m.strip() for m in (_models_env or _DEFAULT_MODELS).split(',') if m.strip()]
if not MODEL_CANDIDATES:
    MODEL_CANDIDATES = ["gemini-2.5-flash"]

current_model_index = 0
current_model_name = MODEL_CANDIDATES[0]

# =============================================================================
# Gemini API key rotation
# =============================================================================
# The user runs three separate API keys to multiply quota. The retry strategy
# inside a batch is:
#   1. Try every model on the current key.
#   2. If they all fail, switch to the next key (wraparound) and try every
#      model again.
#   3. If all keys × all models fail, defer the batch to the pending queue
#      and reset to API1 for the next batch (quota errors usually clear by
#      then).
# Once a (key, model) combo succeeds, both indices stick for the next batch
# so we don't waste calls re-checking dead endpoints.
# =============================================================================
GOOGLE_API_KEYS = []
for _i in (1, 2, 3):
    _k = os.getenv(f"GOOGLE_API_KEY{_i}")
    if _k:
        GOOGLE_API_KEYS.append((f"KEY{_i}", _k))

# Backwards compatibility: if user hasn't migrated yet, still accept the old
# single-key name. Logged loudly so they know to update.
if not GOOGLE_API_KEYS:
    _legacy = os.getenv("GOOGLE_API_KEY")
    if _legacy:
        GOOGLE_API_KEYS.append(("LEGACY", _legacy))
        print(f"{COLOR_YELLOW}⚠ Using legacy GOOGLE_API_KEY. Migrate to GOOGLE_API_KEY1/2/3 for rotation.{COLOR_END}", file=sys.stderr)

gemini_clients = []           # parallel list of (label, genai.Client) tuples
current_api_index = 0          # persists across batches
try:
    for label, key in GOOGLE_API_KEYS:
        try:
            gemini_clients.append((label, genai.Client(api_key=key)))
        except Exception as e:
            print(f"{COLOR_RED}✗ Failed to init Gemini client for {label}: {e}{COLOR_END}", file=sys.stderr)
    if gemini_clients:
        labels = ",".join(lab for lab, _ in gemini_clients)
        print(f"{COLOR_GREEN}{COLOR_BOLD}✓ Gemini API configured: keys=[{labels}], models={MODEL_CANDIDATES}{COLOR_END}", file=sys.stderr)
    else:
        print(f"{COLOR_YELLOW}⚠ No Gemini API keys found (set GOOGLE_API_KEY1/2/3). Filter will skip Gemini.{COLOR_END}", file=sys.stderr)
except Exception as e:
    print(f"{COLOR_RED}✗ Error configuring Gemini API: {e}{COLOR_END}", file=sys.stderr)


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


def _split_author_string(raw):
    """Split a single combined-authors string into individual names.

    Handles:
      * "A and B"                              → 2 names
      * "A, B, and C"                          → 3 names (Oxford comma + and)
      * "A; B; C"                              → 3 names (semicolon)
      * "A, B, C"                              → 3 names (plain comma)
      * "Smith, A."  /  "Jones, B.J."          → 1 name (Last, First initials)
      * "Smith, A.; Jones, B."                 → 2 names (Last, First; ...)

    The 'Last, First' detection is heuristic: if every comma-separated piece
    is short (<= 4 chars or a sequence of capital letters with dots), the
    commas are treated as part of name pairs rather than separators.
    """
    raw = raw.strip()
    if not raw:
        return []

    # Semicolons are unambiguous — split first.
    if ';' in raw:
        return [p for piece in raw.split(';') for p in _split_author_string(piece) if p]

    # Detect "Last, First" form: alternating Surname, Given-initials chunks.
    # Heuristic: piece looks like initials if it's <= 6 chars and contains a dot.
    pieces = [p.strip() for p in raw.split(',')]
    looks_like_lastfirst = (
        len(pieces) >= 2
        and len(pieces) % 2 == 0
        and all('.' in p or len(p) <= 4 for p in pieces[1::2])  # every other piece looks like initials
    )
    if looks_like_lastfirst:
        out = []
        for i in range(0, len(pieces), 2):
            last = pieces[i]
            first = pieces[i+1] if i+1 < len(pieces) else ''
            # Strip leading "and" from the surname half (Oxford-comma case).
            last = re.sub(r'^and\s+', '', last, flags=re.I)
            if first:
                out.append(f"{last}, {first}")
            elif last:
                out.append(last)
        return [x for x in out if x]

    # Default split: comma OR " and ", honoring Oxford comma.
    parts = re.split(r'\s*,?\s+and\s+|\s*,\s*', raw)
    return [p.strip() for p in parts if p.strip()]


def get_authors(entry):
    authors = []
    if entry.get('authors'):
        for a in entry.get('authors', []):
            name = a.get('name') if isinstance(a, dict) else str(a)
            if name:
                authors.append(strip_html(name))
    if not authors and entry.get('author'):
        authors = [strip_html(entry.get('author'))]
    # Some feeds expose dc:creator as dc_creator or creator.
    for key in ['dc_creator', 'creator']:
        if not authors and entry.get(key):
            authors = [strip_html(entry.get(key))]

    # Re-split each author string. Even if feedparser returned a list of N>1
    # entries, each may itself be a multi-author string (APS/arXiv put all
    # names in one dc:creator; Nature usually doesn't but normalizing is
    # safe). The splitter detects 'Last, First' form to avoid breaking
    # Science-style entries.
    expanded = []
    for raw in authors:
        expanded.extend(_split_author_string(raw))
    authors = expanded

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
    # No more "et al.; last authors:" trailer here — the last-authors line is
    # rendered separately above the author list.
    return ", ".join(authors[:front]) + ", et al."


def last_authors_text(authors):
    """Last 1-2 names from the author list. Plain string, no label.
    Replaces the previous 'corresponding-author proxy' heuristic per user
    request — just take the tail.
    """
    if not authors:
        return ""
    return ", ".join(authors[-2:]) if len(authors) >= 2 else authors[0]



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


def text_for_entry(entry):
    return (strip_html(entry.get('title', '')) + " " + strip_html(entry.get('summary', ''))).lower()


def has_a_must_trigger(entry):
    text = text_for_entry(entry)
    for kw in A_MUST_TRIGGER_KEYWORDS:
        if kw.lower() in text:
            return True
    for anchor, partners in A_MUST_COMBO_RULES:
        if anchor in text and any(p in text for p in partners):
            return True
    return False


def postprocess_score_and_tier(journal_name, entry, score, tier, reason=''):
    """Demote tier (NOT score) for broad/theory papers without a direct hit
    on the user's project keywords. Score is preserved so the RSS-feed
    threshold check below uses Gemini's raw judgment (recall-oriented),
    while tier — which drives the briefing's A list — is reserved for
    direct project relevance.

    Previous behavior (v10) capped score, which inadvertently turned
    tier demotion into a hard filter (score below threshold = removed).
    """
    text = text_for_entry(entry)
    has_a = has_a_must_trigger(entry)

    # Demote A → B if there is no direct project/material hit.
    if not has_a and tier == "A_MUST_READ":
        tier = "B_IMPORTANT_CONDMAT"
        reason = (reason + "; demoted from A: broad CM, not direct user/project hit").strip('; ')

    # Theory overpromotion: cap A or B → C for formal/abstract theory.
    if any(h in text for h in THEORY_OVERPROMOTION_HINTS) and not has_a:
        if tier in ("A_MUST_READ", "B_IMPORTANT_CONDMAT"):
            tier = "C_MAYBE"
        reason = (reason + "; tier capped to C: formal/generic theory watch").strip('; ')

    # arXiv: never A without a direct hit.
    if journal_name == 'arXiv_CondMat' and not has_a and tier == "A_MUST_READ":
        tier = "B_IMPORTANT_CONDMAT"

    return score, tier, reason[:220]


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


def coerce_decisions_list(parsed_json):
    """Normalize Gemini's JSON response to a list of decision dicts.

    Gemini sometimes wraps the array in an object even when prompted for an
    array, e.g.:
        {"articles": [{"title": "...", "score": 8}, ...]}
        {"results": [...]}
        {"decisions": [...]}
        {"items": [...]}
        {"data": [...]}
        [{"title": ...}]                      ← already correct
        {"title": "...", "score": 8}          ← single decision (1-item batch)

    Without this helper, iterating a wrapper dict yields its KEYS as strings,
    which then crash on .get() calls and the whole batch gets deferred to
    pending despite Gemini having actually answered correctly.
    """
    if isinstance(parsed_json, list):
        return [d for d in parsed_json if isinstance(d, dict)]
    if isinstance(parsed_json, dict):
        # Look for the most likely array-valued field.
        for key in ('articles', 'results', 'decisions', 'items', 'data', 'papers', 'entries'):
            v = parsed_json.get(key)
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
        # Fallback: any array-valued field.
        for v in parsed_json.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [d for d in v if isinstance(d, dict)]
        # Last resort: the dict itself looks like a single decision.
        if 'title' in parsed_json or 'score' in parsed_json:
            return [parsed_json]
    return []


def norm_title(s):
    """Aggressive title fingerprint for round-tripping titles through Gemini.

    Gemini may return any of these surface forms for the same paper:
      KTaO<sub>3</sub>   →  KTaO3   |   KTaO_3   |   KTaO₃   |   KTaO 3
      α-RuCl<sub>3</sub> →  α-RuCl3 |   α-RuCl₃  |   α-RuCl 3
      multi-<b>Q</b>     →  multi-Q |   multiQ   |   multi Q
      ${(\\mathrm{Bi})}_{2}{Te}_{3}$ → (Bi)2Te3   (LaTeX commands stripped)
      Title (arXiv:2605.12345)       → Title       (arXiv ID stripped)

    Fingerprint pipeline:
      1. strip HTML tags WITHOUT inserting whitespace,
      2. unescape HTML entities,
      3. drop arXiv IDs and DOIs that may or may not appear in Gemini's reply,
      4. drop LaTeX command tokens (\\mathrm, \\rm, \\text, \\mathbf, ...),
      5. map unicode sub/superscript digits to plain ASCII,
      6. lowercase,
      7. keep only alphanumerics.

    Collision risk inside a single 25-paper batch is effectively zero.
    """
    if not s:
        return ""
    t = re.sub(r'<[^>]+>', '', str(s))     # strip tags WITHOUT a space
    t = html.unescape(t)
    # Drop trailing/parenthetical arXiv IDs and DOIs that Gemini may omit.
    t = re.sub(r'\(arXiv:\s*\S+?\)', '', t, flags=re.I)
    t = re.sub(r'\barxiv:\s*\S+', '', t, flags=re.I)
    t = re.sub(r'\bdoi:\s*\S+', '', t, flags=re.I)
    # Drop LaTeX command tokens like \mathrm, \rm, \text, \mathbf, \mathit, etc.
    t = re.sub(r'\\[a-zA-Z]+', '', t)
    t = t.translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹', '01234567890123456789'))
    t = t.lower()
    t = ''.join(ch for ch in t if ch.isalnum())
    return t


def find_hard_reject(title, summary):
    """Word-boundary match against HARD_REJECT_KEYWORDS, plus substring
    match against SUBSTRING_REJECT_STEMS for stems that appear inside
    compound words (e.g., "alphacoronaviruses"). Returns (keyword, location)
    or (None, None). Title is checked before abstract so the more visible
    signal wins. Hyphens in input are normalized to spaces so multi-word
    keywords like "cosmic ray" match titles like "Cosmic-Ray Acceleration".
    """
    title_text = re.sub(r'-', ' ', strip_html(title or ""))
    abstract_text = re.sub(r'-', ' ', strip_html(summary or ""))

    # Whole-word matches in title.
    for kw in HARD_REJECT_KEYWORDS:
        pattern = r'\b' + re.escape(kw) + r'\b'
        if re.search(pattern, title_text, re.IGNORECASE):
            return kw, "Title"
    # Compound stems in title (e.g., alphacoronaviruses).
    for stem in SUBSTRING_REJECT_STEMS:
        if re.search(re.escape(stem), title_text, re.IGNORECASE):
            return stem, "Title"

    # Whole-word matches in abstract.
    for kw in HARD_REJECT_KEYWORDS:
        pattern = r'\b' + re.escape(kw) + r'\b'
        if re.search(pattern, abstract_text, re.IGNORECASE):
            return kw, "Abstract"
    # Compound stems in abstract.
    for stem in SUBSTRING_REJECT_STEMS:
        if re.search(re.escape(stem), abstract_text, re.IGNORECASE):
            return stem, "Abstract"

    return None, None


def arxiv_html_url(link):
    if not link:
        return None
    m = re.search(r'arxiv\.org/(?:abs|pdf)/([^?#]+)', link)
    if not m:
        return None
    arxiv_id = m.group(1).replace('.pdf', '')
    return f"https://arxiv.org/html/{arxiv_id}"


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@lru_cache(maxsize=512)
def fetch_first_image_from_html(url, timeout=15):
    """Fetch og:image (or first content image) from a URL. Memoized per-run
    so a paper that appears in both ensure_description_prefix and
    paper_record only triggers one HTTP fetch.
    """
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=timeout, headers=_BROWSER_HEADERS, allow_redirects=True)
        if resp.status_code >= 400:
            return None
        html_text = resp.text
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html_text, flags=re.I)
        if not m:
            m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
        if m:
            return urljoin(url, html.unescape(m.group(1)))
        for m in re.finditer(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']', html_text, flags=re.I):
            src = html.unescape(m.group(1))
            if any(skip in src.lower() for skip in ["logo", "icon", "favicon", "avatar", "default-cover", "branding"]):
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
10 = only direct hit for user's current projects: ARPES/magnetoARPES/CD-ARPES, AV3Sb5/CsV3Sb5/RbV3Sb5, kagome CDW/nematicity/loop current/TRSB, NbSe3 spin-charge separation, or 112 tellurides.
9 = very direct but not perfect: electronic-structure spectroscopy, kagome electronic order, magnetic/topological material with direct experimental relevance.
7-8 = important condensed-matter/quantum-materials paper worth keeping, but NOT A-level unless directly connected to the user profile.
4-6 = adjacent condensed matter or theory watch; keep if uncertainty is meaningful.
1-3 = mostly unrelated formal theory, generic quantum information, generic Majorana wires, generic Krylov/Floquet/SYK/tensor network, soft matter, photonics without CM/materials relevance.
0 = clearly unrelated biology, medicine, climate, astronomy, chemistry synthesis without CM physics, news/editorial/correction.

THEORY POLICY:
- Do not over-score theory just because it says topological, Majorana, Floquet, Krylov, Kitaev, Chern, quantum, or graphene.
- A_MUST_READ requires direct user/project relevance, not merely being a good condensed-matter paper.
- Put broad but interesting condensed-matter papers in B_IMPORTANT_CONDMAT, not A_MUST_READ.
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
Use decision YES iff score >= {threshold}. If unsure but plausibly relevant, give 4-6 rather than 0-3. Use A_MUST_READ only for direct user/project relevance; otherwise use B_IMPORTANT_CONDMAT even for excellent broad condensed-matter papers.

Articles:
"""


def serialize_entry_for_pending(entry):
    """Store enough metadata to retry a failed Gemini classification in a later run."""
    return {
        "title": entry.get('title', ''),
        "link": get_entry_link(entry),
        "summary": strip_html(entry.get('summary', '')),
        "authors": get_authors(entry),
        "published": entry.get('published', '') or entry.get('updated', ''),
        "id": entry.get('id', '') or get_entry_link(entry),
    }


def entry_from_pending_record(record):
    """Create a small feedparser-like dict for retrying and, if passed, RSS insertion."""
    authors = record.get('authors') or []
    return {
        "title": record.get('title', ''),
        "link": record.get('link', ''),
        "summary": record.get('summary', ''),
        "authors": [{"name": a} for a in authors],
        "author": "; ".join(authors),
        "published": record.get('published', ''),
        "updated": record.get('published', ''),
        "id": record.get('id', '') or record.get('link', ''),
        "_from_pending_queue": True,
    }


def dedupe_entries_by_link_or_title(entries):
    out = []
    seen = set()
    for e in entries:
        key = get_entry_link(e) or strip_html(e.get('title', '')).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def classify_entries_with_gemini(journal_name, entries):
    global current_model_name, current_model_index, current_api_index
    passed, removed, pending, metadata = [], [], [], {}
    if not entries:
        return passed, removed, pending, metadata
    if not gemini_clients:
        print(f"      {COLOR_YELLOW}⏸ Gemini unavailable (no API keys). Holding {len(entries)} items for next run.{COLOR_END}", file=sys.stderr)
        return passed, removed, list(entries), metadata

    threshold = get_threshold(journal_name)
    base_prompt = build_gemini_prompt(journal_name)
    # Default batch size is 15 (was 25). Smaller batches reduce the chance
    # of Gemini truncating its JSON reply, which silently dumps unmatched
    # papers into the pending queue. 15 papers per batch with a 4500-char
    # cap on each abstract fits well under our 8192 max_output_tokens.
    batch_size = int(os.getenv("GEMINI_BATCH_SIZE", "15"))
    n_apis = len(gemini_clients)
    n_models = len(MODEL_CANDIDATES)

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

        api_success = False
        last_error = None              # (key_label, model_name, exception)
        attempts_log = []              # for visibility when total failure occurs

        # Build the rotation order: start at current API key, wrap around.
        api_attempts = [(current_api_index + i) % n_apis for i in range(n_apis)]
        # Build the model rotation order: start at current model, wrap around.
        model_attempts = [(current_model_index + j) % n_models for j in range(n_models)]

        for api_idx in api_attempts:
            key_label, client = gemini_clients[api_idx]

            for model_idx in model_attempts:
                model_name = MODEL_CANDIDATES[model_idx]
                print(f"      🤖 Trying {key_label} + {model_name}", file=sys.stderr)
                # Build config with response_json_schema as best-effort. Some
                # preview models reject schema, so we retry without it on
                # specific schema-related errors below.
                schema = {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "title":    {"type": "STRING"},
                            "score":    {"type": "INTEGER"},
                            "decision": {"type": "STRING"},
                            "tier":     {"type": "STRING"},
                            "reason":   {"type": "STRING"},
                            "tags":     {"type": "ARRAY", "items": {"type": "STRING"}},
                        },
                    },
                }
                try:
                    response = None
                    try:
                        response = client.models.generate_content(
                            model=model_name,
                            contents=full_prompt,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                response_schema=schema,
                                # Without max_output_tokens explicitly set, the
                                # default may be too small for JSON-array
                                # responses on >15-paper batches, causing the
                                # reply to be truncated mid-array. The truncated
                                # JSON parses but yields fewer decisions than
                                # papers sent, and the missing papers end up
                                # in the pending queue.
                                max_output_tokens=8192,
                            ),
                        )
                    except Exception as schema_err:
                        # Fall back if the model doesn't support response_schema.
                        msg = str(schema_err).lower()
                        if "schema" in msg or "response_schema" in msg or "not supported" in msg:
                            print(f"      {COLOR_ORANGE}↳ {model_name} doesn't support schema; retrying without it.{COLOR_END}", file=sys.stderr)
                            response = client.models.generate_content(
                                model=model_name,
                                contents=full_prompt,
                                config=types.GenerateContentConfig(
                                    response_mime_type="application/json",
                                    max_output_tokens=8192,
                                ),
                            )
                        else:
                            raise
                    parsed = json.loads(response.text)
                    decisions = coerce_decisions_list(parsed)
                    if not decisions:
                        # Gemini returned valid JSON but with no parseable
                        # decisions in any expected shape. Treat as a real
                        # failure for this combo so we move to the next.
                        raise ValueError(f"Gemini returned JSON with no parseable decisions; got top-level type={type(parsed).__name__}, keys={list(parsed)[:5] if isinstance(parsed, dict) else 'N/A'}")
                    # Normalize titles when matching Gemini's reply against
                    # the original batch — Gemini may strip HTML entities or
                    # whitespace, which would otherwise leave items unmatched
                    # and they would loop in the pending queue forever.
                    by_title = {norm_title(e.get('title','')): e for e in batch_entries}
                    used_norms = set()
                    for d in decisions:
                        title = d.get('title','')
                        nt = norm_title(title)
                        entry = by_title.get(nt)
                        if not entry:
                            continue
                        used_norms.add(nt)
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
                        score, tier, reason = postprocess_score_and_tier(journal_name, entry, score, tier, reason)
                        link = get_entry_link(entry)
                        metadata[link] = {"tier": tier, "score": score, "reason": reason, "tags": tags}
                        if score >= threshold:
                            passed.append(entry)
                            print(f"      🤖✅ [{score}] {title} [{tier}]", file=sys.stderr)
                        else:
                            removed.append(entry)
                            print(f"      🤖❌ [{score}] {title} [{tier}]", file=sys.stderr)

                    # Detect truncated responses: if Gemini returned valid
                    # JSON but covered far fewer items than we sent, the
                    # reply was likely cut off by max_output_tokens (or the
                    # model just gave up partway). Treat as a failure for
                    # this combo so the next (key, model) gets a chance.
                    # Threshold: at least 60% coverage. Below that, retry.
                    coverage = len(used_norms) / max(1, len(batch_entries))
                    min_coverage = 0.6
                    if coverage < min_coverage:
                        # Don't promote anything from this partial response
                        # — roll back. We'll try next combo.
                        for d in decisions:
                            link_to_drop = None
                            for e_check in batch_entries:
                                if norm_title(e_check.get('title','')) == norm_title(d.get('title','')):
                                    link_to_drop = get_entry_link(e_check)
                                    break
                            if link_to_drop and link_to_drop in metadata:
                                metadata.pop(link_to_drop, None)
                        # Drop any entries we appended this round.
                        # Rebuild passed/removed by stripping batch members we just added.
                        batch_links = {get_entry_link(e) for e in batch_entries}
                        passed[:] = [p for p in passed if get_entry_link(p) not in batch_links]
                        removed[:] = [r for r in removed if get_entry_link(r) not in batch_links]
                        raise ValueError(
                            f"truncated response: matched {len(used_norms)}/{len(batch_entries)} "
                            f"items (<{int(min_coverage*100)}% coverage)"
                        )

                    # Successful coverage: anything still unmatched is a
                    # genuine miss (Gemini deliberately omitted) — defer it.
                    for entry in batch_entries:
                        if norm_title(entry.get('title','')) not in used_norms:
                            pending.append(entry)
                            print(f"      ⏸ Gemini response missing item. Pending retry: {entry.get('title','')}", file=sys.stderr)

                    # Success — persist this (key, model) combo for next batches.
                    current_api_index = api_idx
                    current_model_index = model_idx
                    current_model_name = model_name
                    api_success = True
                    print(f"      ✅ Gemini batch classified using {key_label} + {model_name}", file=sys.stderr)
                    break  # out of model loop

                except Exception as e:
                    last_error = (key_label, model_name, e)
                    msg = str(e).lower()
                    is_quota = ("429" in msg) or ("resource_exhausted" in msg) or ("quota" in msg) or ("rate limit" in msg)
                    is_unavailable = ("503" in msg) or ("unavailable" in msg) or ("overloaded" in msg)
                    is_model_error = ("404" in msg) or ("not found" in msg) or ("unsupported" in msg)
                    is_auth = ("401" in msg) or ("403" in msg) or ("permission" in msg) or ("api key" in msg)
                    cat = "quota" if is_quota else "unavailable" if is_unavailable else "auth" if is_auth else "model" if is_model_error else "other"
                    attempts_log.append(f"{key_label}+{model_name}:{cat}")
                    print(f"      {COLOR_RED}✗ {key_label} + {model_name} failed [{cat}]: {e}{COLOR_END}", file=sys.stderr)
                    # Don't retry the same combo — try the next model on this key,
                    # or the next key once all models on this key have been tried.
                    continue

            if api_success:
                break  # out of api loop

            # Done with all models on this key.
            if api_idx != api_attempts[-1]:  # not the last key in rotation
                next_label = gemini_clients[api_attempts[(api_attempts.index(api_idx) + 1) % n_apis]][0]
                print(f"      {COLOR_ORANGE}🔁 All models failed on {key_label}; switching to {next_label}{COLOR_END}", file=sys.stderr)

        if not api_success:
            print(f"      {COLOR_YELLOW}⏸ Gemini batch failed after trying all keys × models. Deferring {len(batch_entries)} item(s) to pending.{COLOR_END}", file=sys.stderr)
            print(f"      {COLOR_YELLOW}   Attempts: {' → '.join(attempts_log)}{COLOR_END}", file=sys.stderr)
            if last_error:
                lk, lm, le = last_error
                print(f"      {COLOR_YELLOW}   Last error ({lk} + {lm}): {le}{COLOR_END}", file=sys.stderr)
            pending.extend(batch_entries)
            # All keys exhausted — for the next batch, restart rotation from API1.
            # Quota windows are typically minute-level so a fresh start is sane.
            current_api_index = 0

    return passed, removed, pending, metadata

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
    last_authors = last_authors_text(authors)
    tier = meta.get('tier', '')
    score = meta.get('score', '')
    reason = meta.get('reason', '')
    tags = meta.get('tags', []) or tag_keywords(entry.get('title',''), entry.get('summary',''))
    abstract = strip_html(entry.get('summary', ''))
    image_url = get_article_image(entry, journal_name)

    tag_html = " ".join([f"<span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#{safe_text(t)}</span>" for t in tags])
    score_badge = f"<span style='display:inline-block;padding:4px 9px;border-radius:999px;background:#fee2e2;color:#991b1b;font-weight:700;'>Score {safe_text(str(score))}/10</span>" if score != '' else ""

    # Last authors first (most relevant signal of which group the paper is from),
    # then the full author list. If the full list is short enough that
    # author_compact already equals last_authors verbatim, skip the duplicate.
    prefix_html = ""
    if last_authors:
        prefix_html += f"<p><b>Last authors:</b> {safe_text(last_authors)}</p>"
    if author_compact and author_compact != last_authors:
        prefix_html += f"<p><b>Authors:</b> {safe_text(author_compact)}</p>"
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
    ns_rss1 = 'http://purl.org/rss/1.0/'
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
    elif feed_type == 'rss1':
        # RSS 1.0 (arXiv): description lives in the rss1 default namespace.
        # Without this fix v10 wrote a dangling no-namespace <description>
        # while the original (namespaced) description stayed unchanged,
        # so readers showed the un-enriched abstract.
        desc_el = xml_item.find(f'{{{ns_rss1}}}description')
        if desc_el is None:
            desc_el = ET.SubElement(xml_item, f'{{{ns_rss1}}}description')
        desc_el.text = prefix_html  # CDATA inside namespaced element is iffy; plain HTML escaped via safe_text is fine
        if authors:
            dc_el = xml_item.find(f'{{{ns_dc}}}creator')
            if dc_el is None:
                dc_el = ET.SubElement(xml_item, f'{{{ns_dc}}}creator')
            dc_el.text = author_compact
    else:
        # rss2
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
        if feed_type == 'atom':
            # Some atom feeds (e.g. APS Physical Review series) include
            # multiple <title> nodes per entry, or set type='html' which
            # makes some readers ignore prefix-as-text. We rewrite EVERY
            # atom:title for the entry and force type='text'.
            title_els = xml_item.findall(f'{{{ns_atom}}}title')
            for title_el in title_els:
                if title_el.text and not re.match(r'^\[\d{1,2}\]', strip_html(title_el.text)):
                    title_el.text = f"[{score}] {strip_html(title_el.text)}"
                    title_el.set('type', 'text')
        elif feed_type == 'rss1':
            # APS PRB exposes both <title> and <dc:title>. Reeder and some
            # other readers prefer <dc:title> for display, so prefix BOTH.
            for title_el in xml_item.findall(f'{{{ns_rss1}}}title') + xml_item.findall(f'{{{ns_dc}}}title'):
                if title_el.text and not re.match(r'^\[\d{1,2}\]', strip_html(title_el.text)):
                    title_el.text = f"[{score}] {strip_html(title_el.text)}"
        else:
            title_el = xml_item.find('title')
            if title_el is not None and title_el.text and not re.match(r'^\[\d{1,2}\]', strip_html(title_el.text)):
                title_el.text = f"[{score}] {strip_html(title_el.text)}"


def append_synthetic_rss_item(root, entry, meta, journal_name):
    """Append a passed entry to the output XML when it is no longer in the
    source feed (typical for items that were pending in a previous run and
    Gemini classified successfully this run). Supports rss2, atom, and rss1.
    """
    tag = root.tag
    title_text = strip_html(entry.get('title', 'No title'))
    score = meta.get('score', '')
    titled = f"[{score}] {title_text}" if score != '' else title_text
    link = get_entry_link(entry)
    pub = entry.get('published', '') or entry.get('updated', '')

    if tag == 'rss':
        channel = root.find('channel')
        if channel is None:
            return False
        item = ET.SubElement(channel, 'item')
        ET.SubElement(item, 'title').text = titled
        ET.SubElement(item, 'link').text = link
        guid = ET.SubElement(item, 'guid')
        guid.set('isPermaLink', 'true')
        guid.text = link or entry.get('id', '') or title_text
        if pub:
            ET.SubElement(item, 'pubDate').text = pub
        ensure_description_prefix(item, 'rss2', entry, meta, journal_name)
        return True

    if tag == '{http://www.w3.org/2005/Atom}feed':
        ns_atom = 'http://www.w3.org/2005/Atom'
        new_entry = ET.SubElement(root, f'{{{ns_atom}}}entry')
        title_el = ET.SubElement(new_entry, f'{{{ns_atom}}}title')
        title_el.text = titled
        link_el = ET.SubElement(new_entry, f'{{{ns_atom}}}link')
        if link:
            link_el.set('href', link)
        id_el = ET.SubElement(new_entry, f'{{{ns_atom}}}id')
        id_el.text = entry.get('id', '') or link or title_text
        if pub:
            pub_el = ET.SubElement(new_entry, f'{{{ns_atom}}}published')
            pub_el.text = pub
            upd_el = ET.SubElement(new_entry, f'{{{ns_atom}}}updated')
            upd_el.text = pub
        ensure_description_prefix(new_entry, 'atom', entry, meta, journal_name)
        return True

    if tag == '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF':
        ns_rss1 = 'http://purl.org/rss/1.0/'
        ns_rdf = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
        item = ET.SubElement(root, f'{{{ns_rss1}}}item')
        if link:
            item.set(f'{{{ns_rdf}}}about', link)
        title_el = ET.SubElement(item, f'{{{ns_rss1}}}title')
        title_el.text = titled
        link_el = ET.SubElement(item, f'{{{ns_rss1}}}link')
        link_el.text = link
        ensure_description_prefix(item, 'rss1', entry, meta, journal_name)
        # Keep the rdf:Seq listing in sync for arXiv readers that iterate it.
        for channel in root.findall(f'{{{ns_rss1}}}channel'):
            items_el = channel.find(f'{{{ns_rss1}}}items')
            if items_el is not None:
                seq = items_el.find(f'{{{ns_rdf}}}Seq')
                if seq is not None and link:
                    li = ET.SubElement(seq, f'{{{ns_rdf}}}li')
                    li.set(f'{{{ns_rdf}}}resource', link)
        return True

    return False


def filter_rss_for_journal(journal_name, feed_url, pending_records=None):
    target_url = feed_url.strip('<> ')
    print(f"\n{'='*80}\n{COLOR_BOLD}{COLOR_BLUE}📚 {journal_name}{COLOR_END}\n{target_url}\n{'='*80}", file=sys.stderr)
    response = requests.get(target_url, timeout=30)
    response.raise_for_status()
    raw_xml = response.content
    parsed_feed = feedparser.parse(raw_xml)
    source_entries = list(parsed_feed.entries)
    retry_entries = [entry_from_pending_record(r) for r in (pending_records or [])]
    if retry_entries:
        print(f"  ⏸ Retrying {len(retry_entries)} pending papers from previous runs", file=sys.stderr)
    entries_to_classify = dedupe_entries_by_link_or_title(retry_entries + source_entries)

    threshold = get_threshold(journal_name)

    keyword_passed_entries, gemini_pending_entries = [], []
    keyword_removed_entries = []
    meta_by_link = {}

    for entry in entries_to_classify:
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
            continue

        # Hard pre-filter: kill biology/medicine/climate/cosmology before
        # spending Gemini API calls on them. This list is curated to avoid
        # blocking physics terms — see HARD_REJECT_KEYWORDS comment.
        reject_kw, reject_loc = find_hard_reject(title, summary)
        if reject_kw:
            keyword_removed_entries.append(entry)
            meta_by_link[link] = {
                "tier": "D_ARCHIVE",
                "score": 0,
                "reason": f"hard reject: '{reject_kw}' in {reject_loc}",
                "tags": [],
            }
            print(f"  ❌ {title}  ('{COLOR_RED}{COLOR_BOLD}{reject_kw}{COLOR_END}' in {reject_loc})", file=sys.stderr)
            continue

        gemini_pending_entries.append(entry)

    gemini_passed_entries, gemini_removed_entries, gemini_retry_entries, gemini_meta = classify_entries_with_gemini(journal_name, gemini_pending_entries)
    meta_by_link.update(gemini_meta)

    passed_entries = keyword_passed_entries + gemini_passed_entries
    passed_links = set(get_entry_link(e) for e in passed_entries)

    root = ET.fromstring(raw_xml)
    xml_items, namespaces = find_xml_items(root)
    parsed_map = entry_by_link(entries_to_classify)

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

    existing_links = {link for _, link, _, _ in xml_items}
    for entry in passed_entries:
        link = get_entry_link(entry)
        if link and link not in existing_links:
            appended = append_synthetic_rss_item(root, entry, meta_by_link.get(link, {}), journal_name)
            if appended:
                print(f"  ✅ Added previously pending paper to RSS: {entry.get('title','')}", file=sys.stderr)
            else:
                print(f"  ⏸ Passed pending paper could not be inserted into non-RSS feed: {entry.get('title','')}", file=sys.stderr)

    buffer = BytesIO()
    ET.ElementTree(root).write(buffer, encoding='utf-8', xml_declaration=True, pretty_print=True)
    return buffer.getvalue(), keyword_passed_entries, gemini_passed_entries, keyword_removed_entries, gemini_removed_entries, gemini_retry_entries, meta_by_link


def paper_record(entry, journal, source, meta):
    authors = get_authors(entry)
    m = meta.get(get_entry_link(entry), {})
    return {
        "journal": journal,
        "source": source,
        "title": entry.get('title', 'No title'),
        "link": get_entry_link(entry),
        "authors": compact_authors(authors),
        "last_authors": last_authors_text(authors),
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


def create_briefing_html(records, email_body_content=''):
    """Create a fast morning briefing, not a full journal-by-journal browser.

    A/B are listed as papers; C/D are summarized as counts with audit links.
    """
    now_utc = datetime.datetime.utcnow()
    now_texas = now_utc - datetime.timedelta(hours=5)
    now_korea = now_utc + datetime.timedelta(hours=9)

    def score_value(r):
        try:
            return float(r.get('score', 0) or 0)
        except Exception:
            return 0

    # Keep the fast briefing clean: A requires both A tier and a direct trigger.
    def record_has_a_trigger(r):
        fake_entry = {'title': r.get('title', ''), 'summary': r.get('summary', '')}
        return has_a_must_trigger(fake_entry)

    a_items = sorted([r for r in records if r.get('tier') == 'A_MUST_READ' and record_has_a_trigger(r)], key=lambda r: (-score_value(r), r.get('journal', ''), r.get('title', '')))
    b_items = sorted([r for r in records if r.get('tier') == 'B_IMPORTANT_CONDMAT' or (r.get('tier') == 'A_MUST_READ' and not record_has_a_trigger(r))], key=lambda r: (-score_value(r), r.get('journal', ''), r.get('title', '')))
    c_items = [r for r in records if str(r.get('tier', '')).startswith('C_') or not r.get('tier')]

    archived_count = sum(1 for line in email_body_content.splitlines() if '❌' in line) if email_body_content else 0

    journal_c_counts = {}
    for r in c_items:
        journal = r.get('journal', 'Unknown')
        journal_c_counts[journal] = journal_c_counts.get(journal, 0) + 1

    def render_paper_list(items, empty_text):
        if not items:
            return f"<p class='text-slate-500'>{html.escape(empty_text)}</p>"
        chunks = ["<ol class='space-y-4 list-decimal list-inside'>"]
        for r in items:
            tags = r.get('tags') or []
            tag_html = ' '.join(f"<span class='text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-600'>#{html.escape(str(t))}</span>" for t in tags[:6])
            score = r.get('score', '')
            score_badge = f"<span class='text-xs px-2 py-1 rounded-full bg-rose-100 text-rose-800 font-semibold'>{html.escape(str(score))}/10</span>" if score != '' else ''
            chunks.append(f"""
<li class='pl-1'>
  <div class='inline-block w-full align-top p-4 rounded-xl border bg-white hover:bg-slate-50'>
    <div class='flex flex-wrap items-center gap-2 mb-1'>{score_badge}<span class='text-xs px-2 py-1 rounded-full bg-indigo-50 text-indigo-700'>{html.escape(r.get('journal', ''))}</span><span class='text-xs text-slate-500'>{html.escape(r.get('source', ''))}</span></div>
    <a href='{html.escape(r.get('link', ''))}' target='_blank' class='text-lg font-semibold text-blue-700 hover:underline'>{html.escape(strip_html(r.get('title', 'No title')))}</a>
    {("<p class='text-sm text-slate-700 mt-1'><b>Last authors:</b> " + html.escape(r.get('last_authors', '')) + "</p>") if r.get('last_authors') else ""}
    <p class='text-sm text-slate-700'><b>{html.escape(r.get('journal', ''))}</b> | {html.escape(r.get('authors', ''))}</p>
    <p class='text-sm text-slate-600 mt-1'><b>Why:</b> {html.escape(r.get('reason') or 'keyword/Gemini passed')}</p>
    <div class='flex flex-wrap gap-1 mt-2'>{tag_html}</div>
    <p class='mt-2'><a href='{html.escape(r.get('link', ''))}' target='_blank' class='text-sm text-blue-600 hover:underline'>Link →</a></p>
  </div>
</li>""")
        chunks.append("</ol>")
        return '\n'.join(chunks)

    if journal_c_counts:
        rows = ''.join(f"<li><span class='font-medium'>{html.escape(journal)}</span>: {count}</li>" for journal, count in sorted(journal_c_counts.items()))
        c_summary = f"<ul class='text-sm text-slate-600 mt-2 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1'>{rows}</ul>"
    else:
        c_summary = "<p class='text-sm text-slate-500 mt-2'>No Maybe / Theory Watch papers in this run.</p>"

    html_doc = f"""<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Morning Paper Briefing</title><script src='https://cdn.tailwindcss.com'></script></head>
<body class='bg-slate-100 p-6'>
<div class='max-w-5xl mx-auto'>
  <div class='mb-6'><a href='index.html' class='inline-flex items-center px-4 py-2 bg-indigo-600 text-white font-semibold rounded hover:bg-indigo-700'>← To Main</a></div>
  <div class='bg-white rounded-2xl shadow-xl p-8'>
    <h1 class='text-3xl font-bold text-slate-900'>[hoonica RSS] Morning Paper Briefing</h1>
    <p class='text-slate-600 mt-2'>Fast skim page: A/B papers are listed; C/D are summarized. Full pass/fail archive remains in the audit page.</p>
    <div class='grid grid-cols-1 md:grid-cols-4 gap-4 my-6'>
      <div class='p-4 rounded-xl bg-red-50'><div class='text-2xl font-bold'>{len(a_items)}</div><div class='text-sm text-slate-600'>A Must Read</div></div>
      <div class='p-4 rounded-xl bg-amber-50'><div class='text-2xl font-bold'>{len(b_items)}</div><div class='text-sm text-slate-600'>B Important CM</div></div>
      <div class='p-4 rounded-xl bg-slate-50'><div class='text-2xl font-bold'>{len(c_items)}</div><div class='text-sm text-slate-600'>C Maybe / Theory Watch</div></div>
      <div class='p-4 rounded-xl bg-gray-50'><div class='text-2xl font-bold'>{archived_count}</div><div class='text-sm text-slate-600'>D Archived / Removed</div></div>
    </div>
    <p class='text-xs text-slate-500 mb-8'>Last updated: {now_texas.strftime('%Y-%m-%d %H:%M')} Texas / {now_korea.strftime('%Y-%m-%d %H:%M')} Korea</p>

    <section class='mt-8'>
      <h2 class='text-2xl font-bold text-red-700 border-b pb-2'>A. MUST READ</h2>
      <div class='mt-4'>{render_paper_list(a_items, 'No A-level papers in this run.')}</div>
    </section>

    <section class='mt-10'>
      <h2 class='text-2xl font-bold text-amber-700 border-b pb-2'>B. IMPORTANT CONDENSED MATTER</h2>
      <div class='mt-4'>{render_paper_list(b_items, 'No B-level papers in this run.')}</div>
    </section>

    <section class='mt-10 p-5 rounded-xl bg-slate-50 border'>
      <h2 class='text-xl font-bold text-slate-800'>C. MAYBE / THEORY WATCH</h2>
      <p class='text-slate-700 mt-2'>{len(c_items)} papers moved to Maybe / Theory Watch. They are kept in the journal-specific RSS feeds and audit page, but hidden from this fast briefing list.</p>
      {c_summary}
    </section>

    <section class='mt-6 p-5 rounded-xl bg-gray-50 border'>
      <h2 class='text-xl font-bold text-slate-800'>D. ARCHIVED</h2>
      <p class='text-slate-700 mt-2'>{archived_count} clearly unrelated or below-threshold papers archived/removed from the filtered RSS feeds.</p>
      <p class='mt-2'><a href='filtered_results.html' target='_blank' class='text-blue-600 hover:underline'>Audit page →</a></p>
    </section>
  </div>
</div>
</body></html>"""
    with open('briefing.html', 'w', encoding='utf-8') as f:
        f.write(html_doc)

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
    PENDING_FILE = 'pending_classification_queue.json'
    pending_queue = load_json_file(PENDING_FILE, {})
    new_pending_queue = {}
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
                pending_records_for_journal = pending_queue.get(journal_name, [])
                filtered_xml, keyword_passed, gemini_passed, keyword_removed, gemini_removed, gemini_pending, meta = filter_rss_for_journal(journal_name, feed_url, pending_records_for_journal)
                if gemini_pending:
                    new_pending_queue[journal_name] = [serialize_entry_for_pending(e) for e in gemini_pending]
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

                email_content += 'PENDING RETRY PAPERS:\n'
                if not gemini_pending:
                    email_content += 'No papers pending retry.\n\n'
                else:
                    for entry in gemini_pending:
                        email_content += f"  ⏸ {entry.get('title', 'No title')} ({get_entry_link(entry) or 'No link'})\n"
                    email_content += '\n'

                # Persist partial progress after each successful journal. If a later journal fails,
                # the next workflow run can resume without losing already processed results.
                with open('partial_email_content.txt', 'w', encoding='utf-8') as f:
                    f.write(email_content)
                save_json_file('partial_briefing_records.json', briefing_records)
                # Preserve unprocessed old pending journals plus newly pending items.
                merged_pending = dict(pending_queue)
                for done_journal in list(JOURNAL_URLS.keys())[:list(JOURNAL_URLS.keys()).index(journal_name)+1]:
                    merged_pending.pop(done_journal, None)
                merged_pending.update(new_pending_queue)
                save_json_file(PENDING_FILE, merged_pending)
            except Exception as e:
                with open(STATE_FILE, 'w', encoding='utf-8') as f:
                    f.write(journal_name)
                email_content += f"\n\nAn error occurred while running the filter script for '{journal_name}':\n{e}\nPlease check workflow logs.\n"
                raise

        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            f.write('SUCCESS')
        # Keep pending items from journals NOT processed in this run (i.e.
        # journals before start_index on a resume run) plus this run's
        # newly-pending items. Without this, a successful resume drops
        # pending items that were saved in a previous partial run.
        final_pending = dict(pending_queue)
        processed_journals = list(JOURNAL_URLS.keys())[start_index:]
        for j in processed_journals:
            final_pending.pop(j, None)
        final_pending.update(new_pending_queue)
        save_json_file(PENDING_FILE, final_pending)
        create_index_html(JOURNAL_URLS, OUTPUT_FILE_BASE)
        create_results_html_file(email_content)
        create_briefing_html(briefing_records, email_content)
        clear_partial_state()
    finally:
        github_server_url = os.getenv('GITHUB_SERVER_URL')
        github_repository = os.getenv('GITHUB_REPOSITORY')
        github_run_id = os.getenv('GITHUB_RUN_ID')
        if github_server_url and github_repository and github_run_id:
            email_content += f"\n\n---\n\nCheck GitHub Actions run for details:\n{github_server_url}/{github_repository}/actions/runs/{github_run_id}\n"
        create_email_body_file(email_content)
