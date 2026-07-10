"""
build_games_csv.py -- Feed 3 (BGA), step 2: flatten the game list to CSV.

Reads today's raw bga_games_<date>.json (from fetch_game_list.py) and writes a
flat bga_games_<date>.csv alongside it. List fields (player_numbers, aliases)
are joined with '|', tag pairs become 'id:value', and the deeply-nested `media`
field is dropped (its structure isn't needed downstream). The CSV is the
committed artifact; the JSON stays local (gitignored).

Run:  python feeds/bga/build_games_csv.py
"""

import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "bga"
DATE_STR = date.today().strftime("%Y%m%d")
INPUT_JSON = DATA_DIR / f"bga_games_{DATE_STR}.json"
OUTPUT_CSV = DATA_DIR / f"bga_games_{DATE_STR}.csv"


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def pipe_list(value):
    if not value:
        return ""
    return "|".join(str(v) for v in value)


def pipe_tag_pairs(value):
    if not value:
        return ""
    return "|".join(f"{pair[0]}:{pair[1]}" for pair in value)


def main():
    if not INPUT_JSON.exists():
        die(f"Input {INPUT_JSON.name} not found -- run fetch_game_list.py first.")

    with open(INPUT_JSON, encoding="utf-8") as f:
        games = json.load(f)
    if not games:
        die("Game list JSON is empty -- aborting.")

    # Union of keys in first-seen order: robust if BGA ever adds a field to only
    # some games, which would crash a games[0]-only header on an unattended run.
    fieldnames = []
    seen = set()
    for game in games:
        for key in game:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        for game in games:
            row = dict(game)
            row["media"] = ""
            row["tags"] = pipe_tag_pairs(game.get("tags"))
            row["player_numbers"] = pipe_list(game.get("player_numbers"))
            row["aliases"] = pipe_list(game.get("aliases"))
            writer.writerow(row)

    print(f"Saved {len(games)} games to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
