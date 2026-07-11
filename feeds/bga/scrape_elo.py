"""
scrape_elo.py -- Feed 3b (BGA): the daily ELO time series.

For each game in today's bga_games_<date>.csv (from build_games_csv.py), POSTs to
BGA's ranking endpoint and records the top-10 players by ELO, writing one row per
player to data/bga/elo/bga_elo_<date>.csv. This is the time series that CANNOT be
back-filled -- every day not captured is lost -- so it is the reason Feed 3 is
prioritised.

Design (see elo-wiring-plan.md for the full reasoning):

- The scraper is DUMB: one pass, append-only. It never re-reads/rewrites/cleans a
  file mid-run. The wrapper drives repeated passes; this script just does one.
- Completeness is tracked by a separate DONE-LIST file
  (bga_elo_done_<date>.csv, schema `game_id, run_id, n_rows`), NOT by row counts
  -- a crash can truncate a game's rows, and some games legitimately have < 10
  players, so row counts lie. A game's line is written to the done-list only
  AFTER its rows have landed, so "done" = "in the done-list".
- Every pass is resume-aware automatically: it scrapes only the games not yet in
  today's done-list, appends their rows, then appends their done-list line. No
  --resume flag needed.
- RUN ID = a per-day attempt counter (max in today's done-list + 1). It is stamped
  on every results row and every done-list line, so a later read-time dedup can do
  an exact lookup: the done-list says "game X completed on run 2" -> keep X's
  run-2 rows, drop any earlier fragment. Dedup is a READ-time job, never done here.
- Games are scraped most-valuable-first (by lifetime games_played desc), so a
  partial day still captures the games people care about.

Run:  python feeds/bga/scrape_elo.py
"""

import csv
import random
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

TOP_PLAYERS_URL = "https://boardgamearena.com/gamepanel/gamepanel/getRanking.html"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "bga"
ELO_DIR = DATA_DIR / "elo"
DATE_STR = date.today().strftime("%Y%m%d")
GAMES_FILE = DATA_DIR / f"bga_games_{DATE_STR}.csv"
OUTPUT_FILE = ELO_DIR / f"bga_elo_{DATE_STR}.csv"
DONE_FILE = ELO_DIR / f"bga_elo_done_{DATE_STR}.csv"

# (connect, read) seconds + bounded retry: with ~1,300 POSTs, one stalled socket
# read must not block the whole run (the 2026-07-09 bgg-csv failure mode, more
# acute here). A game that still fails after RETRIES raises and is counted as an
# error by the per-game handler in main() -- it is simply left out of the
# done-list, so a later pass retries it.
TIMEOUT = (10, 30)
RETRIES = 3
BACKOFF = 2  # seconds between attempts

FIELDNAMES = [
    "scraped_at",
    "run_id",
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

DONE_FIELDNAMES = ["game_id", "run_id", "n_rows"]


def load_games(path):
    """Games as dicts {id, name, games_played}, sorted most-played-first.

    Sorting by lifetime games_played descending is partial-day insurance: if a run
    goes partial, the most valuable games are already captured. It has no effect on
    whether a day completes.
    """
    with open(path, newline="", encoding="utf-8") as f:
        games = [
            {
                "id": r["id"],
                "name": r["display_name_en"],
                "games_played": int(r.get("games_played") or 0),
            }
            for r in csv.DictReader(f)
        ]
    games.sort(key=lambda g: g["games_played"], reverse=True)
    return games


def load_done(path):
    """Returns (set of completed game_ids, max run_id) from today's done-list."""
    if not path.exists():
        return set(), 0
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    done_ids = {r["game_id"] for r in rows}
    max_run = max((int(r["run_id"]) for r in rows), default=0)
    return done_ids, max_run


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
    # ELO only ever scrapes against TODAY's game list (never an old one), so if
    # today's file isn't there, the game-list unit hasn't run today -- stand down
    # cleanly rather than scrape the heavy time series against the wrong data.
    # This is a deliberate no-op (exit 0), not a failure: reporting the missing
    # game list is the game-list unit's job, not ELO's.
    if not GAMES_FILE.exists():
        print(f"No game list for today ({GAMES_FILE.name}) -- skipping ELO scrape.")
        return

    ELO_DIR.mkdir(parents=True, exist_ok=True)

    games = load_games(GAMES_FILE)
    total = len(games)

    done_ids, max_run = load_done(DONE_FILE)
    run_id = max_run + 1

    todo = [g for g in games if g["id"] not in done_ids]
    if not todo:
        print(f"All {total} games already in today's done-list -- nothing to do.")
        return

    print(f"Run {run_id}: {len(done_ids)}/{total} already done, "
          f"scraping the remaining {len(todo)} (most-played first).")

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    out_is_new = not OUTPUT_FILE.exists()
    done_is_new = not DONE_FILE.exists()

    games_done = 0
    rows_written = 0
    errors = 0

    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as out, \
            open(DONE_FILE, "a", newline="", encoding="utf-8") as done_out:
        writer = csv.DictWriter(out, fieldnames=FIELDNAMES)
        done_writer = csv.writer(done_out)
        if out_is_new:
            writer.writeheader()
        if done_is_new:
            done_writer.writerow(DONE_FIELDNAMES)

        interactive = sys.stdout.isatty()
        for i, game in enumerate(todo, 1):
            # Live per-game progress only when watched in a terminal. Under the
            # pipeline (output piped to the log) the \r line never newline-flushes
            # and would bury the log; emit a periodic milestone instead, so a
            # stalled unattended run still shows how far it got.
            if interactive:
                print(f"[{i}/{len(todo)}] {game['name']:<40}", end="\r", flush=True)
            elif i % 200 == 0:
                print(f"  ...{i}/{len(todo)} games this pass", flush=True)
            try:
                players = fetch_top10(game["id"])
            except Exception as e:
                # Not written to the done-list -> a later pass will retry it.
                print(f"\nERROR on {game['name']} (id={game['id']}): {e}")
                errors += 1
                time.sleep(random.uniform(0.2, 0.6))
                continue

            # Buffer the whole game's rows, then write them in one go, THEN mark it
            # done -- so the done-list line only ever appears after the rows have
            # landed. A crash between the two leaves the game un-done (it gets
            # retried), and the stale rows are cleaned by read-time dedup.
            rows = [
                {
                    "scraped_at": scraped_at,
                    "run_id": run_id,
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
                }
                for player in players
            ]
            writer.writerows(rows)
            out.flush()
            done_writer.writerow([game["id"], run_id, len(rows)])
            done_out.flush()

            games_done += 1
            rows_written += len(rows)
            time.sleep(random.uniform(0.2, 0.6))

    now_done = len(done_ids) + games_done
    print(f"\nDone with run {run_id}. Output: {OUTPUT_FILE}")
    print(f"This pass: {games_done} games, {rows_written:,} rows  |  Errors: {errors}")
    print(f"Done-list now: {now_done}/{total} games "
          f"({'COMPLETE' if now_done == total else 'partial'}).")


if __name__ == "__main__":
    main()
