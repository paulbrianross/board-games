"""
fetch_game_list.py -- Feed 3 (BGA), step 1: the BoardGameArena game list.

Fetches BGA's public game-list page and pulls the embedded `game_list` JSON
blob (~1,300 games) out of the inline <script> that bootstraps the page, then
saves it verbatim to data/bga/ with today's date. This raw JSON is the source
the next step (build_games_csv.py) flattens into the committed CSV; the JSON
itself is kept local only (gitignored) -- it is large and mostly whitespace.

The game list carries no server-side date of its own, so we stamp it with the
local run date.

Run:  python processes/bga/fetch_game_list.py
"""

import json
import sys
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

# --- config -----------------------------------------------------------------
GAME_LIST_URL = "https://boardgamearena.com/gamelist?isPopular="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# (connect, read) seconds -- every request must be bounded so a stalled socket
# read can't block the run forever (the 2026-07-09 bgg-csv failure mode).
TIMEOUT = (10, 60)

# Output lands in the repo's data/bga/ folder, resolved relative to THIS file so
# it works no matter what directory the scheduler runs it from. This script sits
# at processes/bga/, so the repo root is two levels up, then /data/bga.
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "bga"
DATE_STR = date.today().strftime("%Y%m%d")


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def fetch_game_list():
    """Fetch the page and extract the `game_list` array from its inline script.

    BGA renders the list into a JS bootstrap object on the page rather than
    serving JSON; we locate the <script> containing `game_list` and slice the
    object out from `"game_list"` up to the following `"game_tags"` key.
    """
    resp = requests.get(GAME_LIST_URL, headers={"User-Agent": UA}, timeout=TIMEOUT)
    if resp.status_code != 200:
        die(f"BGA game list returned HTTP {resp.status_code}.")

    soup = BeautifulSoup(resp.content, "html.parser")
    script_tag = soup.find("script", string=lambda t: t and "game_list" in t)
    if script_tag is None or not script_tag.string:
        die("Could not find the inline <script> carrying game_list -- "
            "the page structure may have changed.")

    raw = script_tag.string
    start = raw.find('"game_list"')
    end = raw.find('"game_tags"')
    if start == -1 or end == -1:
        die("Could not locate the game_list JSON boundaries in the script.")

    blob = "{" + raw[start:end - 1] + "}"
    try:
        games = json.loads(blob)["game_list"]
    except (json.JSONDecodeError, KeyError) as err:
        die(f"Failed to parse the game_list JSON: {err}")
    return games


def main():
    # One BGA snapshot per calendar day: the game list is a live endpoint (no
    # server date of its own), so unlike the immutable BGG dump a re-fetch just
    # yields a slightly different file. If today's CSV already exists the unit
    # has run today -- skip (exit 0), so a re-run/retry is a clean no-op and
    # ELO's game total stays frozen across the day's retries.
    csv_path = OUTPUT_DIR / f"bga_games_{DATE_STR}.csv"
    if csv_path.exists():
        print(f"Already have today's game list ({csv_path.name}) -- skipping fetch.")
        return

    games = fetch_game_list()
    if not games:
        die("game_list parsed but empty -- aborting.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"bga_games_{DATE_STR}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(games)} games to {out_path}")


if __name__ == "__main__":
    main()
