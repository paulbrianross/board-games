"""bgg-api / fetch_bgg_api -- fetch raw BGG /thing XML for the care-about set.

Walks the care-about list (work/care_about_<date>.csv), fetches each game's full
/thing?stats=1 XML from BGG in batches of 20 (public endpoint -- no auth, unlike the
bulk-CSV download), and writes one per-game <bgg_id>.xml into a dated zip. The XML is
stored VERBATIM -- no parsing or conversion; unzipping gives back the exact API
response. See bgg-api-plan.md.

Output (committed, dated):
  data/bgg-api/bgg_api_<date>.zip   members: <bgg_id>.xml  (one /thing <item> each)

Unattended-safe: request timeouts + bounded retry with backoff (handles BGG's 202
"queued", 429/503 throttling, and stalls). A permanently-failing batch fails the whole
run loud (non-zero exit) rather than banking a partial snapshot -- the pipeline retries.
"""
import csv
import re
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

import keyring
import requests

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent      # processes/bgg-api
ROOT = HERE.parents[1]                       # repo root
WORK_DIR = ROOT / "work"                      # care-about list lives here
OUT_DIR = ROOT / "data" / "bgg-api"           # committed dated zips

API_URL = "https://boardgamegeek.com/xmlapi2/thing"
BATCH_SIZE = 20
DELAY = 5            # polite gap between batches (seconds)
TIMEOUT = (10, 60)   # (connect, read)
RETRIES = 5
BACKOFF = 5          # base backoff (seconds); grows per attempt

ENV_OPEN = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<items termsofuse="https://boardgamegeek.com/xmlapi/termsofuse">')
ENV_CLOSE = "\n</items>\n"
# Each <item ...>...</item> block; group 1 = the id from the OPENING tag (items don't nest).
ITEM_RE = re.compile(r'<item\b[^>]*\bid="(\d+)"[^>]*>.*?</item>', re.DOTALL)


def die(msg):
    print(f"[bgg-api] ERROR: {msg}")
    sys.exit(1)


def latest(paths, what):
    files = sorted(paths)
    if not files:
        die(f"no {what} found")
    return files[-1]


def fetch_batch(ids, headers):
    """One /thing call for up to BATCH_SIZE ids; returns raw XML text, or dies."""
    params = {"id": ",".join(ids), "type": "boardgame", "stats": "1"}
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(API_URL, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            wait = BACKOFF * attempt
            print(f"  request error ({e}); retry {attempt}/{RETRIES} in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 200:
            return r.text
        if r.status_code in (202, 429, 503):   # queued / throttled -- BGG says wait
            wait = BACKOFF * attempt
            print(f"  HTTP {r.status_code}; retry {attempt}/{RETRIES} in {wait}s")
            time.sleep(wait)
            continue
        die(f"HTTP {r.status_code}: {r.text[:300]}")
    die(f"batch failed after {RETRIES} retries")


def main():
    run_date = date.today().strftime("%Y%m%d")

    token = keyring.get_password("bgg", "api_token")
    if not token:
        die("no BGG API token in keyring (service 'bgg', user 'api_token')")
    headers = {"Authorization": f"Bearer {token}"}

    care = latest(WORK_DIR.glob("care_about_*.csv"),
                  "care-about list (run build_care_about.py first)")
    with open(care, encoding="utf-8") as f:
        ids = [r["bgg_id"] for r in csv.DictReader(f) if r["bgg_id"].strip()]
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]   # de-dup, keep order

    total = len(ids)
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"[bgg-api] care-about : {care.name}")
    print(f"[bgg-api] games      : {total}  ({batches} batches of {BATCH_SIZE})\n")

    items = {}   # bgg_id -> raw "<item ...>...</item>"
    for b in range(batches):
        chunk = ids[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        print(f"[bgg-api] batch {b + 1}/{batches} ({len(chunk)} ids)...", flush=True)
        xml = fetch_batch(chunk, headers)
        got = 0
        for m in ITEM_RE.finditer(xml):
            if m.group(1) in seen:      # only keep ids we asked for
                items[m.group(1)] = m.group(0)
                got += 1
        if got != len(chunk):
            absent = [i for i in chunk if i not in items]
            print(f"  note: got {got}/{len(chunk)} items; not returned: {absent}")
        if b < batches - 1:
            time.sleep(DELAY)

    missing = [i for i in ids if i not in items]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"bgg_api_{run_date}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for gid in sorted(items, key=int):
            z.writestr(f"{gid}.xml", ENV_OPEN + "\n" + items[gid] + ENV_CLOSE)

    print(f"\n[bgg-api] fetched : {len(items)}/{total}")
    if missing:
        print(f"[bgg-api] MISSING : {len(missing)} -> {missing}")
    print(f"[bgg-api] zip -> {out.relative_to(ROOT)} ({out.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
