#!/usr/bin/env python3
"""Evaluate diagnostic suppression rules against MapRoulette labels.

Analysis only. Does not change detection, masking, GeoJSON, or production code.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from collections import Counter
from itertools import product


FOLLOW50 = "osm_follow_fraction_50m"
FOLLOW75 = "osm_follow_fraction_75m"
FOLLOW100 = "osm_follow_fraction_100m"
PARALLEL15 = "osm_parallel_fraction_15deg"
PARALLEL30 = "osm_parallel_fraction_30deg"
ELONGATION = "component_elongation"
IQR = "osm_distance_iqr_m"
P90 = "osm_distance_p90_m"
PMAX = "osm_distance_max_m"
PEAK_DIST = "nearest_osm_distance_m"
FERRY_DIST = "nearest_ferry_distance_m"
FERRY_ID = "nearest_ferry_id"
FERRY_NAME = "nearest_ferry_name"
CONSTR_DIST = "nearest_construction_distance_m"
CONSTR_ID = "nearest_construction_id"
CONSTR_NAME = "nearest_construction_name"
TAGS = "nearest_osm_tags"

STATUS_FIXED = "Fixed"
STATUS_NAI = "Not_An_Issue"
STATUS_TOO_HARD = "Too_Hard"

# Current best measured suppression rule (analysis-only visual export).
VISUAL_FOLLOW100_MIN = 0.70
VISUAL_PARALLEL15_MIN = 0.70
VISUAL_FILTER_REASON = "follow100>=0.70 and parallel15>=0.70"
GEOJSON_RECORD_SEP = "\x1e"

GEOJSON_ANALYSIS_FIELDS = [
    FOLLOW50,
    FOLLOW75,
    FOLLOW100,
    PARALLEL15,
    PARALLEL30,
    "osm_parallel_angle_median_deg",
    PEAK_DIST,
    "nearest_osm_highway",
    "nearest_osm_name",
    "nearest_osm_type",
    "nearest_osm_id",
    "component_pixels",
    ELONGATION,
    P90,
    FERRY_DIST,
    CONSTR_DIST,
]

RULE_RESULT_COLUMNS = [
    "rule_name",
    "removed_total_current",
    "removed_labeled",
    "removed_not_an_issue",
    "removed_fixed",
    "removed_too_hard",
    "removed_unlabeled",
    "not_an_issue_recall",
    "fixed_loss_rate",
    "precision_of_removed",
]


def parse_bool(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"true", "1", "yes"}


def parse_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_tags(value):
    if value is None:
        return {}
    text = str(value).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_status(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text.replace(" ", "_")


def fmt_int(value):
    return str(int(value))


def fmt_rate(num, den):
    if den == 0:
        return ""
    return f"{num / den:.6f}".rstrip("0").rstrip(".")


def load_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def ge(field, threshold):
    def pred(row):
        value = parse_float(row.get(field))
        return value is not None and value >= threshold

    return pred


def le(field, threshold):
    def pred(row):
        value = parse_float(row.get(field))
        return value is not None and value <= threshold

    return pred


def all_of(*preds):
    def pred(row):
        return all(p(row) for p in preds)

    return pred


def ferry_populated(row):
    ferry_id = (row.get(FERRY_ID) or "").strip()
    ferry_name = (row.get(FERRY_NAME) or "").strip()
    ferry_dist = parse_float(row.get(FERRY_DIST))
    return bool(ferry_id or ferry_name) and ferry_dist is not None


def ferry_le(threshold):
    def pred(row):
        if not ferry_populated(row):
            return False
        return parse_float(row.get(FERRY_DIST)) <= threshold

    return pred


def construction_populated(row):
    constr_id = (row.get(CONSTR_ID) or "").strip()
    constr_name = (row.get(CONSTR_NAME) or "").strip()
    constr_dist = parse_float(row.get(CONSTR_DIST))
    return bool(constr_id or constr_name) and constr_dist is not None


def construction_le(threshold):
    def pred(row):
        if not construction_populated(row):
            return False
        return parse_float(row.get(CONSTR_DIST)) <= threshold

    return pred


def is_tunnel(row):
    tags = parse_tags(row.get(TAGS))
    return str(tags.get("tunnel", "")).strip().lower() == "yes"


def tunnel_and(pred):
    def wrapped(row):
        return is_tunnel(row) and pred(row)

    return wrapped


def build_rules():
    rules = []

    def add(name, pred):
        rules.append((name, pred))

    for thresh in (0.50, 0.75, 0.80, 0.90, 0.95):
        add(f"follow50 >= {thresh:.2f}", ge(FOLLOW50, thresh))
        add(f"follow75 >= {thresh:.2f}", ge(FOLLOW75, thresh))
    for thresh in (0.80, 0.90, 0.95):
        add(f"follow100 >= {thresh:.2f}", ge(FOLLOW100, thresh))

    follow_fields = [
        ("follow50", FOLLOW50),
        ("follow75", FOLLOW75),
        ("follow100", FOLLOW100),
    ]
    follow_thresholds = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
    for follow_label, follow_field in follow_fields:
        for f_thresh in follow_thresholds:
            for p_thresh in (0.60, 0.70, 0.80, 0.90, 0.95):
                add(
                    f"{follow_label} >= {f_thresh:.2f} AND parallel30 >= {p_thresh:.2f}",
                    all_of(ge(follow_field, f_thresh), ge(PARALLEL30, p_thresh)),
                )
            for p_thresh in (0.50, 0.60, 0.70, 0.80, 0.90):
                add(
                    f"{follow_label} >= {f_thresh:.2f} AND parallel15 >= {p_thresh:.2f}",
                    all_of(ge(follow_field, f_thresh), ge(PARALLEL15, p_thresh)),
                )

    for thresh in (40, 45, 50, 60, 75, 100):
        add(f"osm_distance_p90_m <= {thresh}", le(P90, thresh))
    for thresh in (50, 60, 75):
        add(
            f"p90 <= {thresh} AND parallel30 >= 0.80",
            all_of(le(P90, thresh), ge(PARALLEL30, 0.80)),
        )

    for elong in (2, 3, 5):
        add(
            f"follow75 >= 0.80 AND parallel30 >= 0.80 AND elongation >= {elong}",
            all_of(ge(FOLLOW75, 0.80), ge(PARALLEL30, 0.80), ge(ELONGATION, elong)),
        )
    add(
        "follow75 >= 0.90 AND parallel30 >= 0.80 AND elongation >= 2",
        all_of(ge(FOLLOW75, 0.90), ge(PARALLEL30, 0.80), ge(ELONGATION, 2)),
    )

    for thresh in (50, 75, 100, 150, 200, 250, 500):
        add(f"ferry <= {thresh} (nearest_ferry_* populated)", ferry_le(thresh))
    for thresh in (100, 150, 200):
        add(
            f"ferry <= {thresh} AND follow100 >= 0.80",
            all_of(ferry_le(thresh), ge(FOLLOW100, 0.80)),
        )

    for thresh in (35, 50, 75, 100):
        add(
            f"construction <= {thresh} (nearest_construction_* populated)",
            construction_le(thresh),
        )

    for thresh in (50, 75, 100):
        add(f"tunnel=yes AND p90 <= {thresh}", tunnel_and(le(P90, thresh)))
    add("tunnel=yes AND follow75 >= 0.80", tunnel_and(ge(FOLLOW75, 0.80)))

    for thresh in (40, 45, 50, 60, 75):
        add(
            f"APPROX mask ~{thresh}m via component p90 <= {thresh} (NOT an exact detector rerun)",
            le(P90, thresh),
        )
        add(
            f"APPROX mask ~{thresh}m via component max <= {thresh} (NOT an exact detector rerun)",
            le(PMAX, thresh),
        )

    return rules


def evaluate_rule(name, pred, rows, nai_total, fixed_total):
    removed = [row for row in rows if pred(row)]
    status_counts = Counter(row["_mr_status"] for row in removed)
    nai = status_counts.get(STATUS_NAI, 0)
    fixed = status_counts.get(STATUS_FIXED, 0)
    too_hard = status_counts.get(STATUS_TOO_HARD, 0)
    unlabeled = status_counts.get("", 0)
    labeled = nai + fixed + too_hard
    return {
        "rule_name": name,
        "removed_total_current": len(removed),
        "removed_labeled": labeled,
        "removed_not_an_issue": nai,
        "removed_fixed": fixed,
        "removed_too_hard": too_hard,
        "removed_unlabeled": unlabeled,
        "not_an_issue_recall": fmt_rate(nai, nai_total),
        "fixed_loss_rate": fmt_rate(fixed, fixed_total),
        "precision_of_removed": fmt_rate(nai, nai + fixed),
        "_removed_ids_fixed": [
            row["candidate_id"] for row in removed if row["_mr_status"] == STATUS_FIXED
        ],
        "_recall": (nai / nai_total) if nai_total else None,
        "_loss": (fixed / fixed_total) if fixed_total else None,
        "_precision": (nai / (nai + fixed)) if (nai + fixed) else None,
    }


def pareto_frontier(results):
    frontier = []
    for candidate in results:
        dominated = False
        for other in results:
            if other is candidate:
                continue
            better_nai = other["removed_not_an_issue"] >= candidate["removed_not_an_issue"]
            better_fixed = other["removed_fixed"] <= candidate["removed_fixed"]
            strictly_better = (
                other["removed_not_an_issue"] > candidate["removed_not_an_issue"]
                or other["removed_fixed"] < candidate["removed_fixed"]
            )
            if better_nai and better_fixed and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)
    frontier.sort(
        key=lambda row: (
            -row["removed_not_an_issue"],
            row["removed_fixed"],
            -(row["_precision"] or 0),
            row["rule_name"],
        )
    )
    return frontier


def print_section(title):
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def print_kv(label, value, width=36):
    print(f"{label:<{width}} {value}")


def print_rule_table(results, limit=None):
    header = (
        f"{'rule_name':<78} {'tot':>4} {'lab':>4} {'NAI':>4} {'Fix':>4} "
        f"{'TH':>3} {'unl':>4} {'NAIrec':>8} {'FixLoss':>8} {'PrecRem':>8}"
    )
    print(header)
    print("-" * len(header))
    shown = results if limit is None else results[:limit]
    for row in shown:
        print(
            f"{row['rule_name']:<78} "
            f"{row['removed_total_current']:>4} "
            f"{row['removed_labeled']:>4} "
            f"{row['removed_not_an_issue']:>4} "
            f"{row['removed_fixed']:>4} "
            f"{row['removed_too_hard']:>3} "
            f"{row['removed_unlabeled']:>4} "
            f"{(row['not_an_issue_recall'] or '-'):>8} "
            f"{(row['fixed_loss_rate'] or '-'):>8} "
            f"{(row['precision_of_removed'] or '-'):>8}"
        )
    if limit is not None and len(results) > limit:
        print(f"... {len(results) - limit} more rules in rule-results.csv")


def write_csv(path, rows, fieldnames):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def meets_visual_rule(row):
    follow100 = parse_float(row.get(FOLLOW100))
    parallel15 = parse_float(row.get(PARALLEL15))
    return (
        follow100 is not None
        and follow100 >= VISUAL_FOLLOW100_MIN
        and parallel15 is not None
        and parallel15 >= VISUAL_PARALLEL15_MIN
    )


def visual_keep_reason(row):
    follow100 = parse_float(row.get(FOLLOW100))
    parallel15 = parse_float(row.get(PARALLEL15))
    follow_ok = follow100 is not None and follow100 >= VISUAL_FOLLOW100_MIN
    parallel_ok = parallel15 is not None and parallel15 >= VISUAL_PARALLEL15_MIN
    parts = []
    if not follow_ok:
        parts.append("follow100<0.70")
    if not parallel_ok:
        parts.append("parallel15<0.70")
    return "; ".join(parts) if parts else "kept"


def _feature_candidate_id(feature):
    props = feature.get("properties") or {}
    return str(props.get("candidate_id") or props.get("id") or "").strip()


def load_geojson_features(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    text = text.lstrip("\ufeff")
    chunks = text.split(GEOJSON_RECORD_SEP) if GEOJSON_RECORD_SEP in text else [text]
    features = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        obj = json.loads(chunk)
        obj_type = obj.get("type")
        if obj_type == "Feature":
            features.append(obj)
        elif obj_type == "FeatureCollection":
            features.extend(obj.get("features") or [])
    return features


def annotate_analysis_feature(feature, row, action, reason):
    exported = copy.deepcopy(feature)
    props = dict(exported.get("properties") or {})
    props["analysis_action"] = action
    props["mr_status"] = row.get("_mr_status", "")
    for field in GEOJSON_ANALYSIS_FIELDS:
        props[field] = row.get(field, "")
    props["analysis_reason"] = reason
    exported["properties"] = props
    exported["type"] = "Feature"
    return exported


def write_feature_collection(path, features):
    payload = {"type": "FeatureCollection", "features": features}
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def export_visual_geojson(source_path, accepted_rows, filter_path, keep_path):
    source_features = load_geojson_features(source_path)
    by_id = {}
    for feature in source_features:
        candidate_id = _feature_candidate_id(feature)
        if candidate_id and candidate_id not in by_id:
            by_id[candidate_id] = feature

    filter_features = []
    keep_features = []
    matched_ids = []
    missing_ids = []
    for row in accepted_rows:
        candidate_id = row.get("candidate_id", "")
        feature = by_id.get(candidate_id)
        if feature is None:
            missing_ids.append(candidate_id)
            continue
        matched_ids.append(candidate_id)
        if meets_visual_rule(row):
            filter_features.append(
                annotate_analysis_feature(
                    feature, row, "would_filter", VISUAL_FILTER_REASON
                )
            )
        else:
            keep_features.append(
                annotate_analysis_feature(
                    feature, row, "would_keep", visual_keep_reason(row)
                )
            )

    write_feature_collection(filter_path, filter_features)
    write_feature_collection(keep_path, keep_features)

    unmatched_source = [
        _feature_candidate_id(feature)
        for feature in source_features
        if _feature_candidate_id(feature) not in {row.get("candidate_id", "") for row in accepted_rows}
    ]
    return {
        "source_features": len(source_features),
        "matched_features": len(matched_ids),
        "would_filter": len(filter_features),
        "would_keep": len(keep_features),
        "missing_ids": missing_ids,
        "unmatched_source_ids": unmatched_source,
        "filter_status": Counter(
            feature["properties"].get("mr_status", "") for feature in filter_features
        ),
        "keep_status": Counter(
            feature["properties"].get("mr_status", "") for feature in keep_features
        ),
    }


REMAINING_CATEGORY_ORDER = [
    "ferry",
    "construction",
    "tunnel",
    "near_parallel_miss",
    "close_to_existing_osm",
    "weak_parallel_existing_osm",
    "far_from_existing_osm",
    "other",
]

REMAINING_CSV_EXTRA = [
    "mr_status",
    "mr_task_id",
    "remaining_category",
    "remaining_reason",
    "distance_above_35m",
    "parallel15_gap_to_070",
    "follow100_gap_to_070",
]

REMAINING_RULE_COLUMNS = [
    "rule_name",
    "removed_remaining_total",
    "removed_remaining_NAI",
    "removed_remaining_Fixed",
    "removed_remaining_Too_Hard",
    "total_suppressed_from_189",
    "remaining_geojson_features",
    "total_known_NAI_suppressed",
    "total_known_Fixed_suppressed",
]

REMAINING_GEOJSON_LAYERS = {
    "ferry": "analysis-remaining-ferry.geojson",
    "tunnel": "analysis-remaining-tunnel.geojson",
    "near_parallel_miss": "analysis-remaining-near-parallel.geojson",
    "other": "analysis-remaining-other.geojson",
}


def fmt_pct(num, den):
    if den == 0:
        return ""
    return f"{100.0 * num / den:.1f}%"


def fmt_gap(minuend, subtrahend, digits=4):
    left = parse_float(minuend) if not isinstance(minuend, (int, float)) else minuend
    right = parse_float(subtrahend)
    if left is None or right is None:
        return ""
    value = float(left) - float(right)
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-", "-0"} else "0"


def classify_remaining(row):
    ferry_dist = parse_float(row.get(FERRY_DIST))
    if ferry_dist is not None and ferry_dist <= 500:
        return "ferry"
    constr_dist = parse_float(row.get(CONSTR_DIST))
    if constr_dist is not None and constr_dist <= 100:
        return "construction"
    if is_tunnel(row):
        return "tunnel"
    follow100 = parse_float(row.get(FOLLOW100))
    parallel15 = parse_float(row.get(PARALLEL15))
    parallel30 = parse_float(row.get(PARALLEL30))
    near = (
        not meets_visual_rule(row)
        and follow100 is not None
        and parallel15 is not None
        and (
            (follow100 >= 0.70 and parallel15 >= 0.50)
            or (parallel15 >= 0.70 and follow100 >= 0.50)
        )
    )
    if near:
        return "near_parallel_miss"
    peak = parse_float(row.get(PEAK_DIST))
    if peak is not None and peak <= 50:
        return "close_to_existing_osm"
    if (
        follow100 is not None
        and follow100 >= 0.70
        and parallel30 is not None
        and parallel30 >= 0.70
        and (parallel15 is None or parallel15 < 0.70)
    ):
        return "weak_parallel_existing_osm"
    if peak is not None and peak > 100:
        return "far_from_existing_osm"
    return "other"


def remaining_reason(row, category):
    follow100 = row.get(FOLLOW100) or ""
    parallel15 = row.get(PARALLEL15) or ""
    parallel30 = row.get(PARALLEL30) or ""
    peak = row.get(PEAK_DIST) or ""
    if category == "ferry":
        return f"nearest_ferry_distance_m={row.get(FERRY_DIST, '')}<=500"
    if category == "construction":
        return f"nearest_construction_distance_m={row.get(CONSTR_DIST, '')}<=100"
    if category == "tunnel":
        return "nearest_osm_tags tunnel=yes"
    if category == "near_parallel_miss":
        return (
            f"not (follow100>=0.70 AND parallel15>=0.70); "
            f"follow100={follow100} parallel15={parallel15}"
        )
    if category == "close_to_existing_osm":
        return f"nearest_osm_distance_m={peak}<=50"
    if category == "weak_parallel_existing_osm":
        return (
            f"follow100={follow100}>=0.70 AND parallel30={parallel30}>=0.70 "
            f"AND parallel15={parallel15}<0.70"
        )
    if category == "far_from_existing_osm":
        return f"nearest_osm_distance_m={peak}>100"
    keep = visual_keep_reason(row)
    return f"uncategorized; {keep}; peak={peak}"


def annotate_remaining_row(row):
    category = classify_remaining(row)
    out = dict(row)
    out["remaining_category"] = category
    out["remaining_reason"] = remaining_reason(row, category)
    out["distance_above_35m"] = fmt_gap(parse_float(row.get(PEAK_DIST)), 35, 3)
    # fmt_gap(minuend, subtrahend) = minuend - subtrahend; swap via 0.70 - value
    parallel15 = parse_float(row.get(PARALLEL15))
    follow100 = parse_float(row.get(FOLLOW100))
    out["parallel15_gap_to_070"] = (
        fmt_gap(0.70, row.get(PARALLEL15), 4) if parallel15 is not None else ""
    )
    out["follow100_gap_to_070"] = (
        fmt_gap(0.70, row.get(FOLLOW100), 4) if follow100 is not None else ""
    )
    out["mr_status"] = row.get("_mr_status", "")
    out["mr_task_id"] = row.get("_mr_task_id", "")
    return out


def build_remaining_rules():
    rules = []

    def add(name, pred):
        rules.append((name, pred))

    for thresh in (75, 100, 150, 200, 250, 500):
        add(f"ferry <= {thresh}", le(FERRY_DIST, thresh))
    for thresh in (50, 75, 100):
        add(f"tunnel=yes AND p90 <= {thresh}", tunnel_and(le(P90, thresh)))
    add("tunnel=yes AND follow75 >= 0.80", tunnel_and(ge(FOLLOW75, 0.80)))
    add("tunnel=yes regardless of distance", is_tunnel)
    for thresh in (35, 50, 75, 100):
        add(f"construction <= {thresh}", le(CONSTR_DIST, thresh))
    add(
        "follow100 >= 0.70 AND parallel15 >= 0.65",
        all_of(ge(FOLLOW100, 0.70), ge(PARALLEL15, 0.65)),
    )
    add(
        "follow100 >= 0.70 AND parallel15 >= 0.60",
        all_of(ge(FOLLOW100, 0.70), ge(PARALLEL15, 0.60)),
    )
    add(
        "follow100 >= 0.80 AND parallel15 >= 0.65",
        all_of(ge(FOLLOW100, 0.80), ge(PARALLEL15, 0.65)),
    )
    add(
        "follow100 >= 0.90 AND parallel15 >= 0.65",
        all_of(ge(FOLLOW100, 0.90), ge(PARALLEL15, 0.65)),
    )
    add(
        "follow100 >= 0.80 AND parallel30 >= 0.90",
        all_of(ge(FOLLOW100, 0.80), ge(PARALLEL30, 0.90)),
    )
    add(
        "follow100 >= 0.90 AND parallel30 >= 0.90",
        all_of(ge(FOLLOW100, 0.90), ge(PARALLEL30, 0.90)),
    )
    add(
        "follow75 >= 0.90 AND parallel30 >= 0.90",
        all_of(ge(FOLLOW75, 0.90), ge(PARALLEL30, 0.90)),
    )
    add("p90 <= 40", le(P90, 40))
    add("nearest_osm_distance_m <= 40", le(PEAK_DIST, 40))
    add("nearest_osm_distance_m <= 45", le(PEAK_DIST, 45))
    return rules


def evaluate_remaining_rule(name, pred, remaining_rows, accepted_count, baseline):
    removed = [row for row in remaining_rows if pred(row)]
    status_counts = Counter(row["_mr_status"] for row in removed)
    nai = status_counts.get(STATUS_NAI, 0)
    fixed = status_counts.get(STATUS_FIXED, 0)
    too_hard = status_counts.get(STATUS_TOO_HARD, 0)
    total = len(removed)
    return {
        "rule_name": name,
        "removed_remaining_total": total,
        "removed_remaining_NAI": nai,
        "removed_remaining_Fixed": fixed,
        "removed_remaining_Too_Hard": too_hard,
        "total_suppressed_from_189": baseline["suppressed"] + total,
        "remaining_geojson_features": baseline["remain"] - total,
        "total_known_NAI_suppressed": baseline["nai_suppressed"] + nai,
        "total_known_Fixed_suppressed": baseline["fixed_suppressed"] + fixed,
        "_removed_ids_fixed": [
            row["candidate_id"] for row in removed if row["_mr_status"] == STATUS_FIXED
        ],
        "_removed_ids_th": [
            row["candidate_id"] for row in removed if row["_mr_status"] == STATUS_TOO_HARD
        ],
        "_nai": nai,
        "_fixed": fixed,
        "_th": too_hard,
        "_total": total,
        "_accepted_count": accepted_count,
    }


def remaining_pareto(results):
    frontier = []
    for candidate in results:
        dominated = False
        for other in results:
            if other is candidate:
                continue
            better_nai = other["removed_remaining_NAI"] >= candidate["removed_remaining_NAI"]
            better_fixed = other["removed_remaining_Fixed"] <= candidate["removed_remaining_Fixed"]
            strictly_better = (
                other["removed_remaining_NAI"] > candidate["removed_remaining_NAI"]
                or other["removed_remaining_Fixed"] < candidate["removed_remaining_Fixed"]
            )
            if better_nai and better_fixed and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)
    frontier.sort(
        key=lambda row: (
            -row["removed_remaining_NAI"],
            row["removed_remaining_Fixed"],
            row["removed_remaining_Too_Hard"],
            row["rule_name"],
        )
    )
    return frontier


def print_remaining_rule_table(results):
    header = (
        f"{'rule_name':<52} {'remTot':>6} {'remNAI':>6} {'remFix':>6} {'remTH':>5} "
        f"{'supAll':>6} {'geoLeft':>7} {'NAIall':>6} {'FixAll':>6}"
    )
    print(header)
    print("-" * len(header))
    for row in results:
        print(
            f"{row['rule_name']:<52} "
            f"{row['removed_remaining_total']:>6} "
            f"{row['removed_remaining_NAI']:>6} "
            f"{row['removed_remaining_Fixed']:>6} "
            f"{row['removed_remaining_Too_Hard']:>5} "
            f"{row['total_suppressed_from_189']:>6} "
            f"{row['remaining_geojson_features']:>7} "
            f"{row['total_known_NAI_suppressed']:>6} "
            f"{row['total_known_Fixed_suppressed']:>6}"
        )


def print_remaining_detail(title, rows):
    print()
    print(title)
    if not rows:
        print("  (none)")
        return
    header = (
        f"{'candidate_id':<28} {'status':<14} {'category':<26} "
        f"{'peak':>8} {'hwy':<12} {'route':<8} {'name':<22} "
        f"{'ferry':>8} {'constr':>8} "
        f"{'f50':>6} {'f75':>6} {'f100':>6} {'p15':>6} {'p30':>6} "
        f"{'ang':>6} {'elong':>7} {'p90':>8} {'px':>5} {'smax':>5}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        name = (row.get("nearest_osm_name") or "")[:22]
        print(
            f"{row.get('candidate_id', ''):<28} "
            f"{row.get('_mr_status', ''):<14} "
            f"{row.get('remaining_category', ''):<26} "
            f"{(row.get(PEAK_DIST) or ''):>8} "
            f"{(row.get('nearest_osm_highway') or ''):<12} "
            f"{(row.get('nearest_osm_route') or ''):<8} "
            f"{name:<22} "
            f"{(row.get(FERRY_DIST) or ''):>8} "
            f"{(row.get(CONSTR_DIST) or ''):>8} "
            f"{(row.get(FOLLOW50) or ''):>6} "
            f"{(row.get(FOLLOW75) or ''):>6} "
            f"{(row.get(FOLLOW100) or ''):>6} "
            f"{(row.get(PARALLEL15) or ''):>6} "
            f"{(row.get(PARALLEL30) or ''):>6} "
            f"{(row.get('osm_parallel_angle_median_deg') or ''):>6} "
            f"{(row.get(ELONGATION) or ''):>7} "
            f"{(row.get(P90) or ''):>8} "
            f"{(row.get('component_pixels') or ''):>5} "
            f"{(row.get('strava_max') or ''):>5}"
        )


def remaining_measured_summary(remaining, by_cat, rule_results, baseline):
    nai_remain = sum(1 for row in remaining if row["_mr_status"] == STATUS_NAI)
    lines = []
    ranked = sorted(
        (
            (
                cat,
                sum(1 for row in rows if row["_mr_status"] == STATUS_NAI),
                len(rows),
            )
            for cat, rows in by_cat.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )
    parts = [
        f"{cat} {nai}/{tot}"
        for cat, nai, tot in ranked
        if nai > 0
    ]
    lines.append(
        "Hauptursachen der verbleibenden Not_An_Issue (Kategorie NAI/gesamt, Precedenz): "
        + ("; ".join(parts) if parts else "keine")
        + "."
    )
    ferry_rules = [
        row for row in rule_results
        if row["rule_name"].startswith("ferry <=")
        and row["removed_remaining_Fixed"] == 0
        and row["removed_remaining_Too_Hard"] == 0
        and row["removed_remaining_NAI"] > 0
    ]
    if ferry_rules:
        best_ferry = max(ferry_rules, key=lambda row: (row["removed_remaining_NAI"], -row["removed_remaining_total"]))
        lines.append(
            f"Fähre: naheliegende Low-Risk-Erweiterung. {best_ferry['rule_name']} "
            f"entfernt inkrementell {best_ferry['removed_remaining_NAI']} NAI, "
            f"0 Fixed, 0 Too_Hard auf den {baseline['remain']} Remaining "
            f"(kumulativ {best_ferry['total_known_NAI_suppressed']} bekannte NAI, "
            f"{best_ferry['remaining_geojson_features']} GeoJSON übrig)."
        )
    else:
        lines.append("Fähre: keine gemessene Fährregel entfernt Remaining-NAI bei 0 Fixed/Too_Hard.")
    tunnel_rules = [
        row for row in rule_results if row["rule_name"].startswith("tunnel=")
    ]
    tunnel_safe = [
        row for row in tunnel_rules
        if row["removed_remaining_Fixed"] == 0 and row["removed_remaining_NAI"] > 0
    ]
    if tunnel_safe:
        best_tunnel = max(tunnel_safe, key=lambda row: row["removed_remaining_NAI"])
        lines.append(
            f"Tunnel: begrenzt nützlich. {best_tunnel['rule_name']} "
            f"entfernt {best_tunnel['removed_remaining_NAI']} Remaining-NAI, "
            f"{best_tunnel['removed_remaining_Fixed']} Fixed, "
            f"{best_tunnel['removed_remaining_Too_Hard']} Too_Hard."
        )
    else:
        lines.append(
            f"Tunnel: auf den {baseline['remain']} Remaining kein nennenswerter NAI-Gewinn ohne Fixed-Treffer."
        )
    relax = next(
        (row for row in rule_results if row["rule_name"] == "follow100 >= 0.70 AND parallel15 >= 0.65"),
        None,
    )
    if relax:
        hit = "ja" if relax["removed_remaining_Fixed"] else "nein"
        ids = ", ".join(relax["_removed_ids_fixed"]) if relax["_removed_ids_fixed"] else "keine"
        lines.append(
            f"parallel15 0.70 -> 0.65 bei follow100>=0.70: Fixed-Kontrolle getroffen? {hit} "
            f"(Fixed entfernt={relax['removed_remaining_Fixed']}: {ids}; "
            f"NAI={relax['removed_remaining_NAI']}, Too_Hard={relax['removed_remaining_Too_Hard']})."
        )
    safest = [
        row for row in rule_results
        if row["removed_remaining_Fixed"] == 0
        and row["removed_remaining_Too_Hard"] == 0
        and row["removed_remaining_NAI"] > 0
    ]
    safest.sort(key=lambda row: (-row["removed_remaining_NAI"], row["rule_name"]))
    if safest:
        top = safest[0]
        lines.append(
            f"Sicher zusätzlich entfernbar: {top['removed_remaining_NAI']} NAI "
            f"({top['rule_name']}) ohne Fixed/Too_Hard; "
            f"kumulativ {top['total_suppressed_from_189']} von {baseline['accepted']} unterdrückt, "
            f"{top['remaining_geojson_features']} Tasks übrig."
        )
    else:
        left = baseline["remain"]
        lines.append(
            f"Keine sichere inkrementelle Regel (0 Fixed und 0 Too_Hard) gefunden. "
            f"Es blieben {left} von {baseline['accepted']} Tasks."
        )
    lines.append(
        f"Remaining-NAI in dieser Analyse: {nai_remain}. "
        "Kategorien sind deskriptiv, keine Produktionsregeln."
    )
    return lines


def export_remaining_category_geojson(source_path, remaining_annotated):
    source_features = load_geojson_features(source_path)
    by_id = {}
    for feature in source_features:
        candidate_id = _feature_candidate_id(feature)
        if candidate_id and candidate_id not in by_id:
            by_id[candidate_id] = feature
    written = {}
    for category, path in REMAINING_GEOJSON_LAYERS.items():
        features = []
        for row in remaining_annotated:
            if row.get("remaining_category") != category:
                continue
            feature = by_id.get(row.get("candidate_id", ""))
            if feature is None:
                continue
            exported = annotate_analysis_feature(
                feature, row, "would_keep", row.get("remaining_reason", "")
            )
            props = exported["properties"]
            props["remaining_category"] = row.get("remaining_category", "")
            props["remaining_reason"] = row.get("remaining_reason", "")
            props["distance_above_35m"] = row.get("distance_above_35m", "")
            props["parallel15_gap_to_070"] = row.get("parallel15_gap_to_070", "")
            props["follow100_gap_to_070"] = row.get("follow100_gap_to_070", "")
            features.append(exported)
        write_feature_collection(path, features)
        written[category] = (path, len(features))
    return written


def run_remaining_analysis(accepted, diagnostics, args):
    remaining = [row for row in accepted if not meets_visual_rule(row)]
    filtered = [row for row in accepted if meets_visual_rule(row)]
    annotated = [annotate_remaining_row(row) for row in remaining]
    for src, dst in zip(remaining, annotated):
        src["remaining_category"] = dst["remaining_category"]
        src["remaining_reason"] = dst["remaining_reason"]

    by_cat = {cat: [] for cat in REMAINING_CATEGORY_ORDER}
    for row in remaining:
        by_cat[row["remaining_category"]].append(row)

    remain_total = len(remaining)
    remain_nai = sum(1 for row in remaining if row["_mr_status"] == STATUS_NAI)
    remain_fixed = sum(1 for row in remaining if row["_mr_status"] == STATUS_FIXED)
    remain_th = sum(1 for row in remaining if row["_mr_status"] == STATUS_TOO_HARD)
    baseline = {
        "accepted": len(accepted),
        "suppressed": len(filtered),
        "remain": remain_total,
        "nai_suppressed": sum(1 for row in filtered if row["_mr_status"] == STATUS_NAI),
        "fixed_suppressed": sum(1 for row in filtered if row["_mr_status"] == STATUS_FIXED),
    }

    print_section("Remaining candidates after production-equivalent rule")
    print_kv("Production rule", VISUAL_FILTER_REASON)
    print_kv("Accepted before rule", len(accepted))
    print_kv("Would filter / suppressed", f"{baseline['suppressed']} (NAI {baseline['nai_suppressed']}, Fixed {baseline['fixed_suppressed']})")
    print_kv("Would keep / remaining", f"{remain_total} (NAI {remain_nai}, Fixed {remain_fixed}, Too_Hard {remain_th})")
    print("Categories are descriptive only; they are not production suppression rules.")

    print_section("Remaining category summary")
    header = (
        f"{'category':<28} {'tot':>4} {'NAI':>4} {'Fix':>4} {'TH':>3} "
        f"{'% remain':>8} {'% NAI':>8}"
    )
    print(header)
    print("-" * len(header))
    for cat in REMAINING_CATEGORY_ORDER:
        rows = by_cat[cat]
        nai = sum(1 for row in rows if row["_mr_status"] == STATUS_NAI)
        fixed = sum(1 for row in rows if row["_mr_status"] == STATUS_FIXED)
        too_hard = sum(1 for row in rows if row["_mr_status"] == STATUS_TOO_HARD)
        print(
            f"{cat:<28} {len(rows):>4} {nai:>4} {fixed:>4} {too_hard:>3} "
            f"{fmt_pct(len(rows), remain_total):>8} {fmt_pct(nai, remain_nai):>8}"
        )

    print_remaining_detail("A. All Fixed candidates among remaining", by_status(remaining, STATUS_FIXED))
    print_remaining_detail("B. Too_Hard candidate among remaining", by_status(remaining, STATUS_TOO_HARD))
    print_remaining_detail("C. All ferry remaining candidates", by_cat["ferry"])
    print_remaining_detail("D. All construction remaining candidates", by_cat["construction"])
    print_remaining_detail("E. All tunnel remaining candidates", by_cat["tunnel"])
    print_remaining_detail("F. All near_parallel_miss remaining candidates", by_cat["near_parallel_miss"])
    other_nai = [row for row in by_cat["other"] if row["_mr_status"] == STATUS_NAI]
    print_remaining_detail("G. Remaining Not_An_Issue still in other", other_nai)

    remaining_rules = build_remaining_rules()
    remaining_results = [
        evaluate_remaining_rule(name, pred, remaining, len(accepted), baseline)
        for name, pred in remaining_rules
    ]
    print_section(f"Incremental rules on the {remain_total} remaining only")
    print(
        "Counts are incremental on would-keep only. "
        "supAll / geoLeft / NAIall / FixAll add the current suppressed baseline "
        "without double-counting."
    )
    print()
    print_remaining_rule_table(remaining_results)

    safest_inc = [
        row for row in remaining_results
        if row["removed_remaining_Fixed"] == 0
        and row["removed_remaining_Too_Hard"] == 0
        and row["removed_remaining_NAI"] > 0
    ]
    safest_inc.sort(key=lambda row: (-row["removed_remaining_NAI"], row["rule_name"]))
    zero_fixed = [
        row for row in remaining_results
        if row["removed_remaining_Fixed"] == 0 and row["removed_remaining_NAI"] > 0
    ]
    zero_fixed.sort(
        key=lambda row: (
            -row["removed_remaining_NAI"],
            row["removed_remaining_Too_Hard"],
            row["rule_name"],
        )
    )
    frontier = remaining_pareto(remaining_results)

    print_section("Safest incremental rules (0 Fixed and 0 Too_Hard)")
    if safest_inc:
        print_remaining_rule_table(safest_inc)
    else:
        print("  (none)")

    print_section("Incremental rules with 0 Fixed (Too_Hard allowed)")
    if zero_fixed:
        print_remaining_rule_table(zero_fixed)
    else:
        print("  (none)")

    print_section("Incremental Pareto frontier (max remaining NAI, min remaining Fixed)")
    print_remaining_rule_table(frontier)

    internal = {"_mr_status", "_mr_task_id", "_mr_mapper", "remaining_category", "remaining_reason"}
    remaining_fields = [
        key for key in (diagnostics[0].keys() if diagnostics else []) if key not in internal
    ]
    for field in REMAINING_CSV_EXTRA:
        if field not in remaining_fields:
            remaining_fields.append(field)
    remaining_csv_rows = []
    for row, extra in zip(remaining, annotated):
        out = {key: value for key, value in row.items() if key not in internal}
        for field in REMAINING_CSV_EXTRA:
            out[field] = extra.get(field, "")
        remaining_csv_rows.append(out)
    write_csv(args.remaining, remaining_csv_rows, remaining_fields)
    write_csv(
        args.remaining_rules,
        [{key: row[key] for key in REMAINING_RULE_COLUMNS} for row in remaining_results],
        REMAINING_RULE_COLUMNS,
    )

    print_section("Remaining analysis output files")
    print_kv("analysis-remaining.csv", args.remaining)
    print_kv("remaining-rule-results.csv", args.remaining_rules)
    if args.geojson:
        written = export_remaining_category_geojson(args.geojson, annotated)
        for category, (path, count) in written.items():
            print_kv(path, f"{count} features ({category})")

    print_section("Remaining measured-data summary")
    for line in remaining_measured_summary(remaining, by_cat, remaining_results, baseline):
        print(line)

    return remaining_results


def by_status(rows, status):
    return [row for row in rows if row.get("_mr_status") == status]


def recommend(safest, conservative, aggressive, nai_total, fixed_total):
    lines = []
    if nai_total == 0:
        lines.append(
            "Keine gematchten Not_An_Issue-Labels; keine Suppressions-Empfehlung."
        )
        return lines
    if safest:
        top = safest[0]
        lines.append(
            f"Sicherste gemessene Regel (0 Fixed-Verlust): {top['rule_name']} "
            f"entfernt {top['removed_not_an_issue']}/{nai_total} Not_An_Issue "
            f"(Recall {top['not_an_issue_recall'] or 'n/a'})."
        )
    else:
        lines.append("Keine getestete Regel entfernt Not_An_Issue bei 0 Fixed-Verlust.")
    if conservative:
        top = conservative[0]
        lines.append(
            f"Konservativ (<=2% Fixed-Verlust): {top['rule_name']} "
            f"entfernt {top['removed_not_an_issue']} NAI, "
            f"{top['removed_fixed']} Fixed "
            f"(Loss {top['fixed_loss_rate'] or 'n/a'}, "
            f"Precision {top['precision_of_removed'] or 'n/a'})."
        )
    if aggressive:
        top = aggressive[0]
        lines.append(
            f"Aggressiv (<=5% Fixed-Verlust): {top['rule_name']} "
            f"entfernt {top['removed_not_an_issue']} NAI, "
            f"{top['removed_fixed']} Fixed "
            f"(Loss {top['fixed_loss_rate'] or 'n/a'}, "
            f"Precision {top['precision_of_removed'] or 'n/a'})."
        )
    lines.append(
        "Historische Fixed-Tasks, die in den aktuellen Diagnostics fehlen, "
        "wurden NICHT als durch diese Regeln gefiltert gewertet."
    )
    lines.append("Keine Produktionsregel wurde geändert.")
    return lines


V2_CATEGORY_ORDER = [
    "construction",
    "tunnel",
    "close_to_existing_osm",
    "near_parallel_miss",
    "weak_parallel_existing_osm",
    "far_from_existing_osm",
    "other",
]

V2_RULE_COLUMNS = [
    "rule_name",
    "family",
    "complexity",
    "removed_remaining_total",
    "removed_remaining_NAI",
    "removed_remaining_Fixed",
    "removed_remaining_Too_Hard",
    "removed_unmatched",
    "removed_fixed_ids",
    "removed_too_hard_ids",
]

V2_CSV_EXTRA = [
    "mr_status",
    "mr_task_id",
    "remaining_category",
    "remaining_reason",
]

FIXED_CONTROL_TRACK = "14/8305/6233/875/927"
FIXED_CONTROL_RESIDENTIAL = "14/8308/6230/389/806"
TOO_HARD_CONTROL = "14/8306/6225/102/938"

STRAVA_DIST_FIELDS = [
    "component_pixels",
    "geometry_length_m",
    "geometry_area_m2",
    ELONGATION,
    "strava_max",
    "strava_mean",
    "strava_p75",
    "strava_p90",
    "strava_p95",
]


def is_tunnel_any(row):
    tags = parse_tags(row.get(TAGS))
    return "tunnel" in tags and str(tags.get("tunnel", "")).strip() != ""


def tag_equals(row, key, value):
    tags = parse_tags(row.get(TAGS))
    return str(tags.get(key, "")).strip().lower() == str(value).strip().lower()


def tag_present(row, key):
    tags = parse_tags(row.get(TAGS))
    return key in tags and str(tags.get(key, "")).strip() != ""


def classify_remaining_v2(row):
    constr_dist = parse_float(row.get(CONSTR_DIST))
    if constr_dist is not None and constr_dist <= 100:
        return "construction"
    if is_tunnel(row):
        return "tunnel"
    peak = parse_float(row.get(PEAK_DIST))
    if peak is not None and peak <= 50:
        return "close_to_existing_osm"
    follow100 = parse_float(row.get(FOLLOW100))
    parallel15 = parse_float(row.get(PARALLEL15))
    parallel30 = parse_float(row.get(PARALLEL30))
    if (
        follow100 is not None
        and parallel15 is not None
        and (
            (follow100 >= 0.70 and parallel15 >= 0.50)
            or (parallel15 >= 0.70 and follow100 >= 0.50)
        )
    ):
        return "near_parallel_miss"
    if (
        follow100 is not None
        and follow100 >= 0.70
        and parallel30 is not None
        and parallel30 >= 0.70
        and (parallel15 is None or parallel15 < 0.70)
    ):
        return "weak_parallel_existing_osm"
    if peak is not None and peak > 100:
        return "far_from_existing_osm"
    return "other"


def remaining_reason_v2(row, category):
    follow100 = row.get(FOLLOW100) or ""
    parallel15 = row.get(PARALLEL15) or ""
    parallel30 = row.get(PARALLEL30) or ""
    peak = row.get(PEAK_DIST) or ""
    if category == "construction":
        return f"nearest_construction_distance_m={row.get(CONSTR_DIST, '')}<=100"
    if category == "tunnel":
        return "nearest_osm_tags tunnel=yes"
    if category == "close_to_existing_osm":
        return f"nearest_osm_distance_m={peak}<=50"
    if category == "near_parallel_miss":
        return f"follow100={follow100} parallel15={parallel15} (near miss of 0.70/0.70)"
    if category == "weak_parallel_existing_osm":
        return (
            f"follow100={follow100}>=0.70 AND parallel30={parallel30}>=0.70 "
            f"AND parallel15={parallel15}<0.70"
        )
    if category == "far_from_existing_osm":
        return f"nearest_osm_distance_m={peak}>100"
    return f"uncategorized; peak={peak}; follow100={follow100}; parallel15={parallel15}"


def status_bucket(row):
    status = row.get("_mr_status") or ""
    if status == STATUS_NAI:
        return "nai"
    if status == STATUS_FIXED:
        return "fixed"
    if status == STATUS_TOO_HARD:
        return "too_hard"
    if status:
        return "other"
    return "unmatched"


def percentile(values, p):
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt_num(value, digits=3):
    if value is None:
        return ""
    text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-", "-0"} else "0"


def describe_values(values):
    nums = [v for v in values if v is not None]
    if not nums:
        return {"n": 0, "min": "", "p25": "", "median": "", "mean": "", "p75": "", "max": ""}
    return {
        "n": len(nums),
        "min": fmt_num(min(nums)),
        "p25": fmt_num(percentile(nums, 25)),
        "median": fmt_num(percentile(nums, 50)),
        "mean": fmt_num(sum(nums) / len(nums)),
        "p75": fmt_num(percentile(nums, 75)),
        "max": fmt_num(max(nums)),
    }


def rule_complexity(name):
    return name.count(" AND ") + 1


def evaluate_remaining_v2_rule(name, pred, family, remaining_rows, complexity=None):
    removed = [row for row in remaining_rows if pred(row)]
    buckets = Counter(status_bucket(row) for row in removed)
    fixed_ids = [row["candidate_id"] for row in removed if row.get("_mr_status") == STATUS_FIXED]
    th_ids = [row["candidate_id"] for row in removed if row.get("_mr_status") == STATUS_TOO_HARD]
    ids = [row["candidate_id"] for row in removed]
    return {
        "rule_name": name,
        "family": family,
        "complexity": complexity if complexity is not None else rule_complexity(name),
        "removed_remaining_total": len(removed),
        "removed_remaining_NAI": buckets.get("nai", 0),
        "removed_remaining_Fixed": buckets.get("fixed", 0),
        "removed_remaining_Too_Hard": buckets.get("too_hard", 0),
        "removed_unmatched": buckets.get("unmatched", 0),
        "removed_fixed_ids": ";".join(fixed_ids),
        "removed_too_hard_ids": ";".join(th_ids),
        "_removed_ids": ids,
        "_fixed_ids": fixed_ids,
        "_th_ids": th_ids,
    }


def dedupe_equivalent_rules(results):
    best = {}
    for row in results:
        key = frozenset(row["_removed_ids"])
        prev = best.get(key)
        score = (row["complexity"], len(row["rule_name"]), row["rule_name"])
        if prev is None:
            best[key] = row
            continue
        prev_score = (prev["complexity"], len(prev["rule_name"]), prev["rule_name"])
        if score < prev_score:
            best[key] = row
    return list(best.values())


def print_v2_rule_table(results, unsafe=False):
    header = (
        f"{'rule_name':<72} {'tot':>4} {'NAI':>4} {'Fix':>4} {'TH':>3} "
        f"{'unm':>3} {'cx':>3}"
    )
    if unsafe:
        header += "  note"
    print(header)
    print("-" * len(header))
    for row in results:
        line = (
            f"{row['rule_name']:<72} "
            f"{row['removed_remaining_total']:>4} "
            f"{row['removed_remaining_NAI']:>4} "
            f"{row['removed_remaining_Fixed']:>4} "
            f"{row['removed_remaining_Too_Hard']:>3} "
            f"{row['removed_unmatched']:>3} "
            f"{row['complexity']:>3}"
        )
        if unsafe:
            line += "  UNSAFE FOR PRODUCTION"
        print(line)


def print_control_hits(title, results):
    print()
    print(title)
    header = f"{'rule_name':<56} {'NAI':>4} {'Fix':>4} {'TH':>3} {'Fixed IDs / Too_Hard IDs'}"
    print(header)
    print("-" * len(header))
    for row in results:
        hits = row["removed_fixed_ids"] or "-"
        if row["removed_too_hard_ids"]:
            hits = f"{hits} | TH={row['removed_too_hard_ids']}"
        print(
            f"{row['rule_name']:<56} "
            f"{row['removed_remaining_NAI']:>4} "
            f"{row['removed_remaining_Fixed']:>4} "
            f"{row['removed_remaining_Too_Hard']:>3} "
            f"{hits}"
        )


def print_candidate_metrics(title, rows):
    print()
    print(title)
    if not rows:
        print("  (none)")
        return
    header = (
        f"{'candidate_id':<28} {'status':<14} {'cat':<24} "
        f"{'peak':>8} {'hwy/type':<14} {'name':<22} "
        f"{'p90':>8} {'iqr':>6} {'f75':>6} {'f100':>6} "
        f"{'p15':>6} {'p30':>6} {'elong':>7} {'px':>5} {'len':>7}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        hwy = (row.get("nearest_osm_highway") or row.get("nearest_osm_route") or row.get("nearest_osm_type") or "")[:14]
        name = (row.get("nearest_osm_name") or "")[:22]
        print(
            f"{row.get('candidate_id', ''):<28} "
            f"{(row.get('_mr_status') or '(unmatched)'):<14} "
            f"{(row.get('remaining_category') or ''):<24} "
            f"{(row.get(PEAK_DIST) or ''):>8} "
            f"{hwy:<14} "
            f"{name:<22} "
            f"{(row.get(P90) or ''):>8} "
            f"{(row.get(IQR) or ''):>6} "
            f"{(row.get(FOLLOW75) or ''):>6} "
            f"{(row.get(FOLLOW100) or ''):>6} "
            f"{(row.get(PARALLEL15) or ''):>6} "
            f"{(row.get(PARALLEL30) or ''):>6} "
            f"{(row.get(ELONGATION) or ''):>7} "
            f"{(row.get('component_pixels') or ''):>5} "
            f"{(row.get('geometry_length_m') or ''):>7}"
        )


def build_remaining_v2_rules():
    rules = []

    def add(name, pred, family):
        rules.append((name, pred, family, rule_complexity(name)))

    for thresh in (35, 50, 75, 100, 150):
        add(f"construction <= {thresh}", le(CONSTR_DIST, thresh), "construction")
    add("nearest tags highway=construction", lambda row: tag_equals(row, "highway", "construction"), "construction")
    add("nearest tags construction=*", lambda row: tag_present(row, "construction"), "construction")
    add("nearest tags construction=yes", lambda row: tag_equals(row, "construction", "yes"), "construction")

    add("tunnel=yes", is_tunnel, "tunnel")
    add("tunnel=*", is_tunnel_any, "tunnel")
    for thresh in (50, 75, 100, 125, 150):
        add(f"tunnel=* AND p90 <= {thresh}", all_of(is_tunnel_any, le(P90, thresh)), "tunnel")
    for thresh in (0.50, 0.70, 0.90):
        add(
            f"tunnel=* AND follow100 >= {thresh:.2f}",
            all_of(is_tunnel_any, ge(FOLLOW100, thresh)),
            "tunnel",
        )
        add(
            f"tunnel=* AND follow75 >= {thresh:.2f}",
            all_of(is_tunnel_any, ge(FOLLOW75, thresh)),
            "tunnel",
        )

    for thresh in (36, 37, 38, 39, 40, 42, 45, 50, 60):
        add(f"nearest_osm_distance_m <= {thresh}", le(PEAK_DIST, thresh), "close_peak")
    for thresh in (36, 38, 40, 42, 45, 50, 60):
        add(f"osm_distance_p90_m <= {thresh}", le(P90, thresh), "close_p90")

    follow100s = (0.70, 0.80, 0.90, 0.95)
    parallel15s = (0.40, 0.50, 0.55, 0.60, 0.65)
    parallel30s = (0.70, 0.80, 0.90, 0.95)
    iqrs = (2, 3, 5, 7.5, 10, 15)
    elongs = (2, 3, 5, 10, 20)
    peaks = (40, 45, 50, 60)

    for f_thresh in follow100s:
        add(f"follow100 >= {f_thresh:.2f}", ge(FOLLOW100, f_thresh), "near_parallel")
    for p_thresh in parallel15s:
        add(f"parallel15 >= {p_thresh:.2f}", ge(PARALLEL15, p_thresh), "near_parallel")
    for p_thresh in parallel30s:
        add(f"parallel30 >= {p_thresh:.2f}", ge(PARALLEL30, p_thresh), "near_parallel")
    for thresh in iqrs:
        add(f"osm_distance_iqr_m <= {thresh}", le(IQR, thresh), "near_parallel")
    for thresh in elongs:
        add(f"component_elongation >= {thresh}", ge(ELONGATION, thresh), "near_parallel")

    for f_thresh, p_thresh in product(follow100s, parallel15s):
        add(
            f"follow100 >= {f_thresh:.2f} AND parallel15 >= {p_thresh:.2f}",
            all_of(ge(FOLLOW100, f_thresh), ge(PARALLEL15, p_thresh)),
            "near_parallel",
        )
    for f_thresh, p_thresh in product(follow100s, parallel30s):
        add(
            f"follow100 >= {f_thresh:.2f} AND parallel30 >= {p_thresh:.2f}",
            all_of(ge(FOLLOW100, f_thresh), ge(PARALLEL30, p_thresh)),
            "near_parallel",
        )
    for f_thresh, p_thresh, iqr in product(follow100s, parallel15s, iqrs):
        add(
            f"follow100 >= {f_thresh:.2f} AND parallel15 >= {p_thresh:.2f} AND iqr <= {iqr}",
            all_of(ge(FOLLOW100, f_thresh), ge(PARALLEL15, p_thresh), le(IQR, iqr)),
            "near_parallel",
        )
    for f_thresh, p_thresh, elong in product(follow100s, parallel15s, elongs):
        add(
            f"follow100 >= {f_thresh:.2f} AND parallel15 >= {p_thresh:.2f} AND elong >= {elong}",
            all_of(ge(FOLLOW100, f_thresh), ge(PARALLEL15, p_thresh), ge(ELONGATION, elong)),
            "near_parallel",
        )
    for f_thresh, p_thresh, iqr, elong in product(follow100s, parallel15s, iqrs, elongs):
        add(
            f"follow100 >= {f_thresh:.2f} AND parallel15 >= {p_thresh:.2f} AND iqr <= {iqr} AND elong >= {elong}",
            all_of(
                ge(FOLLOW100, f_thresh),
                ge(PARALLEL15, p_thresh),
                le(IQR, iqr),
                ge(ELONGATION, elong),
            ),
            "near_parallel",
        )
    for f_thresh, p_thresh, peak in product(follow100s, parallel15s, peaks):
        add(
            f"follow100 >= {f_thresh:.2f} AND parallel15 >= {p_thresh:.2f} AND peak <= {peak}",
            all_of(ge(FOLLOW100, f_thresh), ge(PARALLEL15, p_thresh), le(PEAK_DIST, peak)),
            "near_parallel",
        )
    return rules


def export_remaining_v2_geojson(path, remaining_rows):
    features = []
    for row in remaining_rows:
        lon = parse_float(row.get("center_lon"))
        lat = parse_float(row.get("center_lat"))
        if lon is None or lat is None:
            continue
        props = {
            "id": row.get("candidate_id", ""),
            "candidate_id": row.get("candidate_id", ""),
            "mr_status": row.get("_mr_status", ""),
            "remaining_category": row.get("remaining_category", ""),
            "remaining_reason": row.get("remaining_reason", ""),
            "written_to_geojson": row.get("written_to_geojson", ""),
            "accepted": row.get("accepted", ""),
        }
        for field in GEOJSON_ANALYSIS_FIELDS + [IQR, "geometry_length_m", "geometry_area_m2", "strava_max"]:
            props[field] = row.get(field, "")
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    write_feature_collection(path, features)
    return len(features)


def production_flag_counts(accepted):
    parallel = sum(1 for row in accepted if parse_bool(row.get("suppressed_parallel_osm")))
    ferry = sum(1 for row in accepted if parse_bool(row.get("suppressed_ferry")))
    overlap = sum(
        1
        for row in accepted
        if parse_bool(row.get("suppressed_parallel_osm")) and parse_bool(row.get("suppressed_ferry"))
    )
    written = sum(1 for row in accepted if parse_bool(row.get("written_to_geojson")))
    return {
        "accepted": len(accepted),
        "parallel": parallel,
        "ferry": ferry,
        "overlap": overlap,
        "union": parallel + ferry - overlap,
        "written": written,
    }


def run_remaining_v2_analysis(remaining, accepted, diagnostics, args):
    counts = production_flag_counts(accepted)
    print_section("Production diagnostics snapshot (flags as written, not reconstructed)")
    print_kv("Accepted", counts["accepted"])
    print_kv("suppressed_parallel_osm", counts["parallel"])
    print_kv("suppressed_ferry", counts["ferry"])
    print_kv("suppression_overlap", counts["overlap"])
    print_kv("union (parallel + ferry - overlap)", counts["union"])
    print_kv("written_to_geojson", counts["written"])
    print(
        "Remaining is defined as accepted==true AND written_to_geojson==true. "
        "Production suppression predicates are not re-applied."
    )

    for row in remaining:
        category = classify_remaining_v2(row)
        row["remaining_category"] = category
        row["remaining_reason"] = remaining_reason_v2(row, category)

    remain_total = len(remaining)
    remain_nai = sum(1 for row in remaining if row.get("_mr_status") == STATUS_NAI)
    remain_fixed = sum(1 for row in remaining if row.get("_mr_status") == STATUS_FIXED)
    remain_th = sum(1 for row in remaining if row.get("_mr_status") == STATUS_TOO_HARD)
    remain_other = [
        row for row in remaining
        if row.get("_mr_status") and row.get("_mr_status") not in {STATUS_NAI, STATUS_FIXED, STATUS_TOO_HARD}
    ]
    remain_unmatched = [row for row in remaining if not row.get("_mr_status")]
    other_status = Counter(row["_mr_status"] for row in remain_other)

    print_section("Remaining status counts (written_to_geojson == true)")
    print_kv("total Remaining", remain_total)
    print_kv("Not_An_Issue", remain_nai)
    print_kv("Fixed", remain_fixed)
    print_kv("Too_Hard", remain_th)
    print_kv("other MapRoulette statuses", len(remain_other))
    if other_status:
        for status, count in sorted(other_status.items(), key=lambda item: (-item[1], item[0])):
            print_kv(f"  {status}", count)
    print_kv("unmatched", len(remain_unmatched))

    ferry_left = []
    for row in remaining:
        dist = parse_float(row.get(FERRY_DIST))
        if dist is not None and dist <= 500:
            ferry_left.append(row)
    if ferry_left:
        print()
        print("*** CONSISTENCY WARNING: Remaining candidates with nearest_ferry_distance_m <= 500 ***")
        print("The production --suppress-ferry rule should have omitted these.")
        for row in ferry_left:
            print(
                f"  {row['candidate_id']}  status={row.get('_mr_status') or '(unmatched)'}  "
                f"ferry={row.get(FERRY_DIST)}  "
                f"written_to_geojson={row.get('written_to_geojson')}  "
                f"suppressed_ferry={row.get('suppressed_ferry')}"
            )
    else:
        print()
        print("Ferry consistency: no Remaining candidate has nearest_ferry_distance_m <= 500.")

    print()
    print("Methodological note:")
    print(
        "Historical Fixed candidates are affected by post-edit OSM data. "
        "Their current nearest-OSM/follow/parallel measurements may describe the "
        "road that was added when the task was fixed. Use Fixed hits as warning "
        "controls; do not treat this dataset as an unbiased classifier benchmark. "
        "Prefer semantically targeted rules (ferry, tunnel, construction) over "
        "broad distance thresholds when evidence is otherwise comparable."
    )

    print_candidate_metrics("Remaining Fixed controls", by_status(remaining, STATUS_FIXED))
    print_candidate_metrics("Remaining Too_Hard", by_status(remaining, STATUS_TOO_HARD))

    by_cat = {cat: [] for cat in V2_CATEGORY_ORDER}
    for row in remaining:
        by_cat[row["remaining_category"]].append(row)

    print_section("Remaining category summary")
    header = (
        f"{'category':<28} {'tot':>4} {'NAI':>4} {'Fix':>4} {'TH':>3} "
        f"{'unm':>3} {'% rem':>7} {'% NAI':>7}"
    )
    print(header)
    print("-" * len(header))
    for cat in V2_CATEGORY_ORDER:
        rows = by_cat[cat]
        nai = sum(1 for row in rows if row.get("_mr_status") == STATUS_NAI)
        fixed = sum(1 for row in rows if row.get("_mr_status") == STATUS_FIXED)
        too_hard = sum(1 for row in rows if row.get("_mr_status") == STATUS_TOO_HARD)
        unmatched = sum(1 for row in rows if not row.get("_mr_status"))
        print(
            f"{cat:<28} {len(rows):>4} {nai:>4} {fixed:>4} {too_hard:>3} "
            f"{unmatched:>3} {fmt_pct(len(rows), remain_total):>7} "
            f"{fmt_pct(nai, remain_nai):>7}"
        )

    print_section("A) Construction")
    rules = build_remaining_v2_rules()
    results = [
        evaluate_remaining_v2_rule(name, pred, family, remaining, complexity)
        for name, pred, family, complexity in rules
    ]
    by_name = {row["rule_name"]: row for row in results}

    for name in [
        "construction <= 35",
        "construction <= 50",
        "construction <= 75",
        "construction <= 100",
        "construction <= 150",
        "nearest tags highway=construction",
        "nearest tags construction=*",
        "nearest tags construction=yes",
    ]:
        row = by_name[name]
        print(
            f"{name:<40} tot={row['removed_remaining_total']}  "
            f"NAI={row['removed_remaining_NAI']}  "
            f"Fixed={row['removed_remaining_Fixed']}  "
            f"Too_Hard={row['removed_remaining_Too_Hard']}"
        )
        pred = next(item[1] for item in rules if item[0] == name)
        hits = [item for item in remaining if pred(item)]
        if not hits:
            print("  (none)")
            continue
        for item in hits:
            tags = item.get(TAGS) or ""
            print(
                f"  {item['candidate_id']}  status={item.get('_mr_status') or '(unmatched)'}  "
                f"constr_dist={item.get(CONSTR_DIST, '')}  "
                f"hwy={item.get('nearest_osm_highway', '')}  "
                f"tags={tags}"
            )

    print_section("B) Tunnel")
    tunnel_rows = [row for row in remaining if is_tunnel_any(row)]
    if not tunnel_rows:
        print("  (none)")
    else:
        header = (
            f"{'candidate_id':<28} {'status':<14} {'osm_id':<12} {'hwy':<12} "
            f"{'name':<22} {'peak':>8} {'p90':>8} "
            f"{'f50':>6} {'f75':>6} {'f100':>6} {'p15':>6} {'p30':>6} "
            f"{'px':>5} {'len':>7}"
        )
        print(header)
        print("-" * len(header))
        for row in tunnel_rows:
            print(
                f"{row.get('candidate_id', ''):<28} "
                f"{(row.get('_mr_status') or '(unmatched)'):<14} "
                f"{(row.get('nearest_osm_id') or ''):<12} "
                f"{(row.get('nearest_osm_highway') or ''):<12} "
                f"{(row.get('nearest_osm_name') or '')[:22]:<22} "
                f"{(row.get(PEAK_DIST) or ''):>8} "
                f"{(row.get(P90) or ''):>8} "
                f"{(row.get(FOLLOW50) or ''):>6} "
                f"{(row.get(FOLLOW75) or ''):>6} "
                f"{(row.get(FOLLOW100) or ''):>6} "
                f"{(row.get(PARALLEL15) or ''):>6} "
                f"{(row.get(PARALLEL30) or ''):>6} "
                f"{(row.get('component_pixels') or ''):>5} "
                f"{(row.get('geometry_length_m') or ''):>7}"
            )
            print(f"    tags={row.get(TAGS) or ''}")
    print()
    print("Tunnel rule counts:")
    for row in results:
        if row["family"] == "tunnel":
            print(
                f"  {row['rule_name']:<42} tot={row['removed_remaining_total']}  "
                f"NAI={row['removed_remaining_NAI']}  "
                f"Fixed={row['removed_remaining_Fixed']}  "
                f"Too_Hard={row['removed_remaining_Too_Hard']}"
            )

    peak_rules = [row for row in results if row["family"] == "close_peak"]
    p90_rules = [row for row in results if row["family"] == "close_p90"]
    print_section("C) Close-to-existing-OSM (warning controls, not production recommendations)")
    print(
        "Broad distance cuts are listed only as measurements. "
        "A high NAI count is not a reason to adopt them."
    )
    print_control_hits("nearest_osm_distance_m thresholds", peak_rules)
    print_control_hits("osm_distance_p90_m thresholds", p90_rules)

    print_section("D) Near-parallel misses vs Fixed control 14/8305/6233/875/927")
    control = next((row for row in remaining if row.get("candidate_id") == FIXED_CONTROL_TRACK), None)
    if control is None:
        print("Fixed control 14/8305/6233/875/927 is not in Remaining.")
    else:
        print(
            "Control metrics: "
            f"peak={control.get(PEAK_DIST)} p90={control.get(P90)} iqr={control.get(IQR)} "
            f"follow100={control.get(FOLLOW100)} parallel15={control.get(PARALLEL15)} "
            f"parallel30={control.get(PARALLEL30)} elong={control.get(ELONGATION)} "
            f"px={control.get('component_pixels')}"
        )
        print(
            "A rule that also matches this control cannot be a low-risk parallel-miss filter. "
            "High elongation does not separate it: the control itself is highly elongated."
        )

    print_section("E) Strava-only distributions (Remaining only; small Fixed n)")
    groups = [
        ("Not_An_Issue", [row for row in remaining if row.get("_mr_status") == STATUS_NAI]),
        ("Fixed", [row for row in remaining if row.get("_mr_status") == STATUS_FIXED]),
        ("Too_Hard", [row for row in remaining if row.get("_mr_status") == STATUS_TOO_HARD]),
    ]
    header = f"{'field':<22} {'group':<14} {'n':>3} {'min':>8} {'p25':>8} {'med':>8} {'mean':>8} {'p75':>8} {'max':>8}"
    print(header)
    print("-" * len(header))
    for field in STRAVA_DIST_FIELDS:
        for label, rows in groups:
            stats = describe_values([parse_float(row.get(field)) for row in rows])
            print(
                f"{field:<22} {label:<14} {stats['n']:>3} "
                f"{stats['min']:>8} {stats['p25']:>8} {stats['median']:>8} "
                f"{stats['mean']:>8} {stats['p75']:>8} {stats['max']:>8}"
            )
    print("Do not derive a production rule from this Fixed sample size.")

    print_section("F) Highway / route / railway / aeroway grouping")
    for field in (
        "nearest_osm_highway",
        "nearest_osm_route",
        "nearest_osm_railway",
        "nearest_osm_aeroway",
    ):
        print()
        print(field)
        groups_field = Counter((row.get(field) or "(empty)") for row in remaining)
        print(f"{'value':<28} {'tot':>4} {'NAI':>4} {'Fix':>4} {'TH':>3}")
        print("-" * 48)
        for value, _count in sorted(groups_field.items(), key=lambda item: (-item[1], item[0])):
            rows = [row for row in remaining if (row.get(field) or "(empty)") == value]
            nai = sum(1 for row in rows if row.get("_mr_status") == STATUS_NAI)
            fixed = sum(1 for row in rows if row.get("_mr_status") == STATUS_FIXED)
            too_hard = sum(1 for row in rows if row.get("_mr_status") == STATUS_TOO_HARD)
            print(f"{value:<28} {len(rows):>4} {nai:>4} {fixed:>4} {too_hard:>3}")

    unique_results = dedupe_equivalent_rules(results)
    unique_results.sort(
        key=lambda row: (
            -row["removed_remaining_NAI"],
            row["removed_remaining_Fixed"],
            row["removed_remaining_Too_Hard"],
            row["complexity"],
            row["rule_name"],
        )
    )

    safest = [
        row
        for row in unique_results
        if row["removed_remaining_Fixed"] == 0
        and row["removed_remaining_Too_Hard"] == 0
        and row["removed_remaining_NAI"] > 0
    ]
    safest.sort(
        key=lambda row: (-row["removed_remaining_NAI"], row["complexity"], row["rule_name"])
    )
    conservative = [
        row
        for row in unique_results
        if row["removed_remaining_Fixed"] == 0 and row["removed_remaining_NAI"] > 0
    ]
    conservative.sort(
        key=lambda row: (
            -row["removed_remaining_NAI"],
            row["complexity"],
            row["removed_remaining_Too_Hard"],
            row["rule_name"],
        )
    )
    exploratory = [
        row
        for row in unique_results
        if row["removed_remaining_Fixed"] == 1 and row["removed_remaining_NAI"] > 0
    ]
    exploratory.sort(
        key=lambda row: (
            -row["removed_remaining_NAI"],
            row["removed_remaining_Too_Hard"],
            row["complexity"],
            row["rule_name"],
        )
    )

    print_section("G1) Safest additional rules (0 Fixed and 0 Too_Hard)")
    if safest:
        print_v2_rule_table(safest)
    else:
        print("  (none)")

    print_section("G2) Conservative rules (0 Fixed; max NAI, then min complexity)")
    if conservative:
        print_v2_rule_table(conservative[:20])
        if len(conservative) > 20:
            print(f"... {len(conservative) - 20} more 0-Fixed rules in remaining-v2-rule-results.csv")
    else:
        print("  (none)")

    print_section("G3) Exploratory rules (exactly 1 Fixed) — UNSAFE FOR PRODUCTION")
    if exploratory:
        print_v2_rule_table(exploratory[:25], unsafe=True)
        if len(exploratory) > 25:
            print(f"... {len(exploratory) - 25} more unsafe rules in remaining-v2-rule-results.csv")
    else:
        print("  (none)")

    caught = set()
    for row in safest:
        caught.update(row["_removed_ids"])
    leftover_nai = [
        row
        for row in remaining
        if row.get("_mr_status") == STATUS_NAI and row.get("candidate_id") not in caught
    ]
    leftover_nai.sort(
        key=lambda row: (
            parse_float(row.get(PEAK_DIST)) is None,
            parse_float(row.get(PEAK_DIST)) or 0,
            row.get("candidate_id"),
        )
    )
    print_section("H) Remaining Not_An_Issue not caught by any 0-Fixed / 0-Too_Hard rule")
    print(
        f"{len(leftover_nai)} NAI of {remain_nai} Remaining NAI are outside the union of safest rules. "
        "These are the next manual-inspection set."
    )
    print_candidate_metrics("Inspection list", leftover_nai)

    internal = {
        "_mr_status",
        "_mr_task_id",
        "_mr_mapper",
        "remaining_category",
        "remaining_reason",
    }
    remaining_fields = [
        key for key in (diagnostics[0].keys() if diagnostics else []) if key not in internal
    ]
    for field in V2_CSV_EXTRA:
        if field not in remaining_fields:
            remaining_fields.append(field)
    csv_rows = []
    for row in remaining:
        out = {key: value for key, value in row.items() if key not in internal}
        out["mr_status"] = row.get("_mr_status", "")
        out["mr_task_id"] = row.get("_mr_task_id", "")
        out["remaining_category"] = row.get("remaining_category", "")
        out["remaining_reason"] = row.get("remaining_reason", "")
        csv_rows.append(out)
    write_csv(args.remaining_v2, csv_rows, remaining_fields)
    write_csv(
        args.remaining_v2_rules,
        [{key: row[key] for key in V2_RULE_COLUMNS} for row in unique_results],
        V2_RULE_COLUMNS,
    )
    n_geo = export_remaining_v2_geojson(args.remaining_v2_geojson, remaining)

    print_section("Remaining v2 output files")
    print_kv("analysis-remaining-v2.csv", args.remaining_v2)
    print_kv("remaining-v2-rule-results.csv", args.remaining_v2_rules)
    print_kv("remaining-v2.geojson", f"{args.remaining_v2_geojson} ({n_geo} features)")
    print_kv("Rules tested", len(results))
    print_kv("Deduplicated equivalent rules", len(unique_results))
    print_kv("Safest rules", len(safest))

    print_section("Remaining v2 measured-data summary")
    print(
        f"Remaining after production: {remain_total} "
        f"(NAI {remain_nai}, Fixed {remain_fixed}, Too_Hard {remain_th}, "
        f"other {len(remain_other)}, unmatched {len(remain_unmatched)})."
    )
    if safest:
        top = safest[0]
        print(
            f"Best 0-Fixed/0-Too_Hard increment: {top['rule_name']} "
            f"removes {top['removed_remaining_NAI']} NAI."
        )
    else:
        print("No 0-Fixed/0-Too_Hard incremental rule removed Remaining NAI.")
    print("No production rule was changed by this analysis.")
    return unique_results


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="Analyze diagnostic suppression rules (read-only)."
    )
    parser.add_argument("diagnostics_csv")
    parser.add_argument("maproulette_csv")
    parser.add_argument("--rule-results", default="rule-results.csv")
    parser.add_argument("--joined", default="joined-accepted.csv")
    parser.add_argument(
        "--geojson",
        metavar="SOURCE_GEOJSON",
        help="Detector GeoJSON for analysis-only would-filter/would-keep export (does not modify the source)",
    )
    parser.add_argument(
        "--geojson-filter",
        default="analysis-would-filter.geojson",
        help="Output path for candidates the measured rule would filter",
    )
    parser.add_argument(
        "--geojson-keep",
        default="analysis-would-keep.geojson",
        help="Output path for candidates the measured rule would keep",
    )
    parser.add_argument(
        "--remaining",
        default="analysis-remaining.csv",
        help="CSV of would-keep candidates with remaining-category fields",
    )
    parser.add_argument(
        "--remaining-rules",
        default="remaining-rule-results.csv",
        help="CSV of incremental rules evaluated only on would-keep candidates",
    )
    parser.add_argument(
        "--remaining-v2",
        default="analysis-remaining-v2.csv",
        help="CSV of production Remaining candidates (written_to_geojson==true)",
    )
    parser.add_argument(
        "--remaining-v2-rules",
        default="remaining-v2-rule-results.csv",
        help="CSV of incremental rules evaluated only on production Remaining candidates",
    )
    parser.add_argument(
        "--remaining-v2-geojson",
        default="remaining-v2.geojson",
        help="GeoJSON of production Remaining candidates built from diagnostic coordinates",
    )
    args = parser.parse_args(argv)

    diagnostics = load_csv(args.diagnostics_csv)
    maproulette = load_csv(args.maproulette_csv)

    accepted = [row for row in diagnostics if parse_bool(row.get("accepted"))]
    accepted_ids = {row["candidate_id"] for row in accepted}
    all_diag_ids = {row["candidate_id"] for row in diagnostics}

    mr_by_name = {}
    for row in maproulette:
        name = (row.get("TaskName") or "").strip()
        if name:
            mr_by_name[name] = row
    mr_status_counts = Counter(
        normalize_status(row.get("TaskStatus")) for row in maproulette
    )
    mr_fixed_ids = {
        name
        for name, row in mr_by_name.items()
        if normalize_status(row.get("TaskStatus")) == STATUS_FIXED
    }

    for row in accepted:
        mr = mr_by_name.get(row["candidate_id"])
        row["_mr_status"] = normalize_status(mr.get("TaskStatus") if mr else "")
        row["_mr_task_id"] = (mr.get("TaskID") if mr else "") or ""
        row["_mr_mapper"] = (mr.get("Mapper") if mr else "") or ""

    matched = [row for row in accepted if row["_mr_status"]]
    matched_status = Counter(row["_mr_status"] for row in matched)
    nai_total = matched_status.get(STATUS_NAI, 0)
    fixed_total = matched_status.get(STATUS_FIXED, 0)
    too_hard_total = matched_status.get(STATUS_TOO_HARD, 0)
    unlabeled = [row for row in accepted if not row["_mr_status"]]
    missing_fixed = sorted(mr_fixed_ids - all_diag_ids)
    missing_fixed_accepted = sorted(mr_fixed_ids - accepted_ids)

    print_section("Inputs")
    print_kv("Diagnostics CSV", args.diagnostics_csv)
    print_kv("MapRoulette CSV", args.maproulette_csv)
    print_kv("Join", "diagnostics.candidate_id == maproulette.TaskName")
    print_kv("Evaluated rows", "accepted == true only")

    print_section("MapRoulette tasks by status")
    for status, count in sorted(mr_status_counts.items(), key=lambda item: (-item[1], item[0])):
        print_kv(status or "(empty)", count)
    print_kv("Total MR tasks", len(maproulette))

    print_section("Diagnostics vs MapRoulette coverage")
    print_kv("Diagnostics rows (all)", len(diagnostics))
    print_kv("Accepted diagnostics candidates", len(accepted))
    print_kv("Accepted matched to MR", len(matched))
    print_kv("Matched Fixed", fixed_total)
    print_kv("Matched Not_An_Issue", nai_total)
    print_kv("Matched Too_Hard", too_hard_total)
    print_kv("Accepted with no MR label", len(unlabeled))
    print_kv("MR Fixed absent from diagnostics CSV", len(missing_fixed))
    print_kv("MR Fixed absent from accepted diagnostics", len(missing_fixed_accepted))
    print_kv(
        "Loss-rate caveat",
        f"only {fixed_total} matched Fixed remain; removing 1 Fixed = {fmt_rate(1, fixed_total) or 'n/a'} loss",
    )
    print()
    print(
        "NOTE: The diagnostics run happened after many Fixed tasks were mapped "
        "into OSM. Missing historical Fixed tasks are NOT treated as filtered "
        "by the candidate rules below."
    )
    if missing_fixed:
        print()
        print("MR Fixed TaskNames no longer present in diagnostics:")
        for task_id in missing_fixed:
            print(f"  {task_id}")

    has_written_col = bool(diagnostics) and "written_to_geojson" in diagnostics[0]
    if has_written_col:
        remaining = [
            row for row in accepted if parse_bool(row.get("written_to_geojson"))
        ]
        run_remaining_v2_analysis(remaining, accepted, diagnostics, args)
        print_section("Recommendation (measured data only)")
        print("No production rule was changed by this analysis.")
        return 0

    print_section("Construction candidates among matched accepted (distance <= 100 m)")
    constr_hits = []
    for row in matched:
        dist = parse_float(row.get(CONSTR_DIST))
        if dist is None or dist > 100:
            continue
        constr_hits.append(row)
        print(
            f"  {row['candidate_id']:<28}  status={row['_mr_status']:<14}  "
            f"dist={dist:.3f} m  id={row.get(CONSTR_ID, '')}  "
            f"name={row.get(CONSTR_NAME, '')}"
        )
    if not constr_hits:
        print("  (none)")

    rules = build_rules()
    results = [
        evaluate_rule(name, pred, accepted, nai_total, fixed_total)
        for name, pred in rules
    ]

    print_section(f"All simulated rules ({len(results)})")
    print(
        "A rule 'removes' a current accepted candidate if the predicate is true. "
        "Blank numeric fields do not satisfy a comparison."
    )
    print()
    print_rule_table(results)

    frontier = pareto_frontier(results)
    print_section("Pareto frontier")
    print(
        "A rule is on the frontier if no other tested rule removes at least as "
        "many Not_An_Issue and at most as many Fixed, with a strict improvement "
        "on at least one of those two objectives. Too_Hard and unlabeled are "
        "ignored for these two objectives."
    )
    print()
    print_rule_table(frontier)

    safest = [
        row
        for row in results
        if row["removed_fixed"] == 0 and row["removed_not_an_issue"] > 0
    ]
    safest.sort(
        key=lambda row: (-row["removed_not_an_issue"], -row["removed_total_current"], row["rule_name"])
    )
    conservative = [
        row
        for row in results
        if row["_loss"] is not None and row["_loss"] <= 0.02 and row["removed_not_an_issue"] > 0
    ]
    conservative.sort(
        key=lambda row: (-row["removed_not_an_issue"], row["removed_fixed"], row["rule_name"])
    )
    aggressive = [
        row
        for row in results
        if row["_loss"] is not None and row["_loss"] <= 0.05 and row["removed_not_an_issue"] > 0
    ]
    aggressive.sort(
        key=lambda row: (-row["removed_not_an_issue"], row["removed_fixed"], row["rule_name"])
    )

    print_section("Safest rules (fixed_loss_rate == 0)")
    if safest:
        print_rule_table(safest)
    else:
        print("  (none)")

    print_section("Conservative rules (fixed_loss_rate <= 0.02)")
    if conservative:
        print_rule_table(conservative)
    else:
        print("  (none)")

    print_section("Aggressive rules (fixed_loss_rate <= 0.05)")
    if aggressive:
        print_rule_table(aggressive)
    else:
        print("  (none)")

    best = []
    seen = set()
    for pool in (frontier, conservative, aggressive, safest):
        for row in pool:
            if row["rule_name"] in seen:
                continue
            if row["removed_not_an_issue"] <= 0:
                continue
            seen.add(row["rule_name"])
            best.append(row)
            if len(best) >= 10:
                break
        if len(best) >= 10:
            break

    by_id = {row["candidate_id"]: row for row in accepted}
    print_section("Fixed candidates removed by the best 10 candidate rules")
    print("Best 10 = Pareto, then conservative/aggressive fill, skipping empty-NAI rules.")
    if not best:
        print("  (none)")
    for rank, rule in enumerate(best, start=1):
        print()
        print(f"{rank}. {rule['rule_name']}")
        print(
            f"   NAI={rule['removed_not_an_issue']}  Fixed={rule['removed_fixed']}  "
            f"Recall={rule['not_an_issue_recall'] or '-'}  "
            f"Loss={rule['fixed_loss_rate'] or '-'}  "
            f"Prec={rule['precision_of_removed'] or '-'}"
        )
        fixed_ids = rule["_removed_ids_fixed"]
        if not fixed_ids:
            print("   No Fixed candidates removed.")
            continue
        print(
            f"   {'candidate_id':<28} {'peak_m':>8} {'hwy':<12} {'name':<28} "
            f"{'f50':>6} {'f75':>6} {'f100':>6} {'p15':>6} {'p30':>6} "
            f"{'elong':>7} {'p90':>8}"
        )
        for candidate_id in fixed_ids:
            row = by_id[candidate_id]
            print(
                f"   {candidate_id:<28} "
                f"{(row.get(PEAK_DIST) or ''):>8} "
                f"{(row.get('nearest_osm_highway') or ''):<12} "
                f"{(row.get('nearest_osm_name') or ''):<28} "
                f"{(row.get(FOLLOW50) or ''):>6} "
                f"{(row.get(FOLLOW75) or ''):>6} "
                f"{(row.get(FOLLOW100) or ''):>6} "
                f"{(row.get(PARALLEL15) or ''):>6} "
                f"{(row.get(PARALLEL30) or ''):>6} "
                f"{(row.get(ELONGATION) or ''):>7} "
                f"{(row.get(P90) or ''):>8}"
            )

    internal = {"_mr_status", "_mr_task_id", "_mr_mapper"}
    joined_rows = []
    joined_fields = [
        key for key in (diagnostics[0].keys() if diagnostics else []) if key not in internal
    ]
    extra_fields = ["mr_task_id", "mr_status", "mr_mapper"]
    for field in extra_fields:
        if field not in joined_fields:
            joined_fields.append(field)
    for row in accepted:
        out = {key: value for key, value in row.items() if key not in internal}
        out["mr_task_id"] = row["_mr_task_id"]
        out["mr_status"] = row["_mr_status"]
        out["mr_mapper"] = row["_mr_mapper"]
        joined_rows.append(out)

    export_rules = [{key: row[key] for key in RULE_RESULT_COLUMNS} for row in results]
    write_csv(args.rule_results, export_rules, RULE_RESULT_COLUMNS)
    write_csv(args.joined, joined_rows, joined_fields)

    print_section("Output files")
    print_kv("rule-results.csv", args.rule_results)
    print_kv("joined-accepted.csv", args.joined)
    print_kv("Rules evaluated", len(results))
    print_kv("Pareto rules", len(frontier))

    if args.geojson:
        geo = export_visual_geojson(
            args.geojson,
            accepted,
            args.geojson_filter,
            args.geojson_keep,
        )
        print_section("GeoJSON visual validation (analysis-only)")
        print_kv("Source GeoJSON", args.geojson)
        print_kv("Rule", VISUAL_FILTER_REASON)
        print_kv("Source not modified", "true")
        print_kv("source features", geo["source_features"])
        print_kv("matched features", geo["matched_features"])
        print_kv("would filter", geo["would_filter"])
        print_kv("would keep", geo["would_keep"])
        print_kv("would-filter file", args.geojson_filter)
        print_kv("would-keep file", args.geojson_keep)
        if geo["missing_ids"]:
            print()
            print("Missing candidate IDs (accepted diagnostics not in source GeoJSON):")
            for candidate_id in geo["missing_ids"]:
                print(f"  {candidate_id}")
        else:
            print_kv("missing candidate IDs", "none")
        if geo["unmatched_source_ids"]:
            print()
            print("Source GeoJSON features without an accepted diagnostics row:")
            for candidate_id in geo["unmatched_source_ids"]:
                print(f"  {candidate_id or '(empty id)'}")
        print()
        print("MR status distribution: would-filter")
        if geo["filter_status"]:
            for status, count in sorted(geo["filter_status"].items(), key=lambda item: (-item[1], item[0])):
                print_kv(status or "(empty)", count)
        else:
            print("  (none)")
        print()
        print("MR status distribution: would-keep")
        if geo["keep_status"]:
            for status, count in sorted(geo["keep_status"].items(), key=lambda item: (-item[1], item[0])):
                print_kv(status or "(empty)", count)
        else:
            print("  (none)")

    run_remaining_analysis(accepted, diagnostics, args)

    print_section("Recommendation (measured data only)")
    for line in recommend(safest, conservative, aggressive, nai_total, fixed_total):
        print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
