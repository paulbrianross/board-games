"""etl / build_slice1 -- the first served-dataset slice (games + game_players).

Turns the captured BGG-API XML into the two-table model spec'd in
markdown/2-etl/dataset-model.md, designed backward from the user's old Tableau
Public viz. Single source is the API XML for every field except on_bga (ours).

Two tables:
  games         -- one row per bgg_id (scalars + denormalised player ranges + flags)
  game_players  -- tall helper, one row per (game x player count) that is
                   in box OR bgg-recommended OR bgg-best, with per-count flags

Inputs (latest of each, under work/ -- transient, re-extract if gone):
  work/bgg_api_<date>/<bgg_id>.xml   one /thing?stats=1 capture per game
  work/bga_bgg_ids_<date>.csv        resolver output -> the BGA-sourced id set

Output (eyeball artifacts under gitignored work/, committed dataset is a later concern):
  work/etl-slice1/games.csv
  work/etl-slice1/game_players.csv
  work/etl-slice1/bga_mismatches.csv  -- games where the two on-BGA claims disagree:
      bga-only (our resolver says on BGA, BGG's family tag doesn't) vs
      bgg-only (BGG tags it on BGA, our resolver has no BGA game landing on that id).
      The mismatch is wanted reconciliation signal, not an error to silence.

Usage:
  python build_slice1.py                 # every game in the latest XML capture
  python build_slice1.py 266192 30549    # only these bgg_ids (warm-up eyeball)
"""
import csv
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent      # processes/etl
ROOT = HERE.parents[1]                       # repo root
WORK_DIR = ROOT / "work"
OUT_DIR = WORK_DIR / "etl-slice1"

CURRENT_YEAR = date.today().year

# --- BGG tag ids (verified against real captures, see dataset-model.md) ---
FAMILY_BGA = "70360"          # Digital Implementations: Board Game Arena
MECHANIC_COOPERATIVE = "2023"
MECHANIC_LEGACY = "2824"

# --- Weight-class bands over averageweight (1-5) ---
# The user's original Tableau bands, reproducing that viz's four-column split.
def weight_class(w):
    if w is None or w < 1.00:
        return ""            # no weight votes -> unclassified
    if w < 2.15:
        return "Lightweight"
    if w < 2.95:
        return "Middleweight"
    if w < 3.75:
        return "Heavier"
    return "Heaviest"


def die(msg):
    print(f"[slice1] ERROR: {msg}")
    sys.exit(1)


def latest(paths):
    """Newest by the trailing _<YYYYMMDD> / <YYYYMMDD> in the name."""
    def key(p):
        m = re.search(r"(\d{8})", p.name)
        return m.group(1) if m else ""
    picked = sorted((p for p in paths), key=key)
    return picked[-1] if picked else None


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


def load_on_bga(resolver_csv):
    """bgg_ids present in the BGA-sourced set (status kept | remapped)."""
    ids = set()
    with resolver_csv.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("status") in ("kept", "remapped"):
                bid = to_int(row.get("bgg_id"))
                if bid is not None:
                    ids.add(bid)
    return ids


def parse_players(item, bgg_id):
    """Return (box, rec, best, rows) from the suggested_numplayers poll + box min/max.

    box/rec/best are (min, max) tuples or (None, None). rows is the list of
    game_players dicts (one per count in box OR rec OR best).

    The poll's open-ended "N+" bucket is skipped: it aggregates all counts above
    box-max and structurally marks the tail edge, never a real best/recommended
    count (those are always discrete numbers before it).
    """
    box_min = to_int(item.find("minplayers").get("value")) if item.find("minplayers") is not None else None
    box_max = to_int(item.find("maxplayers").get("value")) if item.find("maxplayers") is not None else None

    rec_counts, best_counts = set(), set()
    poll = None
    for p in item.findall("poll"):
        if p.get("name") == "suggested_numplayers":
            poll = p
            break

    if poll is not None:
        for results in poll.findall("results"):
            raw = results.get("numplayers", "")
            if raw.endswith("+"):        # tail-edge bucket, not a real count -- skip
                continue
            n = to_int(raw)
            if n is None:
                continue
            tally = {"Best": 0, "Recommended": 0, "Not Recommended": 0}
            for r in results.findall("result"):
                tally[r.get("value")] = to_int(r.get("numvotes")) or 0
            best, rec, notrec = tally["Best"], tally["Recommended"], tally["Not Recommended"]
            if (best + rec) > notrec:
                rec_counts.add(n)
            if best > rec and best > notrec:
                best_counts.add(n)

    box_counts = set(range(box_min, box_max + 1)) if box_min and box_max else set()
    all_counts = sorted(box_counts | rec_counts | best_counts)

    rows = []
    for n in all_counts:
        rows.append({
            "bgg_id": bgg_id,
            "player_count": n,
            "is_box": int(n in box_counts),
            "is_bgg_recommended": int(n in rec_counts),
            "is_bgg_best": int(n in best_counts),
        })

    def span(s):
        return (min(s), max(s)) if s else (None, None)

    return span(box_counts), span(rec_counts), span(best_counts), rows


