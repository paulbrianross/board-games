"""combine / resolve_bgg_ids -- BGA -> unique BGG id list (process 1 of BGA pre-dupe).

The one job: turn the daily BGA download into a list of UNIQUE BGG ids to hand to the
next step (the BGG-API lookup). Nothing more. Fetches nothing; reads only the latest BGA
game list plus the hand-maintained overrides. See bga-pre-dupe.md (Executive summary).

Flow (per game):
  1. start from the BGA game's own bgg_id;
  2. apply overrides (combine/bga_bgg_overrides.csv): remap swaps the id, drop removes it;
  3. treat 0 / 54321 / blank as "no BGG id" (junk placeholders -- neither exists in BGG);
  4. enforce uniqueness: if two games still share an id, the most-played game keeps it and
     the rest are dropped on the fly (a breadcrumb for the separate diagnostics pass).

This never blocks: it always emits a clean unique list, then the API step can fire.

Output (committed, dated):
  data/bga/bga_bgg_ids_<date>.csv   cols: bga_id, bga_name, bgg_id, status, note
    status : kept | remapped | dropped-placeholder | dropped-override | dropped-on-the-fly
    note   : populated ONLY for dropped-on-the-fly (which id was lost, to whom, and why)
"""
import csv
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BGA_DIR = ROOT / "data" / "bga"
OVERRIDES = HERE / "bga_bgg_overrides.csv"

PLACEHOLDERS = {"", "0", "54321"}   # junk sentinels; neither 0 nor 54321 exists in BGG


def die(msg):
    print(f"[resolve] ERROR: {msg}")
    sys.exit(1)


def to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def latest(paths, what):
    files = sorted(paths)
    if not files:
        die(f"no {what} found")
    return files[-1]


def main():
    run_date = date.today().strftime("%Y%m%d")

    bga_file = latest(BGA_DIR.glob("bga_games_*.csv"), "BGA game list")
    print(f"[resolve] run date  : {run_date}")
    print(f"[resolve] BGA list  : {bga_file.name}")

    # --- load overrides (hand-maintained corrections), keyed by bga_id ---
    with open(OVERRIDES, encoding="utf-8") as f:
        overrides = {r["bga_id"]: r for r in csv.DictReader(f)}
    print(f"[resolve] overrides : {len(overrides)}")

    # --- load the BGA game list ---
    with open(bga_file, encoding="utf-8") as f:
        bga_games = list(csv.DictReader(f))
    print(f"[resolve] BGA games : {len(bga_games):,}\n")

    # --- steps 1-3: resolve each game to an effective id + provisional status ---
    #     res: bga_id -> {name, plays, bgg_id, status, note}
    res = {}
    for g in bga_games:
        gid = g["id"]
        rec = {
            "name": g.get("display_name_en", ""),
            "plays": to_int(g.get("games_played")),
            "bgg_id": "",
            "status": "",
            "note": "",
        }
        ov = overrides.get(gid)
        if ov and ov["outcome"] == "drop":
            rec["status"] = "dropped-override"
        elif ov and ov["outcome"] == "remap":
            rec["bgg_id"] = ov["bgg_target_id"].strip()
            rec["status"] = "remapped"
        else:
            raw = (g.get("bgg_id") or "").strip()
            if raw in PLACEHOLDERS:
                rec["status"] = "dropped-placeholder"
            else:
                rec["bgg_id"] = raw
                rec["status"] = "kept"
        res[gid] = rec

    # --- step 4: enforce uniqueness. Group live ids; most-played keeps a contested id,
    #     the rest are dropped on the fly. Deterministic tie-break on bga_id. ---
    by_id = {}
    for gid, rec in res.items():
        if rec["bgg_id"]:
            by_id.setdefault(rec["bgg_id"], []).append(gid)

    on_the_fly = 0
    for bgg_id, claimants in by_id.items():
        if len(claimants) == 1:
            continue
        claimants.sort(key=lambda gid: (-res[gid]["plays"], to_int(gid)))
        winner = claimants[0]
        for loser in claimants[1:]:
            res[loser]["bgg_id"] = ""
            res[loser]["status"] = "dropped-on-the-fly"
            res[loser]["note"] = (f"lost {bgg_id} to bga {winner} "
                                  f"'{res[winner]['name']}' -- most-played")
            on_the_fly += 1

    # --- write the committed, dated list (one row per BGA game) ---
    out_path = BGA_DIR / f"bga_bgg_ids_{run_date}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bga_id", "bga_name", "bgg_id", "status", "note"])
        for g in bga_games:            # preserve BGA file order
            rec = res[g["id"]]
            w.writerow([g["id"], rec["name"], rec["bgg_id"], rec["status"], rec["note"]])

    # --- summary ---
    counts = {}
    for rec in res.values():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    unique_ids = sum(1 for rec in res.values() if rec["bgg_id"])

    print(f"[resolve] kept                : {counts.get('kept', 0):>5}")
    print(f"[resolve] remapped            : {counts.get('remapped', 0):>5}")
    print(f"[resolve] dropped-placeholder : {counts.get('dropped-placeholder', 0):>5}")
    print(f"[resolve] dropped-override    : {counts.get('dropped-override', 0):>5}")
    print(f"[resolve] dropped-on-the-fly  : {counts.get('dropped-on-the-fly', 0):>5}")
    print(f"[resolve] --------------------------------")
    print(f"[resolve] unique BGG ids      : {unique_ids:>5} / {len(bga_games)}")
    if on_the_fly:
        print(f"[resolve] NOTE: {on_the_fly} on-the-fly drop(s) -- see diagnostics (process 2)")
    print(f"\n[resolve] list -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
