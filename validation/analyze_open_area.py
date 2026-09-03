#!/usr/bin/env python3
"""Offline open-area diagnostics vs completed Mallorca heatmap reviews.

Diagnostic analysis only. Does not change detection, masking, thresholds, or
suppression. Writes validation/open-area-analysis.md.
"""
from __future__ import annotations

import csv
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import LineString, Point, Polygon, box as shapely_box
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diagnostics import (  # noqa: E402
    OPEN_AREA_CLASSES,
    classify_open_area_tags,
    evaluate_open_area_class_predicates,
    extract_component_pixels,
    open_area_metrics_for_candidate,
    osm_open_area_tags_match,
    pixels_to_mercator,
)

RADIUS = 6378137.0
THRESHOLD = 100
OSM_PATH = ROOT / "osm-data" / "mallorca" / "current.osm"
OUT_MD = ROOT / "validation" / "open-area-analysis.md"

POOL_NOTES = {
    "sport_Run": "validation/run-sample.csv + challenge_56716-RUN_tasks.csv",
    "all": "validation/all-only-sample.csv + challenge_56718-ALL_tasks.csv",
}


def lat2y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * RADIUS


def lon2x(lon):
    return math.radians(lon) * RADIUS


def num2deg(x, y, zoom):
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def get_merc_bbox(x, y, zoom):
    lat_ul, lon_ul = num2deg(x, y, zoom)
    lat_lr, lon_lr = num2deg(x + 1, y + 1, zoom)
    return lat2y(lat_ul), lon2x(lon_ul), lat2y(lat_lr), lon2x(lon_lr)


class AreaItem:
    __slots__ = ("source", "osm_id", "fill_polygon", "coords", "envelope", "tags", "cls")

    def __init__(self, source, osm_id, fill_polygon, coords, tags, cls):
        self.source = source
        self.osm_id = osm_id
        self.fill_polygon = fill_polygon
        self.coords = coords
        self.tags = tags
        self.cls = cls
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        self.envelope = shapely_box(min(xs), min(ys), max(xs), max(ys))


class AreaLookup:
    def __init__(self, items):
        self._items = []
        self._geoms = []
        self._classes = []
        for item in items:
            geom = _geom(item)
            if geom is None or geom.is_empty:
                continue
            self._items.append(item)
            self._geoms.append(geom)
            self._classes.append(item.cls)
        self._tree = STRtree(self._geoms) if self._geoms else None

    def __len__(self):
        return len(self._items)

    @property
    def _items_public(self):
        return self._items

    def candidates_near(self, merc_x, merc_y, padding=250.0):
        if self._tree is None:
            return []
        env = shapely_box(merc_x - padding, merc_y - padding, merc_x + padding, merc_y + padding)
        idxs = np.asarray(self._tree.query(env, predicate="intersects")).reshape(-1)
        return [
            (self._items[int(i)], self._geoms[int(i)], self._classes[int(i)])
            for i in idxs
        ]

    # Compatibility with diagnostics.open_area_metrics_for_candidate
    @property
    def items_compat(self):
        return self


def _geom(item):
    coords = item.coords
    if not coords:
        return None
    if len(coords) == 1:
        return Point(coords[0])
    if item.fill_polygon and len(coords) >= 4:
        try:
            poly = Polygon(coords)
            if poly.is_valid and not poly.is_empty:
                return poly
        except Exception:
            pass
    try:
        return LineString(coords)
    except Exception:
        return Point(coords[0])


# Monkey-patch OpenAreaLookup interface expected by open_area_metrics_for_candidate
class OpenAreaLookupAdapter:
    def __init__(self, area_lookup: AreaLookup):
        self._items = area_lookup._items
        self._geoms = area_lookup._geoms
        self._classes = area_lookup._classes
        self._tree = area_lookup._tree

    def __len__(self):
        return len(self._items)

    def candidates_near(self, merc_x, merc_y, padding=250.0):
        if self._tree is None:
            return []
        env = shapely_box(merc_x - padding, merc_y - padding, merc_x + padding, merc_y + padding)
        idxs = np.asarray(self._tree.query(env, predicate="intersects")).reshape(-1)
        return [
            (self._items[int(i)], self._geoms[int(i)], self._classes[int(i)])
            for i in idxs
        ]


