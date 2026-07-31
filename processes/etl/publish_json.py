"""etl / publish_json -- transform the slice-1 CSV working tables into the
published games.json contract (see markdown/2-etl/dataset-model.md).

Reads the eyeball CSVs that build_slice1.py produces and emits the single
nested games.json the web page fetches:
  - one envelope object { generated, count, games: [...] }
  - each game embeds its game_players rows as a `players` array
  - real booleans (not the CSV's 0/1), short player-row keys (n/box/rec/best),
    box/rec/best collapsed to [min, max] pairs, numbers at full ETL precision

Inputs (under gitignored work/, produced by build_slice1.py):
  work/etl-slice1/games.csv
  work/etl-slice1/game_players.csv

Output:
  work/etl-slice1/games.json   -- transient here; step-4 bootstrap hand-copies
                                  it into the paulbrianross.github.io repo /data/.
                                  The automated push is the step-9 refresh loop.

Usage:
  python publish_json.py
"""
import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent      # processes/etl
ROOT = HERE.parents[1]                       # repo root
OUT_DIR = ROOT / "work" / "etl-slice1"
GAMES_CSV = OUT_DIR / "games.csv"
PLAYERS_CSV = OUT_DIR / "game_players.csv"
OUT_JSON = OUT_DIR / "games.json"


def die(msg):
    print(f"[publish] ERROR: {msg}")
    sys.exit(1)


def to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def as_bool(s):
    """CSV 0/1 -> real JSON boolean."""
    return s == "1"


def pair(lo, hi):
    """[min, max] ints, or null when the range is absent (e.g. no player data)."""
    lo, hi = to_int(lo), to_int(hi)
    if lo is None or hi is None:
        return None
    return [lo, hi]


def load_players():
    """bgg_id -> its game_players rows as short-key dicts, sorted by count."""
    by_game = defaultdict(list)
    with PLAYERS_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            bid = to_int(row["bgg_id"])
            by_game[bid].append({
                "n": to_int(row["player_count"]),
                "box": as_bool(row["is_box"]),
                "rec": as_bool(row["is_bgg_recommended"]),
                "best": as_bool(row["is_bgg_best"]),
            })
    for rows in by_game.values():
        rows.sort(key=lambda r: (r["n"] is None, r["n"]))
    return by_game


def load_games(players_by_game):
    """Read games.csv in file order (already sorted weight-class then rating)."""
    games = []
    with GAMES_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            bid = to_int(row["bgg_id"])
            games.append({
                "bgg_id": bid,
                "name": row["name"],
                "year": to_int(row["year"]),
                "bgg_rating": to_float(row["bgg_rating"]),
                "votes_lifetime": to_int(row["votes_lifetime"]),
                "votes_per_birthday": to_float(row["votes_per_birthday"]),
                "weight": to_float(row["weight"]),
                "weight_class": row["weight_class"],
                "on_bga": as_bool(row["on_bga"]),
                "bgg_bga_flag": as_bool(row["bgg_bga_flag"]),
                "is_cooperative": as_bool(row["is_cooperative"]),
                "is_legacy": as_bool(row["is_legacy"]),
                "box": pair(row["box_min"], row["box_max"]),
                "rec": pair(row["rec_min"], row["rec_max"]),
                "best": pair(row["best_min"], row["best_max"]),
                "players": players_by_game.get(bid, []),
            })
    return games


def main():
    if not GAMES_CSV.exists():
        die(f"no {GAMES_CSV} -- run build_slice1.py first")
    if not PLAYERS_CSV.exists():
        die(f"no {PLAYERS_CSV} -- run build_slice1.py first")

    players_by_game = load_players()
    games = load_games(players_by_game)

    # "generated" = when this dataset was published. In the wired weekly pipeline
    # this equals the capture date (same Tuesday run); standalone it's today.
    payload = {
        "generated": date.today().isoformat(),
        "count": len(games),
        "games": games,
    }

    # indent=2 keeps the committed dataset git-diffable (the flat-JSON rationale
    # in dataset-model.md); ensure_ascii=False preserves real UTF-8 names.
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"[publish] wrote {len(games)} games -> {OUT_JSON}")


if __name__ == "__main__":
    main()
