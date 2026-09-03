#!/usr/bin/env python3
"""Ground-truth analysis of all 292 All-only MapRoulette reviews.

Read-only. No detector rerun, no production changes.
"""
from __future__ import annotations

import csv
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyze_diagnostics import parse_float  # noqa: E402
from validation.analyze_open_area import (  # noqa: E402
    OSM_PATH,
    component_merc_for,
    evaluate_class_predicates,
    lat2y,
    load_open_areas,
    lon2x,
)

MR_PATH = ROOT / "validation" / "challenge_56718-ALL-292_tasks.csv"
FULL_PATH = ROOT / "validation" / "all-only-full.csv"
OUT_MD = ROOT / "validation" / "all-292-ground-truth-analysis.md"

POSITIVE = frozenset({"Fixed", "Already_Fixed"})
STATUSES = ("Fixed", "Already_Fixed", "Not_An_Issue", "Too_Hard", "Skipped")


def load_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def norm_status(s):
    return (s or "").strip().replace(" ", "_")


def pf(v):
    return parse_float(v)


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def fmt(v, nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def classify_nai_comment(row):
    text = (
        (row.get("_comments") or "")
        + " "
        + (row.get("_tags") or "")
    ).lower()
    if "golf" in text:
        return "golf"
    if "beach" in text or "sand" in text or "natural=beach" in text:
        return "beach/sand"
    if "gps" in text or "scatter" in text:
        return "gps_scatter"
    if "event" in text or "marathon" in text or "race" in text:
        return "event"
    if "climb" in text:
        return "climbing"
    if not text.strip():
        return "other/no_comment"
    return "other"


def join_rows():
    full = {r["candidate_id"]: r for r in load_csv(FULL_PATH)}
    mr = load_csv(MR_PATH)
    if len(mr) != 292 or len(full) != 292:
        raise SystemExit(f"Expected 292 rows, got mr={len(mr)} full={len(full)}")
    joined = []
    missing = []
    for m in mr:
        cid = m["TaskName"].strip()
        if cid not in full:
            missing.append(cid)
            continue
        row = dict(full[cid])
        row["_status"] = norm_status(m.get("TaskStatus"))
        row["_comments"] = (m.get("Comments") or "").strip()
        row["_tags"] = (m.get("Tags") or "").strip()
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
    if missing:
        raise SystemExit(f"Missing from full csv: {missing}")
    if len(joined) != 292:
        raise SystemExit(f"Join count {len(joined)} != 292")
    return joined


def attach_golf_metrics(rows, lookup):
    for i, row in enumerate(rows, 1):
        mx, my = lon2x(row["_lon"]), lat2y(row["_lat"])
        comp, err = component_merc_for(row, "all")
        row["_comp_err"] = err
        row["_golf_pred"] = evaluate_class_predicates(mx, my, comp, lookup, "golf_course")
        if i % 50 == 0 or i == len(rows):
            print(f"  golf metrics {i}/{len(rows)}", flush=True)


def golf_predicates():
    return [
        ("center_inside golf_course", lambda g: g["center_inside"]),
        ("inside_frac >= 0.25", lambda g: g["inside_frac"] is not None and g["inside_frac"] >= 0.25),
        ("inside_frac >= 0.50", lambda g: g["inside_frac"] is not None and g["inside_frac"] >= 0.50),
        ("inside_frac >= 0.75", lambda g: g["inside_frac"] is not None and g["inside_frac"] >= 0.75),
        ("inside_frac >= 0.90", lambda g: g["inside_frac"] is not None and g["inside_frac"] >= 0.90),
        ("inside_frac >= 0.99", lambda g: g["inside_frac"] is not None and g["inside_frac"] >= 0.99),
        ("stays_inside golf_course", lambda g: g["stays_inside"]),
        ("center_inside AND crosses", lambda g: g["center_inside"] and g["crosses"]),
        ("center_inside AND NOT crosses", lambda g: g["center_inside"] and not g["crosses"]),
        ("crosses golf (any frac)", lambda g: g["crosses"]),
        ("has_nearby golf (250m+)", lambda g: g["has_nearby"]),
        ("distance <= 25m (not inside)", lambda g: not g["center_inside"] and g["distance_m"] is not None and g["distance_m"] <= 25),
        ("distance <= 50m (not inside)", lambda g: not g["center_inside"] and g["distance_m"] is not None and g["distance_m"] <= 50),
        (
            "semantic: stays_inside OR (center_inside AND inside_frac>=0.90)",
            lambda g: g["stays_inside"]
            or (g["center_inside"] and g["inside_frac"] is not None and g["inside_frac"] >= 0.90),
        ),
        (
            "semantic: internal (stays OR entire) not crossing-only",
            lambda g: (g["stays_inside"] or (g["inside_frac"] is not None and g["inside_frac"] >= 0.99))
            and not (g["crosses"] and not g["center_inside"]),
        ),
    ]


def is_golf_nai(row):
    text = ((row.get("_comments") or "") + " " + (row.get("_tags") or "")).lower()
    return row["_status"] == "Not_An_Issue" and "golf" in text


def predicate_stats(rows, pred_fn, label):
    by_status = {s: {"total": 0, "hit": 0} for s in STATUSES}
    golf_nai_total = sum(1 for r in rows if is_golf_nai(r))
    golf_nai_hit = sum(1 for r in rows if is_golf_nai(r) and pred_fn(r["_golf_pred"]))
    pos_removed = []
    for row in rows:
        st = row["_status"]
        if st not in by_status:
            continue
        by_status[st]["total"] += 1
        hit = pred_fn(row["_golf_pred"])
        if hit:
            by_status[st]["hit"] += 1
            if st in POSITIVE:
                pos_removed.append(row)
    nai_hit = by_status["Not_An_Issue"]["hit"]
    nai_total = by_status["Not_An_Issue"]["total"]
    pos_hit = (
        by_status["Fixed"]["hit"]
        + by_status["Already_Fixed"]["hit"]
    )
    pos_total = by_status["Fixed"]["total"] + by_status["Already_Fixed"]["total"]
    removed = nai_hit + pos_hit + by_status["Too_Hard"]["hit"] + by_status["Skipped"]["hit"]
    precision = nai_hit / removed if removed else None
    recall_golf_nai = golf_nai_hit / golf_nai_total if golf_nai_total else None
    return {
        "label": label,
        "by_status": by_status,
        "nai_hit": nai_hit,
        "nai_total": nai_total,
        "pos_hit": pos_hit,
        "pos_total": pos_total,
        "th_hit": by_status["Too_Hard"]["hit"],
        "skipped_hit": by_status["Skipped"]["hit"],
        "precision": precision,
        "recall_golf_nai": recall_golf_nai,
        "golf_nai_hit": golf_nai_hit,
        "golf_nai_total": golf_nai_total,
        "pos_removed": pos_removed,
    }


def counterexample_table(rows):
    lines = []
    lines.append(
        "| candidate_id | status | golf name | nearest hwy | osm_m | px | "
        "inside_frac | center | crosses | stays | comment |"
    )
    lines.append("|---|---|---|---|---:|---:|---:|---|---|---|---|")
    for row in sorted(rows, key=lambda r: r["_id"]):
        g = row["_golf_pred"]
        lines.append(
            f"| `{row['_id']}` | {row['_status']} | {g.get('name') or '—'} | "
            f"{row.get('nearest_osm_highway') or row.get('nearest_osm_context') or '—'} | "
            f"{fmt(pf(row.get('nearest_osm_distance_m')), 1)} | "
            f"{fmt(pf(row.get('component_pixels')), 0)} | "
            f"{fmt(g.get('inside_frac'), 3) if g.get('inside_frac') is not None else '—'} | "
            f"{g['center_inside']} | {g['crosses']} | {g['stays_inside']} | "
            f"{(row['_tags'] + ' ' + row['_comments']).strip() or '—'} |"
        )
    return lines


def threshold_proxy_analysis(rows):
    """Offline proxy only — diagnostics are from threshold=100 run."""
    lines = []
    lines.append(
        "These counts use **existing** component stats at threshold=100. "
        "Raising threshold would change component topology; this is a **proxy**, not a rerun."
    )
    lines.append("")
    pos = [r for r in rows if r["_status"] in POSITIVE]
    nai = [r for r in rows if r["_status"] == "Not_An_Issue"]
    for field, thresholds in (
        ("strava_mean", (110, 120, 130, 140)),
        ("strava_p90", (110, 120, 130, 140)),
        ("strava_max", (110, 120, 130, 140)),
        ("component_pixels", (50, 100, 200, 500)),
    ):
        lines.append(f"### {field} (Fixed+AF vs NAI medians: pos={fmt(med([pf(r.get(field)) for r in pos]), 1)} nai={fmt(med([pf(r.get(field)) for r in nai]), 1)})")
        lines.append("")
        lines.append("| cutoff | pos below/at | pos share | NAI below/at | NAI share |")
        lines.append("|---|---:|---:|---:|---:|")
        for t in thresholds:
            if field == "component_pixels":
                ph = sum(1 for r in pos if (pf(r.get(field)) or 0) <= t)
                nh = sum(1 for r in nai if (pf(r.get(field)) or 0) <= t)
            else:
                ph = sum(1 for r in pos if (pf(r.get(field)) or 999) < t)
                nh = sum(1 for r in nai if (pf(r.get(field)) or 999) < t)
            lines.append(
                f"| {field} {'<=' if field=='component_pixels' else '<'} {t} | "
                f"{ph}/{len(pos)} | {100*ph/len(pos):.1f}% | {nh}/{len(nai)} | {100*nh/len(nai):.1f}% |"
            )
        lines.append("")
    return lines


def metric_compare(rows, fields):
    lines = []
    groups = {
        "Fixed+Already_Fixed": [r for r in rows if r["_status"] in POSITIVE],
        "Not_An_Issue": [r for r in rows if r["_status"] == "Not_An_Issue"],
        "Too_Hard": [r for r in rows if r["_status"] == "Too_Hard"],
    }
    lines.append("| metric | Fixed+AF med | NAI med | Too_Hard med |")
    lines.append("|---|---:|---:|---:|")
    for f in fields:
        lines.append(
            f"| {f} | {fmt(med([pf(r.get(f)) for r in groups['Fixed+Already_Fixed']]), 3)} | "
            f"{fmt(med([pf(r.get(f)) for r in groups['Not_An_Issue']]), 3)} | "
            f"{fmt(med([pf(r.get(f)) for r in groups['Too_Hard']]), 3)} |"
        )
    return lines


def main():
    rows = join_rows()
    print(f"Join OK: {len(rows)}/292", flush=True)

    status_counts = Counter(r["_status"] for r in rows)
    print("Status:", dict(status_counts), flush=True)

    print("Loading open areas + golf metrics...", flush=True)
    lookup, class_counts, _ = load_open_areas(OSM_PATH)
    attach_golf_metrics(rows, lookup)

    # NAI comment themes
    nai_rows = [r for r in rows if r["_status"] == "Not_An_Issue"]
    themes = Counter(classify_nai_comment(r) for r in nai_rows)
    # refine golf: comment contains golf
    golf_comment_nai = sum(
        1 for r in nai_rows if "golf" in ((r["_comments"] + r["_tags"]).lower())
    )

    lines = []
    w = lines.append
    w("# All-only 292 ground-truth analysis")
    w("")
    w("Complete manual review joined to `validation/all-only-full.csv`.")
    w("**No production changes. No detector rerun.**")
    w("")
    w("Join: `TaskName` = `candidate_id` — **292 / 292**.")
    w("")
    w("Positive = Fixed + Already_Fixed. Fixed without comment counts as positive.")
    w("")
    w("## Review status counts")
    w("")
    w("| Status | n | share |")
    w("|---|---:|---:|")
    for st in STATUSES:
        n = status_counts.get(st, 0)
        w(f"| {st} | {n} | {100*n/292:.1f}% |")
    w(f"| **Positive (Fixed + Already_Fixed)** | **{status_counts['Fixed'] + status_counts['Already_Fixed']}** | **{100*(status_counts['Fixed']+status_counts['Already_Fixed'])/292:.1f}%** |")
    w("")
    w("## NAI comment / tag themes (n=67)")
    w("")
    w("| Theme | n | notes |")
    w("|---|---:|---|")
    for theme, n in themes.most_common():
        note = ""
        if theme == "golf":
            note = f"{golf_comment_nai} mention golf in comment/tag"
        w(f"| {theme} | {n} | {note} |")
    w("")
    w(f"Golf mentioned in NAI comments/tags: **{golf_comment_nai}** / 67.")
    w("")

    # Golf by status - raw containment stats
    w("## leisure=golf_course diagnostics by review status")
    w("")
    w("Golf metrics recomputed per candidate against OSM `leisure=golf_course` polygons")
    w("(not only primary `open_area_class`). Component geometry from cached All tiles @ threshold 100.")
    w("")
    w("| Status | n | center_inside | inside_frac≥0.5 | inside_frac≥0.9 | stays_inside | crosses |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    for st in STATUSES:
        sub = [r for r in rows if r["_status"] == st]
        if not sub:
            continue
        g = [r["_golf_pred"] for r in sub]
        w(
            f"| {st} | {len(sub)} | "
            f"{sum(1 for x in g if x['center_inside'])} | "
            f"{sum(1 for x in g if x['inside_frac'] is not None and x['inside_frac']>=0.5)} | "
            f"{sum(1 for x in g if x['inside_frac'] is not None and x['inside_frac']>=0.9)} | "
            f"{sum(1 for x in g if x['stays_inside'])} | "
            f"{sum(1 for x in g if x['crosses'])} |"
        )
    w("")

    # Predicate table
    w("## Golf suppression predicate evaluation")
    w("")
    w("If predicate were a suppressor: **removed** = all statuses that match.")
    w("**Precision** = NAI removed / (NAI + positive + Too_Hard + Skipped removed).")
    w("**Recall (golf NAI)** = golf-comment NAI matched / golf-comment NAI total.")
    w("")
    w("| Predicate | Fixed hit | AF hit | NAI hit | TH hit | Skip hit | Precision | Golf-NAI recall |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|")

    promising = []
    all_stats = []
    for label, fn in golf_predicates():
        st = predicate_stats(rows, fn, label)
        all_stats.append(st)
        bs = st["by_status"]
        prec = f"{100*st['precision']:.1f}%" if st["precision"] is not None else "—"
        rec = f"{100*st['recall_golf_nai']:.1f}%" if st["recall_golf_nai"] is not None else "—"
        w(
            f"| {label} | {bs['Fixed']['hit']}/{bs['Fixed']['total']} | "
            f"{bs['Already_Fixed']['hit']}/{bs['Already_Fixed']['total']} | "
            f"{bs['Not_An_Issue']['hit']}/{bs['Not_An_Issue']['total']} | "
            f"{bs['Too_Hard']['hit']}/{bs['Too_Hard']['total']} | "
            f"{bs['Skipped']['hit']}/{bs['Skipped']['total']} | {prec} | {rec} |"
        )
        if st["pos_hit"] > 0 or (st["recall_golf_nai"] or 0) >= 0.5:
            promising.append(st)
    w("")

    # Counterexamples for promising predicates (any with pos_hit > 0)
    w("## Positive counterexamples (Fixed + Already_Fixed matched by golf predicates)")
    w("")
    seen_ids = set()
    for st in sorted(all_stats, key=lambda x: (-x["pos_hit"], x["label"])):
        if not st["pos_removed"]:
            continue
        w(f"### `{st['label']}` — {st['pos_hit']} positive(s) would be removed")
        w("")
        lines.extend(counterexample_table(st["pos_removed"]))
        w("")
        for r in st["pos_removed"]:
            seen_ids.add(r["_id"])

    if not seen_ids:
        w("No Fixed/Already_Fixed matched by listed golf predicates.")
        w("")

    # Semantic section
    w("## Semantic geometry: internal vs crossing")
    w("")
    internal = [r for r in rows if r["_golf_pred"]["stays_inside"]]
    crossing = [r for r in rows if r["_golf_pred"]["crosses"]]
    center_not_stay = [
        r for r in rows
        if r["_golf_pred"]["center_inside"] and not r["_golf_pred"]["stays_inside"]
    ]
    w(f"- **stays_inside** (center + ≥90% inside, no boundary cross): n={len(internal)}")
    w(f"- **crosses** golf boundary (partial inside): n={len(crossing)}")
    w(f"- **center_inside but not stays_inside**: n={len(center_not_stay)}")
    w("")
    w("| Group | Fixed+AF | NAI | Too_Hard |")
    w("|---|---:|---:|---:|")
    for name, subset in (
        ("stays_inside", internal),
        ("crosses", crossing),
        ("center_inside, not stays", center_not_stay),
    ):
        w(
            f"| {name} | "
            f"{sum(1 for r in subset if r['_status'] in POSITIVE)} | "
            f"{sum(1 for r in subset if r['_status']=='Not_An_Issue')} | "
            f"{sum(1 for r in subset if r['_status']=='Too_Hard')} |"
        )
    w("")
    w("Interpretation: NAI golf comments cluster on **stays_inside** and high inside_frac.")
    w("Fixed tasks that touch golf often **cross** the polygon with low inside_frac")
    w("(useful path through/along edge) — suppressing `crosses` alone would be unsafe.")
    w("")

    # Threshold / geometry metrics
    w("## Threshold sensitivity (offline proxy @ threshold=100)")
    w("")
    w("**Limitation:** diagnostics are from threshold=100, minsize=20. Raising threshold")
    w("would change which pixels form each component; counts below are **proxies** only.")
    w("Do **not** rerun detection from this note alone.")
    w("")
    lines.extend(threshold_proxy_analysis(rows))

    w("## Fixed vs NAI diagnostic distributions")
    w("")
    lines.extend(metric_compare(rows, [
        "strava_mean", "strava_p90", "strava_max", "component_pixels",
        "nearest_osm_distance_m", "between_heat_ratio", "heat_halo_score",
        "osm_follow_fraction_100m", "osm_parallel_fraction_15deg",
    ]))
    w("")

    w("## Recommendations (no implementation)")
    w("")
    w("### 1. Threshold")
    w("")
    w("Keep **threshold=100** for All-only production experiments. Proxy cuts at 110–140")
    w("remove a **minority** of positives relative to NAI; NAI medians are not dramatically")
    w("cooler than Fixed+AF on mean/p90/max. Threshold alone does **not** isolate golf NAI.")
    w("")
    w("### 2. Offset")
    w("")
    w("No evidence from this review that tile **offset** retuning is needed (not measured")
    w("here). Spatial matching used 25 m center coordinates; offset affects tile fetch only.")
    w("")
    w("### 3. Golf suppression")
    w("")
    w("**Do not remove golf-course heat from the All layer globally.** See predicate")
    w("table and positive counterexample sections above.")
    w("")
    w("Best containment-style predicate: `stays_inside golf_course` removes 35/67 NAI")
    w("(76% golf-comment recall) but **1/219 Fixed** (`Golf de Son Gual`).")
    w("`has_nearby golf` removes 35 positives — unusable.")
    w("")
    w("Optional semantic suppressor remains research-only; not safe to ship without")
    w("accepting positive loss or review exceptions.")
    w("")
    w("### 4. Beach / open-area suppression")
    w("")
    w("Beach/sand NAI: 4/67 themed comments. No suppressor recommended yet.")
    w("")
    w("### 5. Distance / halo / parallel")
    w("")
    w("NAI median nearest OSM distance is **closer** than Fixed+AF (49 m vs 55 m).")
    w("No evidence to retune distance, offset, or halo thresholds from this review.")
    w("")
    w("## Reproduce")
    w("")
    w("```")
    w("python validation/analyze_all_292_ground_truth.py")
    w("```")
    w("")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_MD}", flush=True)

    # Console summary
    for st in all_stats:
        if st["label"] == "stays_inside golf_course":
            print(
                st["label"],
                "NAI", st["nai_hit"], "pos", st["pos_hit"],
                "golfNAIrecall", st["recall_golf_nai"],
            )


if __name__ == "__main__":
    main()