def _merge_outers(member_coords, relation_id):
    polygons = []
    leftover = [c for c in member_coords if c]
    while leftover:
        coords = leftover.pop(0)
        finished = False
        while not finished:
            for coord in leftover:
                if coords[-1] == coord[0]:
                    leftover.remove(coord)
                    coords = coords + coord
                    break
                if coords[-1] == coord[-1]:
                    leftover.remove(coord)
                    coord.reverse()
                    coords = coords + coord
                    break
            else:
                finished = True
        if coords[0] != coords[-1]:
            continue
        polygons.append(coords)
    return polygons


def load_open_areas(osm_path: Path):
    print(f"Loading open areas from {osm_path} ...", flush=True)
    member_way_ids = set()
    relations = []
    for _e, elem in ET.iterparse(osm_path, events=("end",)):
        if elem.tag != "relation":
            if elem.tag in ("node", "way"):
                elem.clear()
            continue
        tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
        if not osm_open_area_tags_match(tags):
            elem.clear()
            continue
        osm_id = elem.attrib.get("id", "")
        members = []
        for member in elem.findall("member"):
            if member.attrib.get("type") != "way":
                continue
            ref = member.attrib.get("ref", "")
            members.append((ref, member.attrib.get("role", "")))
            member_way_ids.add(ref)
        relations.append((osm_id, tags, members))
        elem.clear()

    needed_nodes = set()
    open_ways = {}
    member_refs = {}
    for _e, elem in ET.iterparse(osm_path, events=("end",)):
        if elem.tag != "way":
            if elem.tag == "node":
                elem.clear()
            continue
        osm_id = elem.attrib.get("id", "")
        tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
        refs = [nd.get("ref") for nd in elem.findall("nd")]
        is_open = osm_open_area_tags_match(tags)
        if is_open or osm_id in member_way_ids:
            needed_nodes.update(refs)
        if is_open:
            open_ways[osm_id] = (tags, refs)
        elif osm_id in member_way_ids:
            member_refs[osm_id] = refs
        elem.clear()

    nodes = {}
    for _e, elem in ET.iterparse(osm_path, events=("end",)):
        if elem.tag != "node":
            continue
        osm_id = elem.attrib.get("id", "")
        if osm_id not in needed_nodes:
            elem.clear()
            continue
        lon = elem.attrib.get("lon")
        lat = elem.attrib.get("lat")
        if lon is None or lat is None:
            elem.clear()
            continue
        nodes[osm_id] = (lon2x(float(lon)), lat2y(float(lat)))
        elem.clear()

    def refs_to_coords(refs):
        return [nodes[r] for r in refs if r in nodes]

    way_geom = {}
    items = []
    class_counts = Counter()
    for osm_id, (tags, refs) in open_ways.items():
        coords = refs_to_coords(refs)
        way_geom[osm_id] = coords
        if len(coords) < 2:
            continue
        cls = classify_open_area_tags(tags)
        fill = len(coords) >= 4 and coords[0] == coords[-1]
        items.append(AreaItem("way", osm_id, fill, coords, tags, cls))
        class_counts[cls] += 1
    for osm_id, refs in member_refs.items():
        way_geom[osm_id] = refs_to_coords(refs)

    for rel_id, tags, members in relations:
        outers = []
        for ref, role in members:
            coords = way_geom.get(ref)
            if not coords:
                continue
            if role == "outer" or role == "":
                outers.append(coords)
        if not outers:
            continue
        cls = classify_open_area_tags(tags)
        for ring in _merge_outers(outers, rel_id):
            if len(ring) < 4:
                continue
            items.append(AreaItem("relation", rel_id, True, ring, tags, cls))
            class_counts[cls] += 1

    print(f"Open-area drawing objects: {len(items)}", flush=True)
    for cls in OPEN_AREA_CLASSES:
        print(f"  {cls}: {class_counts.get(cls, 0)}", flush=True)
    return OpenAreaLookupAdapter(AreaLookup(items)), class_counts, items


def load_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def norm_status(s):
    return (s or "").strip().replace(" ", "_")


