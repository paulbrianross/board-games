"""
download_bgg_ranks.py -- Feed 1: the BGG bulk "boardgames_ranks" data dump.

Logs in to BoardGameGeek with the password held in the Windows Credential
Manager (see the one-off setup script in the scratch workspace's
code/store_bgg_credential.py), reads the login-gated data-dumps page, extracts
the freshly-signed Amazon S3 download link, and saves the .zip -- as-is,
un-extracted -- into the repo's data/ folder with a dated filename.

Why it works this way:
  - The bulk CSV is behind a login: a plain request (or the XML-API bearer
    token) only ever returns the logged-out HTML, never the file. So we
    establish a real session via BGG's login API and reuse its cookies.
  - The download link on that page is a *pre-signed* S3 URL that expires in
    ~10 minutes, so it cannot be cached -- every run must fetch a fresh one.
  - The S3 download itself needs no auth and isn't behind Cloudflare; the
    signature in the URL is the authorization.

Run:  python bgg-csv/download_bgg_ranks.py [--force]
  --force  re-download even if this date's file already exists.
"""

import argparse
import html
import io
import re
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

import keyring
import requests

sys.stdout.reconfigure(encoding="utf-8")

# --- config -----------------------------------------------------------------
SERVICE = "bgg"                 # keyring service name (set by store_bgg_credential.py)
USERNAME = "Soulspar"           # public BGG username -- not secret
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
LOGIN_URL = "https://boardgamegeek.com/login/api/v1"
DUMPS_URL = "https://boardgamegeek.com/data_dumps/bg_ranks"

# Network resilience. TIMEOUT is (connect, read) in seconds: every request must
# be bounded or a stalled socket read blocks the process forever -- the exact
# failure that silently killed the 2026-07-09 run mid-download. The big S3
# download additionally gets a few bounded retries so a transient stall doesn't
# lose the day.
TIMEOUT = (10, 60)
S3_RETRIES = 3
S3_BACKOFF = 3  # seconds between download attempts

# Output lands in the repo's data/bgg-csv/ folder (one subfolder per feed),
# resolved relative to THIS file so it works no matter what directory the
# scheduler runs it from. This script sits at processes/bgg-csv/, so the repo
# root is three levels up.
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "bgg-csv"

# Matches the <a href="..."> S3 link in the data-dumps page.
LINK_RE = re.compile(
    r'href="(https://geek-export-stats\.s3\.amazonaws\.com/[^"]+)"')


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def get_download_link(session):
    """Log in and return the fresh, fully-unescaped S3 download URL."""
    password = keyring.get_password(SERVICE, USERNAME)
    if not password:
        die(f"No password in Windows Credential Manager for service='{SERVICE}' "
            f"username='{USERNAME}'. Run store_bgg_credential.py first.")

    resp = session.post(
        LOGIN_URL,
        json={"credentials": {"username": USERNAME, "password": password}},
        timeout=TIMEOUT)
    if resp.status_code not in (200, 202, 204) or "bggusername" not in session.cookies:
        die(f"Login failed: HTTP {resp.status_code}, "
            f"cookies={sorted(session.cookies.keys())}. "
            f"Check the stored BGG password.")
    print(f"Logged in as {USERNAME} (HTTP {resp.status_code}).")

    page = session.get(DUMPS_URL, timeout=TIMEOUT)
    if page.status_code != 200:
        die(f"data_dumps page returned HTTP {page.status_code}.")

    match = LINK_RE.search(page.text)
    if not match:
        die("No S3 download link found on the data_dumps page -- "
            "the session may not be authenticated.")
    # HTML-escaped ampersands (&amp;) must be turned back into & or the
    # signed query string breaks.
    return html.unescape(match.group(1))


def output_filename(link):
    """Annotate BGG's served filename with OUR download date, e.g.
    'boardgames_ranks_2026-07-06_downloaded20260718.zip'.

    The served name (taken straight from the S3 object key, the path before the
    '?' query) is kept verbatim -- BGG's 'boardgames_ranks_2026-07-06' stays
    intact -- and we append '_downloaded<YYYYMMDD>' (our dash-free run date). Two
    reasons: (1) the served name is dated by BGG's DUMP date, which lags the run
    date by a day (the ~00:30 UTC run fetches the previous UTC day's dump), so an
    explicit download date lets the pipeline's weekly gate key on run-date like
    every other feed, with no fragile dump-vs-run offset; (2) it records both the
    data date (BGG's) and our capture date. `latest()` sort is unaffected: the
    dump-date prefix still dominates, and '.zip' sorts before '_downloaded'.
    """
    name = link.split("?", 1)[0].rsplit("/", 1)[-1]
    if not name.endswith(".zip"):
        die(f"Unexpected download filename '{name}' -- expected a .zip.")
    stem = name[:-len(".zip")]
    return f"{stem}_downloaded{date.today():%Y%m%d}.zip"


def download_zip(session, link):
    """Fetch the S3 zip with a timeout + bounded retries, returning its bytes.

    The 2026-07-09 run hung here indefinitely: session.get had no timeout and
    the S3 socket read stalled, so the process blocked until it was killed
    mid-run. Now each attempt is bounded by TIMEOUT, and a stalled/dropped read
    (a RequestException) is retried a few times before giving up cleanly -- at
    which point the wrapper aborts without pushing and the next run recovers.
    A non-200 from S3 is a definitive answer (e.g. an expired link), not a
    stall, so it fails immediately rather than retrying.
    """
    last_err = None
    for attempt in range(1, S3_RETRIES + 1):
        try:
            resp = session.get(link, timeout=TIMEOUT)
            if resp.status_code != 200:
                die(f"S3 download returned HTTP {resp.status_code}.")
            return resp.content
        except requests.exceptions.RequestException as err:
            last_err = err
            print(f"  download attempt {attempt}/{S3_RETRIES} failed: {err}")
            if attempt < S3_RETRIES:
                time.sleep(S3_BACKOFF)
    die(f"S3 download failed after {S3_RETRIES} attempts: {last_err}")


def main():
    parser = argparse.ArgumentParser(description="Download the BGG bulk ranks dump.")
    parser.add_argument("--force", action="store_true",
                        help="re-download even if this date's file already exists")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    link = get_download_link(session)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / output_filename(link)
    if out_path.exists() and not args.force:
        print(f"Already have {out_path.name} -- skipping (use --force to re-download).")
        return

    print(f"Downloading zip -> {out_path.name} ...")
    content = download_zip(session, link)
    if content[:2] != b"PK":
        die("Downloaded data is not a zip (missing PK header) -- aborting.")

    # Validate the archive and report the CSV inside before committing to disk.
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            die("Zip contains no .csv -- aborting.")
        info = zf.getinfo(csv_names[0])
        print(f"  contains {csv_names[0]}  "
              f"({info.file_size:,} bytes uncompressed, "
              f"{len(content):,} bytes zipped)")

    out_path.write_bytes(content)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
