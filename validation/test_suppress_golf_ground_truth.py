#!/usr/bin/env python3
"""Regression: production --suppress-golf predicate vs 292 All-only labels.

No detector rerun. Uses existing All tiles + local OSM + MapRoulette export.
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diagnostics import golf_stays_inside_for_candidate, should_suppress_golf  # noqa: E402
from validation.analyze_open_area import (  # noqa: E402
    OSM_PATH,
    component_merc_for,
    lat2y,
    load_open_areas,
    lon2x,
)

MR_PATH = ROOT / "validation" / "challenge_56718-ALL-292_tasks.csv"
FULL_PATH = ROOT / "validation" / "all-only-full.csv"
SURVIVE_ID = "14/8320/6233/446/681"
SUPPRESS_ID = "14/8319/6228/191/310"
POSITIVE = frozenset({"Fixed", "Already_Fixed"})


def load_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def norm_status(s):
    return (s or "").strip().replace(" ", "_")


def main():
    full = {r["candidate_id"]: r for r in load_csv(FULL_PATH)}
    mr = load_csv(MR_PATH)
    if len(full) != 292 or len(mr) != 292:
        raise SystemExit(f"Expected 292/292, got full={len(full)} mr={len(mr)}")
    joined = []
    for m in mr:
        cid = m["TaskName"].strip()
        if cid not in full:
            raise SystemExit(f"Join miss: {cid}")
        row = dict(full[cid])
        row["_status"] = norm_status(m.get("TaskStatus"))
        row["_id"] = cid
        parts = cid.split("/")
        row["_z"] = int(parts[0])
        row["_x"] = int(parts[1])
        row["_y"] = int(parts[2])
        row["_peak_row"] = int(parts[3])
        row["_peak_col"] = int(parts[4])
        row["_lon"] = float(row["center_lon"])
        row["_lat"] = float(row["center_lat"])
        joined.append(row)
    if len(joined) != 292:
        raise SystemExit(f"Join {len(joined)} != 292")

    lookup, _counts, _items = load_open_areas(OSM_PATH)
    hits = Counter()
    by_id = {}
    for i, row in enumerate(joined, 1):
        comp, _err = component_merc_for(row, "all")
        suppress, pred = golf_stays_inside_for_candidate(
            lon2x(row["_lon"]), lat2y(row["_lat"]), comp, lookup
        )
        assert suppress == should_suppress_golf(pred)
        row["_suppress"] = suppress
        row["_pred"] = pred
        by_id[row["_id"]] = row
        if suppress:
            hits[row["_status"]] += 1
        if i % 50 == 0 or i == len(joined):
            print(f"  {i}/{len(joined)}", flush=True)

    nai = hits.get("Not_An_Issue", 0)
    pos = hits.get("Fixed", 0) + hits.get("Already_Fixed", 0)
    th = hits.get("Too_Hard", 0)
    skipped = hits.get("Skipped", 0)
    print(
        f"golf stays_inside hits: NAI={nai} positive={pos} "
        f"Too_Hard={th} Skipped={skipped}"
    )
    if nai != 35 or pos != 1 or th != 0 or skipped != 0:
        raise SystemExit(
            f"Predicate mismatch: expected 35 NAI / 1 positive / 0 TH / 0 Skipped, "
            f"got {nai}/{pos}/{th}/{skipped}"
        )
    if SURVIVE_ID not in by_id:
        raise SystemExit(f"Missing {SURVIVE_ID}")
    if by_id[SURVIVE_ID]["_suppress"]:
        raise SystemExit(f"{SURVIVE_ID} must survive (crosses golf boundary)")
    if SUPPRESS_ID not in by_id:
        raise SystemExit(f"Missing {SUPPRESS_ID}")
    if not by_id[SUPPRESS_ID]["_suppress"]:
        raise SystemExit(f"{SUPPRESS_ID} must be suppressed (stays_inside)")
    print(f"{SURVIVE_ID} survives (crosses={by_id[SURVIVE_ID]['_pred']['crosses']})")
    print(f"{SUPPRESS_ID} suppressed (stays_inside={by_id[SUPPRESS_ID]['_pred']['stays_inside']})")
    print("OK: --suppress-golf predicate matches 292-task ground truth")


if __name__ == "__main__":
    main()