def join_sample(sample_path, mr_path):
    sample = load_csv(sample_path)
    mr = {r["TaskName"].strip(): r for r in load_csv(mr_path)}
    joined = []
    for s in sample:
        m = mr[s["candidate_id"]]
        row = dict(s)
        row["_status"] = norm_status(m.get("TaskStatus"))
        row["_comments"] = (m.get("Comments") or "").strip()
        row["_tags"] = (m.get("Tags") or "").strip()
        parts = s["candidate_id"].split("/")
        row["_z"] = int(parts[0])
        row["_x"] = int(parts[1])
        row["_y"] = int(parts[2])
        row["_peak_row"] = int(parts[3])
        row["_peak_col"] = int(parts[4])
        row["_lon"] = float(s["lon"])
        row["_lat"] = float(s["lat"])
        joined.append(row)
    return joined


def tile_path(layer, z, x, y):
    if layer == "sport_Run":
        stem = "run"
    else:
        stem = "all"
    return ROOT / "cache" / "strava" / stem / "strava" / str(z) / str(x) / f"{y}.png"


def component_merc_for(row, layer):
    path = tile_path(layer, row["_z"], row["_x"], row["_y"])
    if not path.is_file():
        return None, "missing_tile"
    try:
        with Image.open(path) as im:
            if im.mode != "L":
                im = im.convert("L")
            data = np.array(im)
            size = im.size[0]
    except Exception as exc:
        return None, f"tile_error:{exc}"
    values, rows, cols = extract_component_pixels(
        data, row["_peak_row"], row["_peak_col"], THRESHOLD
    )
    if values.size == 0:
        return None, "empty_component"
    bbox = get_merc_bbox(row["_x"], row["_y"], row["_z"])
    pixel_size = (bbox[0] - bbox[2]) / size
    return pixels_to_mercator(rows, cols, bbox, pixel_size), None


def evaluate_class_predicates(merc_x, merc_y, component_xy, lookup, cls):
    """Same containment predicates as production diagnostics / --suppress-golf."""
    return evaluate_open_area_class_predicates(
        merc_x, merc_y, component_xy, lookup, cls
    )


def annotate(rows, layer, lookup):
    for row in rows:
        mx, my = lon2x(row["_lon"]), lat2y(row["_lat"])
        comp, err = component_merc_for(row, layer)
        row["_comp_err"] = err
        row["_metrics"] = open_area_metrics_for_candidate(mx, my, comp, lookup)
        row["_by_class"] = {
            cls: evaluate_class_predicates(mx, my, comp, lookup, cls)
            for cls in OPEN_AREA_CLASSES
        }
        # Convenience primary
        row["_primary_class"] = row["_metrics"].get("open_area_class") or ""
        row["_center_inside"] = (row["_metrics"].get("open_area_center_inside") or "") == "true"
        frac = row["_metrics"].get("open_area_component_inside_frac") or ""
        row["_inside_frac"] = float(frac) if frac != "" else None
        row["_crosses"] = (row["_metrics"].get("open_area_component_crosses_boundary") or "") == "true"
        row["_stays"] = (row["_metrics"].get("open_area_component_stays_inside") or "") == "true"
        dist = row["_metrics"].get("open_area_distance_m") or ""
        row["_dist"] = float(dist) if dist != "" else None


def status_bucket(status, layer):
    if layer == "sport_Run":
        if status == "Fixed":
            return "Fixed"
        if status == "Too_Hard":
            return "Too_Hard"
        if status == "Not_An_Issue":
            return "Not_An_Issue"
        return status
    # All-only
    if status in ("Fixed", "Already_Fixed", "Too_Hard", "Not_An_Issue"):
        return status
    return status


def hit_table(rows, layer, predicate_fn, label):
    if layer == "sport_Run":
        keys = ["Fixed", "Too_Hard", "Not_An_Issue"]
    else:
        keys = ["Fixed", "Already_Fixed", "Too_Hard", "Not_An_Issue"]
    counts = Counter()
    hits = Counter()
    examples = defaultdict(list)
    for row in rows:
        bucket = status_bucket(row["_status"], layer)
        if bucket not in keys:
            continue
        counts[bucket] += 1
        if predicate_fn(row):
            hits[bucket] += 1
            if len(examples[bucket]) < 8:
                examples[bucket].append(row)
    return keys, counts, hits, examples


