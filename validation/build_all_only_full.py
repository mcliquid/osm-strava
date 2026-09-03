#!/usr/bin/env python3
"""Build full All-only review set from existing FINAL diagnostics.

No detector rerun. Spatial definition matches all-only-sample construction:
All finals minus Ride@25m minus sport_Run@25m.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyze_diagnostics import load_csv  # noqa: E402
from diagnostics import DIAGNOSTIC_COLUMNS, open_area_metrics_for_candidate  # noqa: E402
from validation.analyze_open_area import (  # noqa: E402
    component_merc_for,
    lat2y,
    load_open_areas,
    lon2x,
)
from validation.sample_heatmap_review import (  # noqa: E402
    annotate_matches,
    load_finals,
)

SOURCES = {
    "ride": ROOT / "diagnostics-mallorca-ride-ab.csv",
    "sport_Run": ROOT / "diagnostics-mallorca-sportrun-ab.csv",
    "all": ROOT / "diagnostics-mallorca-all-ab.csv",
}
SAMPLE_CSV = ROOT / "validation" / "all-only-sample.csv"
OUT_GEOJSON = ROOT / "validation" / "all-only-full.geojson"
OUT_CSV = ROOT / "validation" / "all-only-full.csv"

REVIEW_EXTRA = [
    "review_layer",
    "nearest_osm_context",
    "matched_ride_25m",
    "matched_sport_run_25m",
    "matched_all_25m",
    "nearest_ride_distance_m",
    "nearest_sport_run_distance_m",
    "nearest_all_distance_m",
    "nearest_cross_layer_distance_m",
]


def fmt_m(value):
    if value is None:
        return ""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def feature_properties(row):
    props = {}
    for field in DIAGNOSTIC_COLUMNS:
        if field in row:
            props[field] = row[field]
    props["candidate_id"] = row["_id"]
    props["id"] = row["_id"]
    props["review_layer"] = "all"
    props["nearest_osm_context"] = row["_context"]
    props["matched_ride_25m"] = bool(row["_m_ride"])
    props["matched_sport_run_25m"] = bool(row["_m_run"])
    props["matched_all_25m"] = True
    props["nearest_ride_distance_m"] = fmt_m(row["_d_ride"])
    props["nearest_sport_run_distance_m"] = fmt_m(row["_d_run"])
    props["nearest_all_distance_m"] = fmt_m(row["_d_all"])
    cross = [d for d in (row["_d_ride"], row["_d_run"]) if d is not None]
    props["nearest_cross_layer_distance_m"] = fmt_m(min(cross) if cross else None)
    return props


def apply_open_area_metrics(rows, lookup):
    """Attach primary open_area_* fields from existing OSM + All tile cache."""
    for i, row in enumerate(rows, 1):
        parts = row["_id"].split("/")
        row["_z"] = int(parts[0])
        row["_x"] = int(parts[1])
        row["_y"] = int(parts[2])
        row["_peak_row"] = int(parts[3])
        row["_peak_col"] = int(parts[4])
        comp, _err = component_merc_for(row, "all")
        mx, my = lon2x(row["_lon"]), lat2y(row["_lat"])
        metrics = open_area_metrics_for_candidate(mx, my, comp, lookup)
        for key, value in metrics.items():
            row[key] = value
        if i % 50 == 0 or i == len(rows):
            print(f"  open-area metrics {i}/{len(rows)}", flush=True)


def main():
    ride = load_finals(SOURCES["ride"])
    sport_run = load_finals(SOURCES["sport_Run"])
    all_rows = load_finals(SOURCES["all"])
    annotate_matches(all_rows, ride, sport_run, all_rows, "all")

    n_all = len(all_rows)
    n_ride_ex = sum(1 for r in all_rows if r["_m_ride"])
    n_run_ex = sum(1 for r in all_rows if r["_m_run"])
    n_union_ex = sum(1 for r in all_rows if r["_m_ride"] or r["_m_run"])
    only = [r for r in all_rows if not r["_m_ride"] and not r["_m_run"]]
    only.sort(key=lambda r: r["_id"])

    print(f"source All finals: {n_all}")
    print(f"Ride exclusions @25m: {n_ride_ex}")
    print(f"sport_Run exclusions @25m: {n_run_ex}")
    print(f"union exclusions (Ride OR sport_Run) @25m: {n_union_ex}")
    print(f"All-only count: {len(only)}")
    if len(only) != 292:
        raise SystemExit(f"Expected 292 All-only candidates, got {len(only)}")

    sample_ids = {r["candidate_id"] for r in load_csv(SAMPLE_CSV)}
    only_ids = {r["_id"] for r in only}
    inter = sample_ids & only_ids
    missing = sorted(sample_ids - only_ids)
    print(f"sample intersection: {len(inter)}/{len(sample_ids)}")
    if missing:
        raise SystemExit(f"Sample candidates missing from full set: {missing}")
    if len(inter) != 50:
        raise SystemExit(f"Expected sample intersection 50, got {len(inter)}")

    print("Loading open-area OSM context (existing local extract)...", flush=True)
    lookup, _counts, _items = load_open_areas(ROOT / "osm-data" / "mallorca" / "current.osm")
    apply_open_area_metrics(only, lookup)

    features = []
    for row in only:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["_lon"], row["_lat"]],
            },
            "properties": feature_properties(row),
        })
    payload = {
        "type": "FeatureCollection",
        "name": "all-only-full",
        "features": features,
    }
    OUT_GEOJSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    fieldnames = list(DIAGNOSTIC_COLUMNS)
    for extra in REVIEW_EXTRA:
        if extra not in fieldnames:
            fieldnames.append(extra)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in only:
            props = feature_properties(row)
            out = {k: props.get(k, "") for k in fieldnames}
            writer.writerow(out)

    written = json.loads(OUT_GEOJSON.read_text(encoding="utf-8"))
    written_ids = [f["properties"]["candidate_id"] for f in written["features"]]
    assert len(written_ids) == 292
    assert len(set(written_ids)) == 292
    assert sample_ids <= set(written_ids)

    csv_rows = load_csv(OUT_CSV)
    assert len(csv_rows) == 292
    assert {r["candidate_id"] for r in csv_rows} == set(written_ids)

    open_filled = sum(1 for r in csv_rows if (r.get("open_area_class") or "").strip())
    print(f"wrote {OUT_GEOJSON} ({len(written_ids)} features)")
    print(f"wrote {OUT_CSV} ({len(csv_rows)} rows)")
    print(f"open_area_class populated: {open_filled}/{len(csv_rows)}")
    print("OK: 292 All-only, sample 50/50 present")


if __name__ == "__main__":
    main()
