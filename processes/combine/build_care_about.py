"""combine / build_care_about -- the care-about set (the BGG-API fetch list).

The lean master list (~1,000 games) the pipeline keeps: the great games in, the filler
out. Two sides, deliberately different bars (see care-about-set.md for the full design):

  BGA side  -- games playable on BGA (the resolver gave them a BGG id):
              keep if  is_expansion == 0  AND  (geek >= 5.95  OR  lifetime plays >= 100k)
  BGG side  -- good games NOT on BGA, pulled in as similarity-browse neighbours:
              keep if  is_expansion == 0  AND  geek >= 6.95,  minus anything already on BGA

Hard constraint: every kept game must have a BGG rating. A resolver id absent from the
bulk CSV, or a bulk row with no geek rating, has no rating -> dropped.

  geek  = bulk CSV bayesaverage    votes = usersrated    lifetime plays = BGA games_played

Inputs (latest of each):
  work/bga_bgg_ids_<date>.csv      the resolver's unique BGG-id list (transient)
  data/bgg-csv/*.zip               BGG bulk CSV (member boardgames_ranks.csv), zipped
  data/bga/bga_games_<date>.csv    BGA game list (for games_played)

Output (transient, dated -- gitignored scratch, reconstructible from the inputs):
  work/care_about_<date>.csv
    cols: bgg_id, bgg_name, side, qualifier, geek, votes, rank, year,
          bga_id, bga_name, bga_plays
    side      : bga | bgg   (a game on BGA is always 'bga', even if it also clears 6.95)
    qualifier : which bar earned it -- geek | plays | geek+plays (bga), geek (bgg)
"""
import csv
import io
import sys
import zipfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent      # processes/combine
ROOT = HERE.parents[1]                       # repo root
WORK_DIR = ROOT / "work"                      # resolver output lives here + our output
BGA_DIR = ROOT / "data" / "bga"               # BGA game list (for games_played)
BGG_DIR = ROOT / "data" / "bgg-csv"           # BGG bulk CSV zips

BGA_BAR = 5.95        # BGA-side geek bar (rounds to 6.0)
BGG_BAR = 6.95        # BGG-side geek bar (rounds to 7.0)
PLAYS_BAR = 100_000   # BGA-side lifetime-plays rescue


def die(msg):
    print(f"[care-about] ERROR: {msg}")
    sys.exit(1)


def to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def latest(paths, what):
    files = sorted(paths)
    if not files:
        die(f"no {what} found")
    return files[-1]


def load_bulk(zip_path):
    """bgg_id -> bulk-CSV row dict, read straight from the zip (no disk unpack)."""
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if n.endswith(".csv")]
        if not members:
            die(f"no .csv inside {zip_path.name}")
        with z.open(members[0]) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            return {r["id"]: r for r in reader}


def main():
    run_date = date.today().strftime("%Y%m%d")

    ids_file = latest(WORK_DIR.glob("bga_bgg_ids_*.csv"), "resolver id-list (run the resolver first)")
    bga_file = latest(BGA_DIR.glob("bga_games_*.csv"), "BGA game list")
    zip_file = latest(BGG_DIR.glob("boardgames_ranks_*.zip"), "BGG bulk CSV zip")

    print(f"[care-about] run date   : {run_date}")
    print(f"[care-about] resolver   : {ids_file.name}")
    print(f"[care-about] BGA list   : {bga_file.name}")
    print(f"[care-about] bulk CSV   : {zip_file.name}\n")

    # --- load inputs ---
    with open(ids_file, encoding="utf-8") as f:
        resolved = list(csv.DictReader(f))
    with open(bga_file, encoding="utf-8") as f:
        plays_by_bga = {r["id"]: to_int(r.get("games_played")) for r in csv.DictReader(f)}
    bulk = load_bulk(zip_file)
    print(f"[care-about] bulk games : {len(bulk):,}")

    rows = []            # output rows (dicts)
    bga_claimed = set()  # bgg_ids taken by the BGA side (excluded from the BGG side)

    # counters for the summary
    n_bga_geek = n_bga_plays = n_bga_both = 0
    n_no_bulk = n_bga_cut = 0

    # --- BGA side ---
    for r in resolved:
        bgg_id = r["bgg_id"].strip()
        if not bgg_id:                       # dropped by the resolver -- no id
            continue
        b = bulk.get(bgg_id)
        if b is None:                        # id not in BGG -> no rating -> out
            n_no_bulk += 1
            continue
        if b["is_expansion"] == "1":
            n_bga_cut += 1
            continue
        geek = to_float(b["bayesaverage"])
        plays = plays_by_bga.get(r["bga_id"], 0)
        geek_ok = geek >= BGA_BAR            # geek > 0 implied; a rated game clears the prior
        plays_ok = plays >= PLAYS_BAR
        if not (geek_ok or plays_ok):
            n_bga_cut += 1
            continue
        # plays-only rescue still requires a rating (the hard constraint)
        if plays_ok and not geek_ok and geek <= 0:
            n_no_bulk += 1
            continue

        if geek_ok and plays_ok:
            qualifier = "geek+plays"; n_bga_both += 1
        elif geek_ok:
            qualifier = "geek"; n_bga_geek += 1
        else:
            qualifier = "plays"; n_bga_plays += 1

        bga_claimed.add(bgg_id)
        rows.append({
            "bgg_id": bgg_id, "bgg_name": b["name"], "side": "bga", "qualifier": qualifier,
            "geek": b["bayesaverage"], "votes": b["usersrated"], "rank": b["rank"],
            "year": b["yearpublished"],
            "bga_id": r["bga_id"], "bga_name": r["bga_name"], "bga_plays": plays,
        })

    # --- BGG side: good games not already on BGA ---
    n_bgg = 0
    for bgg_id, b in bulk.items():
        if bgg_id in bga_claimed:
            continue
        if b["is_expansion"] == "1":
            continue
        if to_float(b["bayesaverage"]) < BGG_BAR:
            continue
        n_bgg += 1
        rows.append({
            "bgg_id": bgg_id, "bgg_name": b["name"], "side": "bgg", "qualifier": "geek",
            "geek": b["bayesaverage"], "votes": b["usersrated"], "rank": b["rank"],
            "year": b["yearpublished"],
            "bga_id": "", "bga_name": "", "bga_plays": "",
        })

    # --- write ---
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out_path = WORK_DIR / f"care_about_{run_date}.csv"
    cols = ["bgg_id", "bgg_name", "side", "qualifier", "geek", "votes", "rank", "year",
            "bga_id", "bga_name", "bga_plays"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # --- summary ---
    n_bga = n_bga_geek + n_bga_plays + n_bga_both
    print(f"[care-about] BGA side       : {n_bga:>5}  "
          f"(geek {n_bga_geek}, plays {n_bga_plays}, both {n_bga_both})")
    print(f"[care-about] BGG side net-new: {n_bgg:>5}")
    print(f"[care-about] --------------------------------")
    print(f"[care-about] care-about total: {len(rows):>5}")
    print(f"[care-about] (BGA ids not in BGG / no rating: {n_no_bulk}; "
          f"BGA below bar or expansion: {n_bga_cut})")
    print(f"\n[care-about] list -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