def parse_game(xml_path, on_bga_ids):
    root = ET.parse(xml_path).getroot()
    item = root.find("item")
    if item is None:
        return None, []
    bgg_id = to_int(item.get("id"))

    name = ""
    for nm in item.findall("name"):
        if nm.get("type") == "primary":
            name = nm.get("value", "")
            break

    year = to_int(item.find("yearpublished").get("value")) if item.find("yearpublished") is not None else None

    stats = item.find("statistics/ratings")
    bayes = to_float(stats.find("bayesaverage").get("value")) if stats is not None else None
    usersrated = to_int(stats.find("usersrated").get("value")) if stats is not None else None
    weight = to_float(stats.find("averageweight").get("value")) if stats is not None else None

    # category / family flags
    is_coop = is_legacy = bgg_bga = 0
    for lk in item.findall("link"):
        t, i = lk.get("type"), lk.get("id")
        if t == "boardgamemechanic" and i == MECHANIC_COOPERATIVE:
            is_coop = 1
        elif t == "boardgamemechanic" and i == MECHANIC_LEGACY:
            is_legacy = 1
        elif t == "boardgamefamily" and i == FAMILY_BGA:
            bgg_bga = 1

    (box, rec, best, player_rows) = parse_players(item, bgg_id)

    age = CURRENT_YEAR - year if year else None
    vpb = round(usersrated / max(age, 1), 1) if (usersrated is not None and age is not None) else None

    game = {
        "bgg_id": bgg_id,
        "name": name,
        "year": year,
        "bgg_rating": round(bayes, 4) if bayes is not None else None,
        "votes_lifetime": usersrated,
        "votes_per_birthday": vpb,
        "weight": round(weight, 4) if weight is not None else None,
        "weight_class": weight_class(weight),
        "on_bga": int(bgg_id in on_bga_ids),
        "bgg_bga_flag": bgg_bga,
        "is_cooperative": is_coop,
        "is_legacy": is_legacy,
        "box_min": box[0], "box_max": box[1],
        "rec_min": rec[0], "rec_max": rec[1],
        "best_min": best[0], "best_max": best[1],
    }
    return game, player_rows


def main():
    id_filter = {int(a) for a in sys.argv[1:]} if len(sys.argv) > 1 else None

    xml_dirs = [p for p in WORK_DIR.glob("bgg_api_*") if p.is_dir()]
    xml_dir = latest(xml_dirs)
    if xml_dir is None:
        die(f"no bgg_api_<date>/ capture under {WORK_DIR} (re-extract from data/bgg-api/)")

    resolver = latest(list(WORK_DIR.glob("bga_bgg_ids_*.csv")))
    if resolver is None:
        die(f"no bga_bgg_ids_<date>.csv under {WORK_DIR}")

    on_bga_ids = load_on_bga(resolver)
    print(f"[slice1] XML capture : {xml_dir.name}")
    print(f"[slice1] resolver    : {resolver.name}  ({len(on_bga_ids)} BGA ids)")

    games, players = [], []
    for xml_path in sorted(xml_dir.glob("*.xml")):
        bid = to_int(xml_path.stem)
        if id_filter is not None and bid not in id_filter:
            continue
        game, prows = parse_game(xml_path, on_bga_ids)
        if game is None:
            print(f"[slice1] WARN: no <item> in {xml_path.name}")
            continue
        games.append(game)
        players.extend(prows)

    games.sort(key=lambda g: (g["weight_class"], -(g["bgg_rating"] or 0)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    game_cols = list(games[0].keys()) if games else []
    with (OUT_DIR / "games.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=game_cols)
        w.writeheader()
        w.writerows(games)
    with (OUT_DIR / "game_players.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["bgg_id", "player_count", "is_box",
                                          "is_bgg_recommended", "is_bgg_best"])
        w.writeheader()
        w.writerows(players)

    mismatches = []
    for g in games:
        if g["on_bga"] != g["bgg_bga_flag"]:
            mismatches.append({
                "bgg_id": g["bgg_id"],
                "name": g["name"],
                "year": g["year"],
                "on_bga": g["on_bga"],
                "bgg_bga_flag": g["bgg_bga_flag"],
                "mismatch_kind": "bga-only" if g["on_bga"] else "bgg-only",
            })
    mismatches.sort(key=lambda m: (m["mismatch_kind"], m["name"].lower()))
    with (OUT_DIR / "bga_mismatches.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["bgg_id", "name", "year", "on_bga",
                                          "bgg_bga_flag", "mismatch_kind"])
        w.writeheader()
        w.writerows(mismatches)

    print(f"[slice1] wrote {len(games)} games, {len(players)} game_players rows -> {OUT_DIR}")
    print(f"[slice1] wrote {len(mismatches)} bga_mismatches rows -> {OUT_DIR}")


if __name__ == "__main__":
    main()
