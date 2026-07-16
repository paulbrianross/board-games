"""Derive the lean machine-read override file from the hand-maintained master.

  reads : combine/bga_bgg_manual_checks.csv   (the human source of truth)
  writes: combine/bga_bgg_overrides.csv        (the thin file the resolver applies)

The override file is just the master rows whose outcome is 'remap' or 'drop'
(a 'keep' row means the raw id is correct, so there is nothing to override -- the
resolver keeps the raw id by default). Re-run this any time the master changes.

Lean schema: bga_id, bga_name, outcome, bgg_raw_id, bgg_target_id, reason
  - bgg_target_id is the corrected id for a remap, blank for a drop.
"""

import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
MASTER = HERE / "bga_bgg_manual_checks.csv"
OUT = HERE / "bga_bgg_overrides.csv"

HEADER = ["bga_id", "bga_name", "outcome", "bgg_raw_id", "bgg_target_id", "reason"]


def mid_name(composite):
    """'1305 - Liverpool (Cozy Oaks) Rummy - 42,350 plays' -> the name segment."""
    parts = composite.split(" · ")
    return parts[1].strip() if len(parts) >= 2 else composite.strip()


def first_id(composite):
    """'15878 - Rummy' -> '15878' (blank in, blank out)."""
    composite = composite.strip()
    return composite.split(" · ", 1)[0].strip() if composite else ""


out_rows = []
with MASTER.open(encoding="utf-8", newline="") as f:
    for r in csv.DictReader(f):
        if r["outcome"] not in ("remap", "drop"):
            continue
        out_rows.append({
            "bga_id": r["bga_id"],
            "bga_name": mid_name(r["bga_game"]),
            "outcome": r["outcome"],
            "bgg_raw_id": first_id(r["bgg_raw"]),
            "bgg_target_id": r["remap_target_id"].strip(),
            "reason": r["ruling"].strip(),
        })

with OUT.open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(out_rows)

from collections import Counter
oc = Counter(r["outcome"] for r in out_rows)
print(f"override rows: {len(out_rows)}  -> {OUT}")
print(f"  remap={oc['remap']}  drop={oc['drop']}")
