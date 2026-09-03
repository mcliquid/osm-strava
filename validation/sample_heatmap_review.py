#!/usr/bin/env python3
"""Build deterministic Mallorca review samples from existing FINAL candidates.

Read-only on detector output. Does not change thresholds, suppression, or
candidate_id. Spatial matching uses center coordinates, not candidate_id.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyze_diagnostics import load_csv, parse_bool, parse_float  # noqa: E402
from diagnostics import DIAGNOSTIC_COLUMNS  # noqa: E402

RADIUS_M = 25.0
QUOTA = {
    "path": 20,
    "track": 10,
    "footway": 5,
    "road/service": 10,
    "other": 5,
}
ROADS = frozenset({
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "pedestrian", "road", "corridor", "escape",
    "raceway",
})
SOURCES = {
    "ride": ROOT / "diagnostics-mallorca-ride-ab.csv",
    "sport_Run": ROOT / "diagnostics-mallorca-sportrun-ab.csv",
    "all": ROOT / "diagnostics-mallorca-all-ab.csv",
}
MANIFEST_FIELDS = [
    "candidate_id",
    "lon",
    "lat",
    "nearest_osm_context",
    "nearest_osm_type",
    "nearest_osm_highway",
    "nearest_osm_distance_m",
    "component_pixels",
    "strava_mean",
    "strava_max",
    "strava_p90",
    "between_heat_ratio",
    "heat_halo_score",
    "matched_ride_25m",
    "matched_sport_run_25m",
    "matched_all_25m",
    "nearest_cross_layer_distance_m",
]


def haversine_m(lon1, lat1, lon2, lat2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def osm_context(row):
    hwy = (row.get("nearest_osm_highway") or "").strip()
    leisure = (row.get("nearest_osm_leisure") or "").strip()
    route = (row.get("nearest_osm_route") or "").strip()
    tags_raw = row.get("nearest_osm_tags") or ""
    tags = {}
    if tags_raw:
        try:
            tags = json.loads(tags_raw)
        except json.JSONDecodeError:
            tags = {}
    if leisure in ("pitch", "track") or tags.get("leisure") in ("pitch", "track"):
        return "other"
    if (
        route == "ferry"
        or tags.get("route") == "ferry"
        or tags.get("natural") in ("coastline", "water", "bay", "beach")
        or tags.get("waterway")
        or hwy == "ferry"
    ):
        return "other"
    if hwy == "path":
        return "path"
    if hwy == "footway":
        return "footway"
    if hwy == "track":
        return "track"
    if hwy == "service" or hwy in ROADS:
        return "road/service"
    return "other"


def load_finals(path):
    out = []
    for row in load_csv(path):
        if not parse_bool(row.get("written_to_geojson")):
            continue
        lon = parse_float(row.get("center_lon"))
        lat = parse_float(row.get("center_lat"))
        if lon is None or lat is None:
            continue
        rec = dict(row)
        rec["_lon"] = lon
        rec["_lat"] = lat
        rec["_context"] = osm_context(rec)
        rec["_id"] = rec.get("candidate_id") or ""
        out.append(rec)
    return out


def nearest_distance(point, others, skip_self=False):
    best = None
    pid = point["_id"]
    for other in others:
        if skip_self and other["_id"] == pid:
            continue
        d = haversine_m(point["_lon"], point["_lat"], other["_lon"], other["_lat"])
        if best is None or d < best:
            best = d
    return best


def annotate_matches(rows, ride, sport_run, all_rows, source):
    for row in rows:
        d_ride = nearest_distance(row, ride, skip_self=(source == "ride"))
        d_run = nearest_distance(row, sport_run, skip_self=(source == "sport_Run"))
        d_all = nearest_distance(row, all_rows, skip_self=(source == "all"))
        row["_d_ride"] = d_ride
        row["_d_run"] = d_run
        row["_d_all"] = d_all
        row["_m_ride"] = True if source == "ride" else (
            d_ride is not None and d_ride <= RADIUS_M
        )
        row["_m_run"] = True if source == "sport_Run" else (
            d_run is not None and d_run <= RADIUS_M
        )
        row["_m_all"] = True if source == "all" else (
            d_all is not None and d_all <= RADIUS_M
        )


def farthest_point_sample(pool, k):
    if k <= 0 or not pool:
        return []
    ordered = sorted(pool, key=lambda r: r["_id"])
    if len(ordered) <= k:
        return ordered
    seed = min(ordered, key=lambda r: (r["_lon"], r["_lat"], r["_id"]))
    selected = [seed]
    remaining = [r for r in ordered if r["_id"] != seed["_id"]]
    while len(selected) < k and remaining:
        def score(cand):
            mind = min(
                haversine_m(cand["_lon"], cand["_lat"], s["_lon"], s["_lat"])
                for s in selected
            )
            return (mind, cand["_id"])
        best = max(remaining, key=score)
        selected.append(best)
        remaining = [r for r in remaining if r["_id"] != best["_id"]]
    return selected


def sample_stratified(pool, quota):
    by_class = {key: [] for key in quota}
    for row in pool:
        cls = row["_context"] if row["_context"] in quota else "other"
        by_class[cls].append(row)
    selected = []
    actual = {}
    for cls, n in quota.items():
        picked = farthest_point_sample(by_class.get(cls, []), n)
        actual[cls] = len(picked)
        selected.extend(picked)
    selected.sort(key=lambda r: r["_id"])
    return selected, actual


def fmt_m(value):
    if value is None:
        return ""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def feature_properties(row, review_layer, cross_keys):
    props = {}
    for field in DIAGNOSTIC_COLUMNS:
        if field in row:
            props[field] = row[field]
    props["candidate_id"] = row["_id"]
    props["id"] = row["_id"]
    props["review_layer"] = review_layer
    props["nearest_osm_context"] = row["_context"]
    props["matched_ride_25m"] = bool(row["_m_ride"])
    props["matched_sport_run_25m"] = bool(row["_m_run"])
    props["matched_all_25m"] = bool(row["_m_all"])
    props["nearest_ride_distance_m"] = fmt_m(row["_d_ride"])
    props["nearest_sport_run_distance_m"] = fmt_m(row["_d_run"])
    props["nearest_all_distance_m"] = fmt_m(row["_d_all"])
    cross = [row[k] for k in cross_keys if row.get(k) is not None]
    props["nearest_cross_layer_distance_m"] = fmt_m(min(cross) if cross else None)
    return props


def write_geojson(path, rows, review_layer, cross_keys):
    features = []
    for row in rows:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["_lon"], row["_lat"]],
            },
            "properties": feature_properties(row, review_layer, cross_keys),
        })
    payload = {
        "type": "FeatureCollection",
        "name": path.stem,
        "features": features,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_manifest(path, rows, cross_keys):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            cross = [row[k] for k in cross_keys if row.get(k) is not None]
            writer.writerow({
                "candidate_id": row["_id"],
                "lon": f"{row['_lon']:.10f}".rstrip("0").rstrip("."),
                "lat": f"{row['_lat']:.10f}".rstrip("0").rstrip("."),
                "nearest_osm_context": row["_context"],
                "nearest_osm_type": row.get("nearest_osm_type") or "",
                "nearest_osm_highway": row.get("nearest_osm_highway") or "",
                "nearest_osm_distance_m": row.get("nearest_osm_distance_m") or "",
                "component_pixels": row.get("component_pixels") or "",
                "strava_mean": row.get("strava_mean") or "",
                "strava_max": row.get("strava_max") or "",
                "strava_p90": row.get("strava_p90") or "",
                "between_heat_ratio": row.get("between_heat_ratio") or "",
                "heat_halo_score": row.get("heat_halo_score") or "",
                "matched_ride_25m": str(bool(row["_m_ride"])).lower(),
                "matched_sport_run_25m": str(bool(row["_m_run"])).lower(),
                "matched_all_25m": str(bool(row["_m_all"])).lower(),
                "nearest_cross_layer_distance_m": fmt_m(min(cross) if cross else None),
            })


def lon_band(lon):
    if lon < 2.7:
        return "west"
    if lon < 3.1:
        return "central"
    return "east"


def lat_band(lat):
    if lat >= 39.65:
        return "north"
    if lat >= 39.5:
        return "mid"
    return "south"


def geo_report(rows):
    bands = Counter(f"{lon_band(r['_lon'])}-{lat_band(r['_lat'])}" for r in rows)
    lons = [r["_lon"] for r in rows]
    lats = [r["_lat"] for r in rows]
    tiles = sorted({"/".join(r["_id"].split("/")[:3]) for r in rows})
    return {
        "bbox": [min(lons), min(lats), max(lons), max(lats)] if rows else None,
        "bands": dict(sorted(bands.items())),
        "unique_tiles": len(tiles),
    }


def print_sample_report(title, source_n, pool_n, excluded, selected, actual, extra):
    print(f"\n=== {title} ===")
    print(f"source finals: {source_n}")
    print(f"eligible after cross-layer filter: {pool_n}")
    for key, n in excluded.items():
        print(f"excluded {key}: {n}")
    print(f"sample size: {len(selected)}")
    print("quota vs actual:")
    for cls, n in QUOTA.items():
        print(f"  {cls}: quota {n}, actual {actual.get(cls, 0)}, pool {extra['pool_class'].get(cls, 0)}")
    geo = geo_report(selected)
    print(f"bbox lon/lat: {geo['bbox']}")
    print(f"unique z/x/y tiles: {geo['unique_tiles']}")
    print("geographic bands (west/central/east x north/mid/south):")
    for band, n in geo["bands"].items():
        print(f"  {band}: {n}")


def main():
    ride = load_finals(SOURCES["ride"])
    sport_run = load_finals(SOURCES["sport_Run"])
    all_rows = load_finals(SOURCES["all"])
    annotate_matches(sport_run, ride, sport_run, all_rows, "sport_Run")
    annotate_matches(all_rows, ride, sport_run, all_rows, "all")

    run_pool = [r for r in sport_run if not r["_m_ride"]]
    run_excluded = {
        "sport_Run matched Ride @25m": sum(1 for r in sport_run if r["_m_ride"]),
    }
    run_pool_class = Counter(r["_context"] for r in run_pool)
    run_selected, run_actual = sample_stratified(run_pool, QUOTA)
    run_cross = ["_d_ride", "_d_all"]
    write_geojson(ROOT / "validation" / "run-sample.geojson", run_selected, "sport_Run", run_cross)
    write_manifest(ROOT / "validation" / "run-sample.csv", run_selected, run_cross)
    print_sample_report(
        "run-sample (sport_Run, prefer no Ride @25m)",
        len(sport_run),
        len(run_pool),
        run_excluded,
        run_selected,
        run_actual,
        {"pool_class": dict(run_pool_class)},
    )

    all_pool = [r for r in all_rows if not r["_m_ride"] and not r["_m_run"]]
    all_excluded = {
        "All matched Ride @25m": sum(1 for r in all_rows if r["_m_ride"]),
        "All matched sport_Run @25m": sum(1 for r in all_rows if r["_m_run"]),
        "All matched Ride or sport_Run @25m": sum(
            1 for r in all_rows if r["_m_ride"] or r["_m_run"]
        ),
    }
    all_pool_class = Counter(r["_context"] for r in all_pool)
    all_selected, all_actual = sample_stratified(all_pool, QUOTA)
    all_cross = ["_d_ride", "_d_run"]
    write_geojson(
        ROOT / "validation" / "all-only-sample.geojson",
        all_selected,
        "all",
        all_cross,
    )
    write_manifest(
        ROOT / "validation" / "all-only-sample.csv",
        all_selected,
        all_cross,
    )
    print_sample_report(
        "all-only-sample (All minus Ride and sport_Run @25m)",
        len(all_rows),
        len(all_pool),
        all_excluded,
        all_selected,
        all_actual,
        {"pool_class": dict(all_pool_class)},
    )

    run_ids = {r["_id"] for r in run_selected}
    all_ids = {r["_id"] for r in all_selected}
    print(f"\nexact candidate_id overlap between samples: {len(run_ids & all_ids)}")
    print("wrote validation/run-sample.geojson")
    print("wrote validation/run-sample.csv")
    print("wrote validation/all-only-sample.geojson")
    print("wrote validation/all-only-sample.csv")


if __name__ == "__main__":
    main()