def fmt_hits(keys, counts, hits):
    parts = []
    for k in keys:
        parts.append(f"{k} {hits.get(k, 0)}/{counts.get(k, 0)}")
    return ", ".join(parts)


def main():
    lookup, class_counts, _items = load_open_areas(OSM_PATH)

    run = join_sample(
        ROOT / "validation" / "run-sample.csv",
        ROOT / "validation" / "challenge_56716-RUN_tasks.csv",
    )
    alll = join_sample(
        ROOT / "validation" / "all-only-sample.csv",
        ROOT / "validation" / "challenge_56718-ALL_tasks.csv",
    )
    print(f"Joined RUN {len(run)} ALL {len(alll)}", flush=True)
    annotate(run, "sport_Run", lookup)
    annotate(alll, "all", lookup)
    print(
        "Component extract errors RUN",
        Counter(r["_comp_err"] for r in run if r["_comp_err"]),
        "ALL",
        Counter(r["_comp_err"] for r in alll if r["_comp_err"]),
        flush=True,
    )

    lines = []
    def w(s=""):
        lines.append(s)

    w("# Open-area diagnostic analysis (Mallorca review samples)")
    w()
    w("Diagnostic-only. **No suppression rule implemented.** Detector thresholds")
    w("and existing suppressors (parallel / ferry / heat-halo) are unchanged.")
    w()
    w("Open-area polygons/lines are loaded from `osm-data/mallorca/current.osm`")
    w("separately from the OSM mask universe. Component geometry is reconstructed")
    w("from cached Strava tiles (unmasked) at threshold 100 using the candidate peak.")
    w()
    w("Joins: `TaskName` = `candidate_id` — RUN 50/50, ALL-only 50/50.")
    w()
    w("## Open-area objects loaded")
    w()
    w("| Class | Drawing objects |")
    w("|---|---:|")
    for cls in OPEN_AREA_CLASSES:
        w(f"| {cls} | {class_counts.get(cls, 0)} |")
    w(f"| **total objects** | **{sum(class_counts.values())}** |")
    w()

    # Known NAI recovery
    w("## Known All-only NAI recovery")
    w()
    known = {
        "golf": [r for r in alll if "golf" in (r["_comments"] + r["_tags"]).lower()],
        "beach": [r for r in alll if "beach" in (r["_comments"] + r["_tags"]).lower() or "sand" in (r["_comments"]).lower()],
        "climbing": [r for r in alll if "climbing" in (r["_comments"] + r["_tags"]).lower()],
    }
    w(f"- Golf NAI in review comments: **{len(known['golf'])}** (expect 5)")
    w(f"- Beach/sand NAI in review comments: **{len(known['beach'])}** (expect 3)")
    w(f"- Climbing NAI in review comments: **{len(known['climbing'])}** (expect 1)")
    w()
    for theme, subset in known.items():
        w(f"### {theme}")
        w()
        w("| candidate_id | status | primary_class | center_inside | inside_frac | crosses | stays | dist_m | class-specific |")
        w("|---|---|---|---:|---:|---:|---:|---:|---|")
        for r in subset:
            target = {
                "golf": "golf_course",
                "beach": "beach",
                "climbing": "sport_climbing",
            }[theme]
            pred = r["_by_class"][target]
            # beach theme also checks sand
            if theme == "beach" and not pred["center_inside"]:
                sand = r["_by_class"]["sand"]
                if sand["center_inside"] or (sand["inside_frac"] or 0) > (pred["inside_frac"] or 0):
                    pred = sand
                    target = "sand"
            w(
                f"| `{r['candidate_id']}` | {r['_status']} | {r['_primary_class'] or '—'} | "
                f"{r['_center_inside']} | {r['_inside_frac'] if r['_inside_frac'] is not None else '—'} | "
                f"{r['_crosses']} | {r['_stays']} | "
                f"{'' if r['_dist'] is None else f'{r['_dist']:.1f}'} | "
                f"{target}: inside={pred['center_inside']} frac={pred['inside_frac']} crosses={pred['crosses']} |"
            )
        w()

    predicates = [
        ("center_inside any listed class", lambda r: any(r["_by_class"][c]["center_inside"] for c in OPEN_AREA_CLASSES)),
        ("majority component inside any", lambda r: any(r["_by_class"][c]["majority_inside"] for c in OPEN_AREA_CLASSES)),
        ("near-entire (>=0.9) inside any", lambda r: any(r["_by_class"][c]["near_entire_inside"] for c in OPEN_AREA_CLASSES)),
        ("entire (>=0.99) inside any", lambda r: any(r["_by_class"][c]["entire_inside"] for c in OPEN_AREA_CLASSES)),
        ("stays_inside any (center + >=0.9 + not crosses)", lambda r: any(r["_by_class"][c]["stays_inside"] for c in OPEN_AREA_CLASSES)),
        ("center_inside AND crosses any", lambda r: any(r["_by_class"][c]["center_inside"] and r["_by_class"][c]["crosses"] for c in OPEN_AREA_CLASSES)),
        ("center_inside golf_course", lambda r: r["_by_class"]["golf_course"]["center_inside"]),
        ("stays_inside golf_course", lambda r: r["_by_class"]["golf_course"]["stays_inside"]),
        ("majority inside golf_course", lambda r: r["_by_class"]["golf_course"]["majority_inside"]),
        ("center_inside beach", lambda r: r["_by_class"]["beach"]["center_inside"]),
        ("stays_inside beach", lambda r: r["_by_class"]["beach"]["stays_inside"]),
        ("majority inside beach OR sand", lambda r: r["_by_class"]["beach"]["majority_inside"] or r["_by_class"]["sand"]["majority_inside"]),
        ("center_inside beach OR sand", lambda r: r["_by_class"]["beach"]["center_inside"] or r["_by_class"]["sand"]["center_inside"]),
        ("stays_inside beach OR sand", lambda r: r["_by_class"]["beach"]["stays_inside"] or r["_by_class"]["sand"]["stays_inside"]),
        ("center_inside sport_climbing", lambda r: r["_by_class"]["sport_climbing"]["center_inside"]),
        ("stays_inside sport_climbing", lambda r: r["_by_class"]["sport_climbing"]["stays_inside"]),
        ("center_inside pitch", lambda r: r["_by_class"]["pitch"]["center_inside"]),
        ("center_inside sports_centre", lambda r: r["_by_class"]["sports_centre"]["center_inside"]),
        ("center_inside parking", lambda r: r["_by_class"]["parking"]["center_inside"]),
        ("center_inside pedestrian_area", lambda r: r["_by_class"]["pedestrian_area"]["center_inside"]),
        ("center_inside park OR playground", lambda r: r["_by_class"]["park"]["center_inside"] or r["_by_class"]["playground"]["center_inside"]),
        (
            "stays_inside golf OR beach OR sand OR climbing",
            lambda r: any(
                r["_by_class"][c]["stays_inside"]
                for c in ("golf_course", "beach", "sand", "sport_climbing")
            ),
        ),
        (
            "center_inside golf OR beach OR sand OR climbing",
            lambda r: any(
                r["_by_class"][c]["center_inside"]
                for c in ("golf_course", "beach", "sand", "sport_climbing")
            ),
        ),
        (
            "majority inside golf OR beach OR sand OR climbing",
            lambda r: any(
                r["_by_class"][c]["majority_inside"]
                for c in ("golf_course", "beach", "sand", "sport_climbing")
            ),
        ),
    ]

    for layer_name, rows in (("ALL-ONLY", alll), ("SPORT_RUN", run)):
        w(f"## {layer_name} predicate hits")
        w()
        w("Counts are **hits / status-total** in the 50-task sample.")
        w()
        if layer_name == "ALL-ONLY":
            w("| Predicate | Fixed | Already_Fixed | Too_Hard | Not_An_Issue |")
            w("|---|---:|---:|---:|---:|")
            keys = ["Fixed", "Already_Fixed", "Too_Hard", "Not_An_Issue"]
        else:
            w("| Predicate | Fixed | Too_Hard | Not_An_Issue |")
            w("|---|---:|---:|---:|")
            keys = ["Fixed", "Too_Hard", "Not_An_Issue"]
        for label, fn in predicates:
            _k, counts, hits, _ex = hit_table(rows, "all" if layer_name == "ALL-ONLY" else "sport_Run", fn, label)
            cells = " | ".join(f"{hits.get(k, 0)}/{counts.get(k, 0)}" for k in keys)
            w(f"| {label} | {cells} |")
        w()

    # Fixed candidates inside same area types (critical)
    w("## Valid Fixed candidates inside / crossing the same area types")
    w()
    w("Critical check: a real path through a golf course or beach must not be")
    w("treated as an automatic false positive.")
    w()
    for layer_name, rows in (("ALL-ONLY", alll), ("SPORT_RUN", run)):
        w(f"### {layer_name} Fixed with center_inside golf/beach/sand/climbing")
        w()
        fixed_inside = [
            r for r in rows
            if r["_status"] in ("Fixed", "Already_Fixed")
            and any(
                r["_by_class"][c]["center_inside"]
                for c in ("golf_course", "beach", "sand", "sport_climbing")
            )
        ]
        if not fixed_inside:
            w("None.")
        else:
            w("| candidate_id | class | inside_frac | crosses | stays | name | comment/tags |")
            w("|---|---|---:|---:|---:|---|---|")
            for r in fixed_inside:
                for c in ("golf_course", "beach", "sand", "sport_climbing"):
                    p = r["_by_class"][c]
                    if not p["center_inside"]:
                        continue
                    w(
                        f"| `{r['candidate_id']}` | {c} | {p['inside_frac']} | {p['crosses']} | "
                        f"{p['stays_inside']} | {p['name'] or '—'} | "
                        f"{(r['_tags'] + ' ' + r['_comments']).strip() or '—'} |"
                    )
        w()
        w(f"### {layer_name} Fixed that **cross** golf/beach/sand/climbing (center inside or not)")
        w()
        fixed_cross = [
            r for r in rows
            if r["_status"] in ("Fixed", "Already_Fixed")
            and any(
                r["_by_class"][c]["crosses"]
                for c in ("golf_course", "beach", "sand", "sport_climbing")
            )
        ]
        if not fixed_cross:
            w("None.")
        else:
            w("| candidate_id | classes crossing | inside_frac (primary hit) |")
            w("|---|---|---|")
            for r in fixed_cross:
                parts = []
                for c in ("golf_course", "beach", "sand", "sport_climbing"):
                    p = r["_by_class"][c]
                    if p["crosses"]:
                        parts.append(f"{c} frac={p['inside_frac']} center={p['center_inside']}")
                w(f"| `{r['candidate_id']}` | {'; '.join(parts)} |")
        w()

    # Summary judgment
    def summarize(rows, layer):
        keys = ["Fixed", "Already_Fixed", "Too_Hard", "Not_An_Issue"] if layer == "all" else ["Fixed", "Too_Hard", "Not_An_Issue"]
        focus = ("golf_course", "beach", "sand", "sport_climbing")

        def pred_stays(r):
            return any(r["_by_class"][c]["stays_inside"] for c in focus)

        def pred_center(r):
            return any(r["_by_class"][c]["center_inside"] for c in focus)

        def pred_majority(r):
            return any(r["_by_class"][c]["majority_inside"] for c in focus)

        out = {}
        for name, fn in (("stays", pred_stays), ("center", pred_center), ("majority", pred_majority)):
            _k, counts, hits, _e = hit_table(rows, layer, fn, name)
            out[name] = {k: (hits.get(k, 0), counts.get(k, 0)) for k in keys}
        return out

    all_sum = summarize(alll, "all")
    run_sum = summarize(run, "sport_Run")

    w("## Can we catch known NAI without Fixed loss?")
    w()
    w("Focus classes: `golf_course`, `beach`, `sand`, `sport_climbing`.")
    w()
    w("### ALL-ONLY")
    w()
    for name in ("center", "majority", "stays"):
        h = all_sum[name]
        w(
            f"- **{name}_inside focus classes**: "
            f"Fixed {h['Fixed'][0]}/{h['Fixed'][1]}, "
            f"Already_Fixed {h['Already_Fixed'][0]}/{h['Already_Fixed'][1]}, "
            f"Too_Hard {h['Too_Hard'][0]}/{h['Too_Hard'][1]}, "
            f"Not_An_Issue {h['Not_An_Issue'][0]}/{h['Not_An_Issue'][1]}"
        )
    w()
    w("### SPORT_RUN")
    w()
    for name in ("center", "majority", "stays"):
        h = run_sum[name]
        w(
            f"- **{name}_inside focus classes**: "
            f"Fixed {h['Fixed'][0]}/{h['Fixed'][1]}, "
            f"Too_Hard {h['Too_Hard'][0]}/{h['Too_Hard'][1]}, "
            f"Not_An_Issue {h['Not_An_Issue'][0]}/{h['Not_An_Issue'][1]}"
        )
    w()

    # Per known NAI catch with stays vs center
    def caught(subset, classes, mode):
        n = 0
        for r in subset:
            ok = False
            for c in classes:
                p = r["_by_class"][c]
                if mode == "center" and p["center_inside"]:
                    ok = True
                if mode == "majority" and p["majority_inside"]:
                    ok = True
                if mode == "stays" and p["stays_inside"]:
                    ok = True
            n += int(ok)
        return n

    w("### Known thematic NAI catch rate (All-only)")
    w()
    w("| Theme | n | center_inside | majority_inside | stays_inside |")
    w("|---|---:|---:|---:|---:|")
    w(
        f"| golf | {len(known['golf'])} | "
        f"{caught(known['golf'], ['golf_course'], 'center')} | "
        f"{caught(known['golf'], ['golf_course'], 'majority')} | "
        f"{caught(known['golf'], ['golf_course'], 'stays')} |"
    )
    w(
        f"| beach/sand | {len(known['beach'])} | "
        f"{caught(known['beach'], ['beach', 'sand'], 'center')} | "
        f"{caught(known['beach'], ['beach', 'sand'], 'majority')} | "
        f"{caught(known['beach'], ['beach', 'sand'], 'stays')} |"
    )
    w(
        f"| climbing | {len(known['climbing'])} | "
        f"{caught(known['climbing'], ['sport_climbing'], 'center')} | "
        f"{caught(known['climbing'], ['sport_climbing'], 'majority')} | "
        f"{caught(known['climbing'], ['sport_climbing'], 'stays')} |"
    )
    w()

    w("## Interpretation")
    w()
    w("- Open-area context is now available as diagnostic fields on future")
    w("  `--diagnostics` runs (`open_area_*` columns). These fields are **not**")
    w("  wired into any suppressor.")
    w("- Prefer predicates that distinguish **activity contained in an area**")
    w("  (`stays_inside` / near-entire component inside) from **ways that cross**")
    w("  an area (center inside but crosses boundary, or low inside_frac).")
    w("- If `stays_inside` on golf/beach/sand/climbing catches All-only NAI while")
    w("  sparing Fixed that merely cross those areas, that is the promising")
    w("  direction for a *future* optional suppressor — not implemented here.")
    w("- Pitch / parking / pedestrian_area / park hits on Fixed are expected and")
    w("  must not be blanket-suppressed.")
    w()
    w("## Reproduce")
    w()
    w("```")
    w("python validation/analyze_open_area.py")
    w("```")
    w()

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_MD}", flush=True)

    # Console summary
    print("\nALL-ONLY stays focus:", all_sum["stays"])
    print("ALL-ONLY center focus:", all_sum["center"])
    print("RUN stays focus:", run_sum["stays"])
    print("RUN center focus:", run_sum["center"])
    print(
        "Known catch stays golf/beach/climb:",
        caught(known["golf"], ["golf_course"], "stays"),
        caught(known["beach"], ["beach", "sand"], "stays"),
        caught(known["climbing"], ["sport_climbing"], "stays"),
    )
    print(
        "Known catch center golf/beach/climb:",
        caught(known["golf"], ["golf_course"], "center"),
        caught(known["beach"], ["beach", "sand"], "center"),
        caught(known["climbing"], ["sport_climbing"], "center"),
    )


if __name__ == "__main__":
    main()
