#!/usr/bin/env python3
"""Offline spatial comparison of three fresh Mallorca production GeoJSON outputs.

Compares Ride, sport_Run, and All candidate layers at multiple distance
thresholds using Haversine distance between candidate center coordinates.

Usage:
    python validation/analyze_fresh_layers.py
"""

import json
import math
import os
import sys
import csv
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VALIDATION = REPO / "validation"

INPUT_FILES = {
    "Ride": VALIDATION / "mallorca-fresh-ride.geojson",
    "Run": VALIDATION / "mallorca-fresh-run.geojson",
    "All": VALIDATION / "mallorca-fresh-all.geojson",
}

EXPECTED_COUNTS = {"Ride": 15, "Run": 145, "All": 98}

THRESHOLDS_M = [10, 25, 50, 75, 100]

HISTOGRAM_BUCKETS = [
    (0, 10, "0-10 m"),
    (10, 25, "10-25 m"),
    (25, 50, "25-50 m"),
    (50, 75, "50-75 m"),
    (75, 100, "75-100 m"),
    (100, 250, "100-250 m"),
    (250, float("inf"), ">250 m"),
]


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_geojson_seq(path):
    """Load a GeoJSON-Sequence file (one FeatureCollection per line)."""
    features = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            fc = json.loads(line)
            for feat in fc.get("features", []):
                features.append(feat)
    return features


def extract_candidates(features):
    """Return list of dicts with id, lon, lat, properties."""
    cands = []
    for f in features:
        props = f["properties"]
        cands.append({
            "candidate_id": props["id"],
            "lon": float(props["longitude"]),
            "lat": float(props["latitude"]),
            "properties": props,
        })
    return cands


# ---------------------------------------------------------------------------
# Haversine
# ---------------------------------------------------------------------------

_R = 6_371_000.0  # Earth radius in metres


