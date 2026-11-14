"""Keep the requested recovered article visible for a bounded reader-sync window.

Reissues one requested article with a fixed fresh feed identity and date.
Original arXiv submission and announcement dates remain explicit in its content.
No classifier calls, external requests, or email changes.
"""
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
import re
import xml.etree.ElementTree as ET

EXPIRES_AT = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)
FEED_PATH = "filtered_feed_arXiv_CondMat.xml"
ARTICLE_URL = "https://arxiv.org/abs/2611.09857"
RECOVERED_ITEM = "<item><title>[10] Energy- and momentum-resolved Cooper-pair emission from a cuprate superconductor</title><link>https://arxiv.org/abs/2611.09857</link><guid isPermaLink=\"true\">https://arxiv.org/abs/2611.09857</guid><pubDate>Mon, 09 Nov 2026 00:00:00 -0500</pubDate><description><![CDATA[<p><b>Last authors:</b> Dirk K. Morr, Fahad Mahmood</p><p><b>Authors:</b> Henry Amir, Jack Zwettler, Ka Ho Wong, et al.</p><p><span style='display:inline-block;padding:4px 9px;border-radius:999px;background:#fee2e2;color:#991b1b;font-weight:700;'>Score 10/10</span> <b>A_MUST_READ</b></p><p><b>Why:</b> Direct experimental ARPES-related study (2e-ARPES) on cuprates, highly relevant to methodology and physics.</p><p><b>Tags:</b> <span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#experimental</span> <span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#ARPES</span> <span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#cuprate</span> <span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#superconductivity</span></p><p><img src='https://arxiv.org/html/2611.09857v1/fig1_v8.png' style='max-width:100%;height:auto;border-radius:10px;' /></p><hr/><p><b>Abstract:</b> Superconductivity arises from Cooper pairing, but conventional photoemission probes electrons individually rather than as pairs. Here we report the observation of Cooper-pair emission from an optimally doped cuprate superconductor using two-electron angle-resolved photoemission (2e-ARPES). Below the superconducting transition, we observe a distinct two-electron emission peak at zero pair binding energy and zero pair momentum that disappears in the normal state, revealing the characteristic fingerprint of a Cooper pair. Unexpectedly, the joint energy distribution broadens markedly along the relative-energy axis as temperature decreases, while the total energy width remains nearly unchanged. This anomalous behavior cannot be explained by instrumental resolution, static disorder or thermal broadening, and instead points to scattering processes involving collective excitations of the superconductor. Our results establish 2e-ARPES as a probe of both Cooper-pair emission and the many-body dynamics of the superconducting state.</p>]]></description><content:encoded><![CDATA[<p><b>Last authors:</b> Dirk K. Morr, Fahad Mahmood</p><p><b>Authors:</b> Henry Amir, Jack Zwettler, Ka Ho Wong, et al.</p><p><span style='display:inline-block;padding:4px 9px;border-radius:999px;background:#fee2e2;color:#991b1b;font-weight:700;'>Score 10/10</span> <b>A_MUST_READ</b></p><p><b>Why:</b> Direct experimental ARPES-related study (2e-ARPES) on cuprates, highly relevant to methodology and physics.</p><p><b>Tags:</b> <span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#experimental</span> <span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#ARPES</span> <span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#cuprate</span> <span style='display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;'>#superconductivity</span></p><p><img src='https://arxiv.org/html/2611.09857v1/fig1_v8.png' style='max-width:100%;height:auto;border-radius:10px;' /></p><hr/><p><b>Abstract:</b> Superconductivity arises from Cooper pairing, but conventional photoemission probes electrons individually rather than as pairs. Here we report the observation of Cooper-pair emission from an optimally doped cuprate superconductor using two-electron angle-resolved photoemission (2e-ARPES). Below the superconducting transition, we observe a distinct two-electron emission peak at zero pair binding energy and zero pair momentum that disappears in the normal state, revealing the characteristic fingerprint of a Cooper pair. Unexpectedly, the joint energy distribution broadens markedly along the relative-energy axis as temperature decreases, while the total energy width remains nearly unchanged. This anomalous behavior cannot be explained by instrumental resolution, static disorder or thermal broadening, and instead points to scattering processes involving collective excitations of the superconductor. Our results establish 2e-ARPES as a probe of both Cooper-pair emission and the many-body dynamics of the superconducting state.</p>]]></content:encoded><dc:creator>Last authors: Dirk K. Morr, Fahad Mahmood; Authors: Henry Amir, Jack Zwettler, Ka Ho Wong, et al.</dc:creator></item>"

