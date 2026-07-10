"""
scrape_elo.py -- Feed 3 (BGA), step 3: the daily ELO time series.

For each game in today's bga_games_<date>.csv (from build_games_csv.py), POSTs
to BGA's ranking endpoint and records the top-10 players by ELO, writing one row
per player to bga_elo_<date>.csv. This is the time series that CANNOT be
back-filled -- every day not captured is lost -- so it is the reason Feed 3 is
prioritised.

All rows from one run share a single `scraped_at` UTC timestamp set at the
start. `--resume` re-reads the file, skips games already complete (exactly 10
rows) under the latest timestamp, and re-scrapes the rest under that same
timestamp -- so an interrupted run can be finished without double-counting.

Run:  python feeds/bga/scrape_elo.py [--resume]
"""

import csv
import random
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

TOP_PLAYERS_URL = "https://boardgamearena.com/gamepanel/gamepanel/getRanking.html"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "bga"
DATE_STR = date.today().strftime("%Y%m%d")
GAMES_FILE = DATA_DIR / f"bga_games_{DATE_STR}.csv"
OUTPUT_FILE = DATA_DIR / f"bga_elo_{DATE_STR}.csv"

# (connect, read) seconds + bounded retry: with ~1,300 POSTs, one stalled socket
# read must not block the whole run (the 2026-07-09 bgg-csv failure mode, more
# acute here). A game that still fails after RETRIES raises and is counted as an
# error by the per-game handler in main(), so one bad game never kills the run.
TIMEOUT = (10, 30)
RETRIES = 3
BACKOFF = 2  # seconds between attempts

FIELDNAMES = [
    "scraped_at",
    "game_id",
    "game_name",
    "rank_no",
    "player_id",
    "player_name",
    "country_code",
    "elo",
    "nbr_game",
    "device",
    "status",
]


def load_games(path):
    with open(path, newline="", encoding="utf-8") as f:
        return [{"id": r["id"], "name": r["display_name_en"]} for r in csv.DictReader(f)]


def load_latest_batch(path):
    """Returns (scraped_at, {game_id: row_count}) for the most recent batch in the file."""
    if not path.exists():
        return None, {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None, {}
    latest_ts = max(r["scraped_at"] for r in rows)
    counts = defaultdict(int)
    for r in rows:
        if r["scraped_at"] == latest_ts:
            counts[r["game_id"]] += 1
    return latest_ts, counts


def check_integrity(path):
    """Warn about any game+run combinations with more than 10 rows."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    counts = defaultdict(int)
    for r in rows:
        counts[(r["scraped_at"], r["game_id"], r["game_name"])] += 1
    problems = [(ts, gid, name, n) for (ts, gid, name), n in counts.items() if n > 10]
    if problems:
        print(f"\nWARNING: {len(problems)} game(s) have more than 10 rows in a single run:")
        for ts, gid, name, n in sorted(problems):
            print(f"  {ts}  {name} (id={gid})  --  {n} rows")
    else:
        print("Integrity check passed: no game has more than 10 rows in any run.")


def fetch_top10(game_id):
    """POST for a game's top-10 ELO ranks, bounded by TIMEOUT with a few retries.

    A transient stall or drop (RequestException) is retried; anything still
    failing after RETRIES is raised for main() to count as a per-game error.
    """
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.post(
                TOP_PLAYERS_URL,
                data={"game": game_id, "start": 0, "mode": "elo"},
                timeout=TIMEOUT)
            response.raise_for_status()
            return response.json()["data"]["ranks"]
        except requests.exceptions.RequestException as err:
            last_err = err
            if attempt < RETRIES:
                time.sleep(BACKOFF)
    raise RuntimeError(f"failed after {RETRIES} attempts: {last_err}")


def main():
    resume = "--resume" in sys.argv

    # ELO only ever scrapes against TODAY's game list (never an old one), so if
    # today's file isn't there, the game-list unit hasn't run today -- stand down
    # cleanly rather than scrape the heavy time series against the wrong data.
    # This is a deliberate no-op (exit 0), not a failure: reporting the missing
    # game list is the game-list unit's job, not ELO's.
    if not GAMES_FILE.exists():
        print(f"No game list for today ({GAMES_FILE.name}) -- skipping ELO scrape.")
        return

    games = load_games(GAMES_FILE)
    game_ids = {g["id"] for g in games}

    if resume:
        scraped_at, batch_counts = load_latest_batch(OUTPUT_FILE)
        if scraped_at is None:
            print("No existing data found -- starting fresh.")
            resume = False
        else:
            complete = {gid for gid, n in batch_counts.items() if n == 10}
            if complete >= game_ids:
                print("Last run was complete -- nothing to resume. "
                      "Run without --resume for a fresh scrape.")
                return
            skip_ids = complete
            print(f"Resuming run from {scraped_at} -- skipping {len(skip_ids)} "
                  f"complete games, re-scraping {len(games) - len(skip_ids)}")

    if not resume:
        scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        skip_ids = set()

    file_exists = OUTPUT_FILE.exists()
    rows_written = 0
    errors = 0

    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()

        for i, game in enumerate(games, 1):
            if game["id"] in skip_ids:
                continue

            print(f"[{i}/{len(games)}] {game['name']:<40}", end="\r", flush=True)
            try:
                players = fetch_top10(game["id"])
                for player in players:
                    writer.writerow({
                        "scraped_at": scraped_at,
                        "game_id": game["id"],
                        "game_name": game["name"],
                        "rank_no": player["rank_no"],
                        "player_id": player["id"],
                        "player_name": player["name"],
                        "country_code": player.get("country", {}).get("code", ""),
                        "elo": round(float(player["ranking"])),
                        "nbr_game": player["nbr_game"],
                        "device": player["device"],
                        "status": player["status"],
                    })
                    rows_written += 1
            except Exception as e:
                print(f"\nERROR on {game['name']} (id={game['id']}): {e}")
                errors += 1

            time.sleep(random.uniform(0.2, 0.6))

    print(f"\nDone. Output: {OUTPUT_FILE}")
    print(f"Rows written this session: {rows_written:,}  |  Errors: {errors}")
    check_integrity(OUTPUT_FILE)


if __name__ == "__main__":
    main()