def haversine_m(lon1, lat1, lon2, lat2):
    """Haversine distance in metres between two WGS-84 points."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * _R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Nearest-distance computation
# ---------------------------------------------------------------------------

def nearest_distances(src_cands, tgt_cands):
    """For each src candidate return (nearest_distance_m, nearest_tgt_id)."""
    results = []
    for s in src_cands:
        best_d = float("inf")
        best_id = None
        for t in tgt_cands:
            d = haversine_m(s["lon"], s["lat"], t["lon"], t["lat"])
            if d < best_d:
                best_d = d
                best_id = t["candidate_id"]
        results.append((s["candidate_id"], best_d, best_id))
    return results


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def count_within(nearest_dists, threshold):
    return sum(1 for _, d, _ in nearest_dists if d <= threshold)


def percentile(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def dist_stats(nearest_dists):
    vals = [d for _, d, _ in nearest_dists]
    if not vals:
        return {}
    return {
        "min": min(vals),
        "p10": percentile(vals, 10),
        "p25": percentile(vals, 25),
        "median": percentile(vals, 50),
        "p75": percentile(vals, 75),
        "p90": percentile(vals, 90),
        "max": max(vals),
    }


def histogram(nearest_dists):
    counts = []
    vals = [d for _, d, _ in nearest_dists]
    for lo, hi, label in HISTOGRAM_BUCKETS:
        c = sum(1 for v in vals if lo < v <= hi) if lo > 0 else sum(1 for v in vals if v <= hi)
        counts.append((label, c))
    return counts


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def main():
    # Load data
    layers = {}
    for name, path in INPUT_FILES.items():
        if not path.exists():
            print(f"ERROR: {path} not found", file=sys.stderr)
            sys.exit(1)
        feats = load_geojson_seq(path)
        cands = extract_candidates(feats)
        layers[name] = cands

    # Validate counts
    for name, expected in EXPECTED_COUNTS.items():
        actual = len(layers[name])
        if actual != expected:
            print(f"FAIL: {name} has {actual} candidates, expected {expected}", file=sys.stderr)
            sys.exit(1)

    # Validate unique IDs
    for name, cands in layers.items():
        ids = [c["candidate_id"] for c in cands]
        if len(ids) != len(set(ids)):
            dupes = [i for i in ids if ids.count(i) > 1]
            print(f"FAIL: {name} has duplicate IDs: {set(dupes)}", file=sys.stderr)
            sys.exit(1)

    print(f"Ride: {len(layers['Ride'])}")
    print(f"Run: {len(layers['Run'])}")
    print(f"All: {len(layers['All'])}")
    print()

    # Compute all pairwise nearest distances
    pairs = [
        ("Ride", "Run"), ("Run", "Ride"),
        ("Ride", "All"), ("All", "Ride"),
        ("Run", "All"), ("All", "Run"),
    ]
    nd = {}
    for src, tgt in pairs:
        nd[(src, tgt)] = nearest_distances(layers[src], layers[tgt])

    lines = []  # markdown output
    w = lines.append

    w("# Mallorca Fresh Layer Spatial Comparison")
    w("")
    w(f"- Ride: {len(layers['Ride'])} candidates")
    w(f"- sport_Run: {len(layers['Run'])} candidates")
    w(f"- All: {len(layers['All'])} candidates")
    w("")

    # --- Directional overlap ---
    w("## Directional Overlap")
    w("")
    pair_labels = [
        ("Ride", "Run", "Ride -> Run"),
        ("Run", "Ride", "Run -> Ride"),
        ("Ride", "All", "Ride -> All"),
        ("All", "Ride", "All -> Ride"),
        ("Run", "All", "Run -> All"),
        ("All", "Run", "All -> Run"),
    ]
    w("| Direction | Total |" + "|".join(f" <={t}m " for t in THRESHOLDS_M) + "|")
    w("|---|---|" + "|".join("---" for _ in THRESHOLDS_M) + "|")
    for src, tgt, label in pair_labels:
        total = len(layers[src])
        cells = []
        for t in THRESHOLDS_M:
            c = count_within(nd[(src, tgt)], t)
            cells.append(f"{c} ({c*100/total:.0f}%)")
        w(f"| {label} | {total} | " + " | ".join(cells) + " |")
    w("")

    # --- All-only analysis ---
    w("## All-Only Analysis")
    w("")
    w("For each All candidate, nearest distance to Ride OR sport_Run (union).")
    w("")

    # Compute union nearest for All
    all_union_nearest = []
    for c in layers["All"]:
        best_d = float("inf")
        best_id = None
        for other_name in ["Ride", "Run"]:
            for t in layers[other_name]:
                d = haversine_m(c["lon"], c["lat"], t["lon"], t["lat"])
                if d < best_d:
                    best_d = d
                    best_id = t["candidate_id"]
        all_union_nearest.append((c["candidate_id"], best_d, best_id))

    w("| Threshold | Represented | All-only |")
    w("|---|---|---|")
    total_all = len(layers["All"])
    for t in THRESHOLDS_M:
        rep = count_within(all_union_nearest, t)
        w(f"| <={t}m | {rep} ({rep*100/total_all:.0f}%) | {total_all - rep} ({(total_all-rep)*100/total_all:.0f}%) |")
    w("")

    # Also report All -> Ride and All -> Run separately
    w("### All: Nearest Ride vs Nearest Run")
    w("")
    w("| All candidate | nearest_ride_m | nearest_ride_id | nearest_run_m | nearest_run_id |")
    w("|---|---|---|---|---|")
    ride_map = {cid: (d, tid) for cid, d, tid in nd[("All", "Ride")]}
    run_map = {cid: (d, tid) for cid, d, tid in nd[("All", "Run")]}
    # Just summary, not all 98 rows - show counts per bucket
    w("")
    w("*(Full per-candidate table omitted for brevity; see CSV output.)*")
    w("")

    # --- Run-only analysis ---
    w("## Run-Only Analysis")
    w("")
    w("sport_Run candidates not represented in Ride OR All (union).")
    w("")

    run_union_nearest = []
    for c in layers["Run"]:
        best_d = float("inf")
        best_id = None
        for other_name in ["Ride", "All"]:
            for t in layers[other_name]:
                d = haversine_m(c["lon"], c["lat"], t["lon"], t["lat"])
                if d < best_d:
                    best_d = d
                    best_id = t["candidate_id"]
        run_union_nearest.append((c["candidate_id"], best_d, best_id))

    total_run = len(layers["Run"])
    w("| Threshold | Represented | Run-only |")
    w("|---|---|---|")
    for t in THRESHOLDS_M:
        rep = count_within(run_union_nearest, t)
        w(f"| <={t}m | {rep} ({rep*100/total_run:.0f}%) | {total_run - rep} ({(total_run-rep)*100/total_run:.0f}%) |")
    w("")

    # --- Three-layer overlap ---
    w("## Three-Layer Overlap Categories")
    w("")
    w("Methodology: For each candidate in each layer, check whether it has a")
    w("match (within threshold) in each of the other two layers. A candidate is")
    w("'matched' to another layer if its nearest candidate in that layer is")
    w("within the threshold. Categories are assigned per-candidate and summed.")
    w("")
    w("Note: Because matching is directional and not transitive, the same")
    w("physical location may be counted in different categories depending on")
    w("which layer's candidate is being classified. We report from each layer's")
    w("perspective and also union counts.")
    w("")

    for t in THRESHOLDS_M:
        w(f"### Threshold: {t}m")
        w("")

        # Classify each layer's candidates
        cats = defaultdict(int)

        for c in layers["Ride"]:
            cid = c["candidate_id"]
            has_run = dict((sid, d) for sid, d, _ in nd[("Ride", "Run")]).get(cid, float("inf")) <= t
            has_all = dict((sid, d) for sid, d, _ in nd[("Ride", "All")]).get(cid, float("inf")) <= t
            if has_run and has_all:
                cats["Ride+Run+All"] += 1
            elif has_run:
                cats["Ride+Run"] += 1
            elif has_all:
                cats["Ride+All"] += 1
            else:
                cats["Ride only"] += 1

        for c in layers["Run"]:
            cid = c["candidate_id"]
            has_ride = dict((sid, d) for sid, d, _ in nd[("Run", "Ride")]).get(cid, float("inf")) <= t
            has_all = dict((sid, d) for sid, d, _ in nd[("Run", "All")]).get(cid, float("inf")) <= t
            if has_ride and has_all:
                cats["Run+Ride+All"] += 1
            elif has_ride:
                cats["Run+Ride"] += 1
            elif has_all:
                cats["Run+All"] += 1
            else:
                cats["Run only"] += 1

        for c in layers["All"]:
            cid = c["candidate_id"]
            has_ride = dict((sid, d) for sid, d, _ in nd[("All", "Ride")]).get(cid, float("inf")) <= t
            has_run = dict((sid, d) for sid, d, _ in nd[("All", "Run")]).get(cid, float("inf")) <= t
            if has_ride and has_run:
                cats["All+Ride+Run"] += 1
            elif has_ride:
                cats["All+Ride"] += 1
            elif has_run:
                cats["All+Run"] += 1
            else:
                cats["All only"] += 1

        w("| Category (from each layer's perspective) | Count |")
        w("|---|---|")
        for k in ["Ride only", "Ride+Run", "Ride+All", "Ride+Run+All",
                   "Run only", "Run+Ride", "Run+All", "Run+Ride+All",
                   "All only", "All+Ride", "All+Run", "All+Ride+Run"]:
            if cats.get(k, 0) > 0:
                w(f"| {k} | {cats[k]} |")
        w("")

    # --- Nearest-distance distributions ---
    w("## Nearest-Distance Distributions")
    w("")
    dist_pairs = [
        ("All", "Run"), ("All", "Ride"),
        ("Run", "All"), ("Ride", "All"),
    ]
    for src, tgt in dist_pairs:
        w(f"### {src} -> {tgt}")
        w("")
        stats = dist_stats(nd[(src, tgt)])
        w("| Statistic | Value (m) |")
        w("|---|---|")
        for k in ["min", "p10", "p25", "median", "p75", "p90", "max"]:
            w(f"| {k} | {stats[k]:.1f} |")
        w("")

        w("| Bucket | Count |")
        w("|---|---|")
        for label, count in histogram(nd[(src, tgt)]):
            w(f"| {label} | {count} |")
        w("")

    # --- Same-corridor investigation ---
    w("## Same-Corridor Investigation: Run <-> All (25-100m)")
    w("")
    w("Pairs where sport_Run and All candidate centers are >25m and <=100m apart.")
    w("These may represent the same physical path with offset detection peaks.")
    w("")

    corridor_pairs = []
    for sid, d, tid in nd[("Run", "All")]:
        if 25 < d <= 100:
            run_c = next(c for c in layers["Run"] if c["candidate_id"] == sid)
            all_c = next(c for c in layers["All"] if c["candidate_id"] == tid)
            corridor_pairs.append({
                "run_id": sid,
                "all_id": tid,
                "distance_m": d,
                "run_lon": run_c["lon"],
                "run_lat": run_c["lat"],
                "all_lon": all_c["lon"],
                "all_lat": all_c["lat"],
                "run_size": run_c["properties"].get("size", ""),
                "all_size": all_c["properties"].get("size", ""),
            })

    # Also check All -> Run direction for additional corridor pairs
    for sid, d, tid in nd[("All", "Run")]:
        if 25 < d <= 100:
            all_c = next(c for c in layers["All"] if c["candidate_id"] == sid)
            run_c = next(c for c in layers["Run"] if c["candidate_id"] == tid)
            key = (tid, sid)  # (run_id, all_id)
            if not any((p["run_id"], p["all_id"]) == key for p in corridor_pairs):
                corridor_pairs.append({
                    "run_id": tid,
                    "all_id": sid,
                    "distance_m": d,
                    "run_lon": run_c["lon"],
                    "run_lat": run_c["lat"],
                    "all_lon": all_c["lon"],
                    "all_lat": all_c["lat"],
                    "run_size": run_c["properties"].get("size", ""),
                    "all_size": all_c["properties"].get("size", ""),
                })

    corridor_pairs.sort(key=lambda p: p["distance_m"])

    w(f"Total review pairs: {len(corridor_pairs)}")
    w("")
    if corridor_pairs:
        w("| Run ID | All ID | Distance (m) | Run coords | All coords | Run size | All size |")
        w("|---|---|---|---|---|---|---|")
        for p in corridor_pairs:
            w(f"| {p['run_id']} | {p['all_id']} | {p['distance_m']:.1f} | "
              f"{p['run_lon']:.6f}, {p['run_lat']:.6f} | "
              f"{p['all_lon']:.6f}, {p['all_lat']:.6f} | "
              f"{p['run_size']} | {p['all_size']} |")
        w("")

    # --- Write markdown ---
    md_path = VALIDATION / "mallorca-fresh-layer-analysis.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {md_path}")

    # --- Write CSV ---
    csv_path = VALIDATION / "mallorca-fresh-layer-analysis.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "layer", "candidate_id", "lon", "lat", "size",
            "nearest_ride_m", "nearest_ride_id",
            "nearest_run_m", "nearest_run_id",
            "nearest_all_m", "nearest_all_id",
        ])
        for layer_name in ["Ride", "Run", "All"]:
            for c in layers[layer_name]:
                cid = c["candidate_id"]
                row = [layer_name, cid, f"{c['lon']:.10f}", f"{c['lat']:.10f}",
                       c["properties"].get("size", "")]
                for other in ["Ride", "Run", "All"]:
                    if other == layer_name:
                        row.extend(["", ""])
                    else:
                        match = next((d, tid) for sid, d, tid in nd[(layer_name, other)] if sid == cid)
                        row.extend([f"{match[0]:.2f}", match[1]])
                writer.writerow(row)
    print(f"Wrote {csv_path}")

    # --- Write review GeoJSONs ---
    # all-near-run-25m: All candidates with nearest Run <= 25m
    # all-near-run-100m: All candidates with nearest Run > 25m and <= 100m
    run_nearest_map = {sid: (d, tid) for sid, d, tid in nd[("All", "Run")]}

    for suffix, predicate in [
        ("25m", lambda d: d <= 25),
        ("100m", lambda d: 25 < d <= 100),
    ]:
        out_path = VALIDATION / f"mallorca-fresh-all-near-run-{suffix}.geojson"
        features = []
        for c in layers["All"]:
            cid = c["candidate_id"]
            d, tid = run_nearest_map[cid]
            if predicate(d):
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
                    "properties": {
                        **c["properties"],
                        "nearest_run_candidate_id": tid,
                        "nearest_run_distance_m": round(d, 2),
                    },
                })
        collection = {"type": "FeatureCollection", "features": features}
        out_path.write_text(
            json.dumps(collection, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {out_path} ({len(features)} features)")

    # --- Console summary ---
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Ride: {len(layers['Ride'])}")
    print(f"Run: {len(layers['Run'])}")
    print(f"All: {len(layers['All'])}")
    print()

    for t in [25, 50, 100]:
        rep = count_within(all_union_nearest, t)
        print(f"All represented by Ride/Run @{t}m: {rep}")
        print(f"All-only @{t}m: {total_all - rep}")
        print()

    for t in [25, 50, 100]:
        # Run-All overlap: Run candidates within t of All
        ra = count_within(nd[("Run", "All")], t)
        print(f"Run-All overlap @{t}m: {ra}")
    print()

    print(f"Potential Run-All same-corridor review pairs (25-100m): {len(corridor_pairs)}")


if __name__ == "__main__":
    main()