# Fixed once, not regenerated daily: lets readers discover the requested replay.
REPLAY_GUID = "urn:rss-filter:reissue:2611.09857v1:20261114T024100Z"
REPLAY_DATE = "Sat, 14 Nov 2026 02:41:00 +0000"
NOTICE = ("<p><b>RSS reissue for reader synchronization:</b> November 14, 2026 UTC. "
          "This is not a new paper version. Original arXiv v1 submission: November 6, 2026; "
          "arXiv RSS announcement: November 9, 2026.</p>")
REPLAY_ITEM = RECOVERED_ITEM.replace(
    '<guid isPermaLink="true">' + ARTICLE_URL + '</guid>',
    '<guid isPermaLink="false">' + REPLAY_GUID + '</guid>'
).replace('<link>' + ARTICLE_URL + '</link>', '<link>' + ARTICLE_URL + 'v1</link>')
REPLAY_ITEM = re.sub(r"<pubDate>.*?</pubDate>", "<pubDate>" + REPLAY_DATE + "</pubDate>", REPLAY_ITEM)
REPLAY_ITEM = REPLAY_ITEM.replace('<title>[10] ', '<title>[10] [RSS reissue] ')
REPLAY_ITEM = REPLAY_ITEM.replace('<![CDATA[', '<![CDATA[' + NOTICE)


def restore_requested_item(feed_path=FEED_PATH, now=None):
    now = now or datetime.now(timezone.utc)
    if now >= EXPIRES_AT:
        return False
    path = Path(feed_path)
    original = path.read_text(encoding="utf-8")
    root = ET.fromstring(original)
    channel = root.find("channel")
    if channel is None or original.count("</channel>") != 1:
        raise ValueError("RSS channel boundary missing or ambiguous")
    # Remove only this article's original/replay blocks; keep all other bytes.
    namespaces = " ".join('xmlns:%s="%s"' % pair for pair in
                          re.findall(r'xmlns:([^= ]+)="([^"]+)"', original))
    def keep_other_item(match):
        block = match.group(0)
        entry = ET.fromstring("<root " + namespaces + ">" + block + "</root>")[0]
        link = (entry.findtext("link") or "").strip().rstrip("/")
        guid = (entry.findtext("guid") or "").strip()
        target = link in (ARTICLE_URL, ARTICLE_URL + "v1") or guid in (
            ARTICLE_URL, "oai:arXiv.org:2611.09857v1", REPLAY_GUID)
        return "" if target else block
    stripped = re.sub(r"<item(?:\s[^>]*)?>.*?</item>", keep_other_item, original, flags=re.S)
    # Place the recent replay first without altering other items or their order.
    first_item = re.search(r"<item(?:\s[^>]*)?>", stripped)
    pos = first_item.start() if first_item else stripped.index("</channel>")
    restored = stripped[:pos] + REPLAY_ITEM + stripped[pos:]
    # Whitespace left by prior replacements is harmless, but avoid repeat writes.
    existing = channel.findall("item")
    if sum((e.findtext("guid") or "") == REPLAY_GUID for e in existing) == 1:
        target_entries = [e for e in existing if (e.findtext("link") or "").rstrip("/") in (ARTICLE_URL, ARTICLE_URL + "v1")]
        if len(target_entries) == 1 and target_entries[0].findtext("pubDate") == REPLAY_DATE:
            return False
    restored = re.sub(r"<lastBuildDate>.*?</lastBuildDate>",
                      "<lastBuildDate>" + format_datetime(now) + "</lastBuildDate>", restored, count=1)
    ET.fromstring(restored)
    temp = path.with_name(path.name + ".restore.tmp")
    temp.write_text(restored, encoding="utf-8")
    temp.replace(path)
    print("Reissued 2611.09857 for reader synchronization until " + EXPIRES_AT.isoformat())
    return True

if __name__ == "__main__":
    restore_requested_item()
