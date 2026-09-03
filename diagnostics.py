# Component geometry metrics for osm-strava diagnostics and opt-in suppression.
# Detection, masking, and GeoJSON geometry are owned by strava.py.
import csv
import json
import math
from collections import deque

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box as shapely_box
from shapely.strtree import STRtree

# Diagnostic-only neighbourhood around a component (Web Mercator metres).
_OSM_NEIGHBOURHOOD_M = 100.0
_LATERAL_OSM_SEARCH_M = 250.0
_LATERAL_PARALLEL_DEG = 15.0
_LATERAL_PARALLEL_WIDE_DEG = 30.0
_LATERAL_COVER_BANDS_M = (50.0, 75.0, 100.0, 150.0, 200.0, 250.0)
_LATERAL_MAX_GEOMS_PER_SAMPLE = 32
_LATERAL_HEAT_MAX_SAMPLES = 80
_MAX_COMPONENT_SAMPLES = 160
_TANGENT_WINDOW_M = 20.0
_LOCAL_COMPONENT_RADIUS_M = 30.0
_MIN_TANGENT_SPAN_M = 1.0
_MAX_ELONGATION = 1e6


DIAGNOSTIC_COLUMNS = [
    "candidate_id",
    "tile_z",
    "tile_x",
    "tile_y",
    "candidate_index_in_tile",
    "peak_row",
    "peak_col",
    "center_lon",
    "center_lat",
    "geometry_length_m",
    "geometry_area_m2",
    "component_pixels",
    "component_width_px",
    "component_height_px",
    "strava_min",
    "strava_max",
    "strava_mean",
    "strava_median",
    "strava_p75",
    "strava_p90",
    "strava_p95",
    "nearest_osm_distance_m",
    "nearest_osm_type",
    "nearest_osm_id",
    "nearest_osm_highway",
    "nearest_osm_railway",
    "nearest_osm_aeroway",
    "nearest_osm_leisure",
    "nearest_osm_route",
    "nearest_osm_construction",
    "nearest_osm_name",
    "nearest_osm_tags",
    "nearest_ferry_distance_m",
    "nearest_ferry_id",
    "nearest_ferry_name",
    "nearest_construction_distance_m",
    "nearest_construction_id",
    "nearest_construction_name",
    "component_orientation_deg",
    "component_elongation",
    "osm_distance_min_m",
    "osm_distance_p25_m",
    "osm_distance_median_m",
    "osm_distance_p75_m",
    "osm_distance_p90_m",
    "osm_distance_max_m",
    "osm_distance_iqr_m",
    "osm_follow_fraction_50m",
    "osm_follow_fraction_75m",
    "osm_follow_fraction_100m",
    "osm_parallel_angle_median_deg",
    "osm_parallel_fraction_15deg",
    "osm_parallel_fraction_30deg",
    "nearest_parallel_osm_distance_m",
    "nearest_parallel_osm_min_m",
    "nearest_parallel_osm_angle_deg",
    "nearest_parallel_osm_id",
    "nearest_parallel_osm_highway",
    "nearest_parallel_osm_name",
    "parallel_osm_cover_frac_50m",
    "parallel_osm_cover_frac_75m",
    "parallel_osm_cover_frac_100m",
    "parallel_osm_cover_frac_150m",
    "parallel_osm_cover_frac_200m",
    "parallel_osm_cover_frac_250m",
    "parallel_osm_cover_frac_250m_30deg",
    "candidate_axis_heat_mean",
    "candidate_axis_heat_p90",
    "candidate_axis_heat_n",
    "parallel_osm_heat_mean",
    "parallel_osm_heat_p90",
    "parallel_osm_heat_n",
    "parallel_osm_heat_ratio_mean",
    "parallel_osm_heat_ratio_p90",
    "between_heat_median",
    "between_heat_ratio",
    "heat_halo_score",
    # Diagnostic-only OSM area context (activity-on-area). Never used for suppression.
    "open_area_class",
    "open_area_osm_id",
    "open_area_osm_type",
    "open_area_name",
    "open_area_tags",
    "open_area_center_inside",
    "open_area_distance_m",
    "open_area_component_inside_frac",
    "open_area_component_crosses_boundary",
    "open_area_component_stays_inside",
    "raw_candidate",
    "too_small",
    "accepted",
    "inside_area",
    "written_to_geojson",
    "suppressed_parallel_osm",
    "suppressed_ferry",
    "suppressed_heat_halo",
]

# Diagnostic-only open-area classes. Not part of the OSM mask universe.
OPEN_AREA_CLASSES = (
    "golf_course",
    "beach",
    "sand",
    "pitch",
    "sports_centre",
    "stadium",
    "sport_climbing",
    "sport_area",
    "parking",
    "pedestrian_area",
    "park",
    "playground",
)
_OPEN_AREA_SEARCH_M = 250.0
_OPEN_AREA_CROSS_EPS = 1e-6


def _fmt_float(value, digits=6):
    if value is None or value == "":
        return ""
    return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")


def _fmt_int(value):
    if value is None or value == "":
        return ""
    return str(int(value))


def _bool_csv(value):
    return "true" if value else "false"


def extract_component_pixels(heatmap, row, col, threshold):
    """4-connected component of pixels >= threshold, same connectivity as check_trace_area.

    Operates on a copy-safe array; does not modify detection state.
    Returns (values, rows, cols) as numpy arrays, or empty arrays if the seed is cold.
    """
    height, width = heatmap.shape
    if row < 0 or col < 0 or row >= height or col >= width:
        return np.array([]), np.array([]), np.array([])
    if heatmap[row, col] < threshold:
        return np.array([]), np.array([]), np.array([])

    seen = np.zeros(heatmap.shape, dtype=bool)
    seen[row, col] = True
    queue = deque([(int(row), int(col))])
    values = []
    rows = []
    cols = []
    while queue:
        r, c = queue.popleft()
        values.append(int(heatmap[r, c]))
        rows.append(r)
        cols.append(c)
        for nr, nc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
            if nr < 0 or nc < 0 or nr >= height or nc >= width:
                continue
            if seen[nr, nc]:
                continue
            if heatmap[nr, nc] < threshold:
                continue
            seen[nr, nc] = True
            queue.append((nr, nc))
    return np.asarray(values), np.asarray(rows), np.asarray(cols)


def component_strava_stats(values):
    if values.size == 0:
        return {
            "strava_min": "",
            "strava_max": "",
            "strava_mean": "",
            "strava_median": "",
            "strava_p75": "",
            "strava_p90": "",
            "strava_p95": "",
        }
    return {
        "strava_min": _fmt_int(int(np.min(values))),
        "strava_max": _fmt_int(int(np.max(values))),
        "strava_mean": _fmt_float(float(np.mean(values)), 4),
        "strava_median": _fmt_float(float(np.median(values)), 4),
        "strava_p75": _fmt_float(float(np.percentile(values, 75)), 4),
        "strava_p90": _fmt_float(float(np.percentile(values, 90)), 4),
        "strava_p95": _fmt_float(float(np.percentile(values, 95)), 4),
    }


def _item_geometry(item):
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


def _item_tags(item):
    tags = getattr(item, "tags", None)
    if not tags:
        return {}
    return tags


def pixels_to_mercator(rows, cols, bbox_merc, pixel_size):
    """Pixel (row, col) -> Web Mercator, same as reverse_transform without lon/lat round-trip."""
    merc_x = cols.astype(np.float64) * pixel_size + bbox_merc[1]
    merc_y = bbox_merc[0] - rows.astype(np.float64) * pixel_size
    return np.column_stack((merc_x, merc_y))


def _sample_indices(n, max_samples=_MAX_COMPONENT_SAMPLES):
    if n <= max_samples:
        return np.arange(n, dtype=int)
    return np.unique(np.linspace(0, n - 1, max_samples).round().astype(int))


def _safe_elongation(lam1, lam2):
    if lam1 <= 1e-18:
        return None
    if lam2 <= 1e-18:
        return _MAX_ELONGATION
    return min(lam1 / lam2, _MAX_ELONGATION)


def component_pca(merc_xy):
    """Return (orientation_deg 0..180, elongation, axis_vector) or Nones if degenerate."""
    if merc_xy.shape[0] < 2:
        return None, None, None
    centered = merc_xy - merc_xy.mean(axis=0)
    if merc_xy.shape[0] == 2:
        delta = centered[1]
        if float(np.dot(delta, delta)) < 1e-12:
            return None, None, None
        vec = delta / np.linalg.norm(delta)
        angle = math.degrees(math.atan2(vec[1], vec[0])) % 180.0
        return angle, _MAX_ELONGATION, vec
    cov = np.cov(centered, rowvar=False)
    if cov.shape != (2, 2) or not np.all(np.isfinite(cov)):
        return None, None, None
    eigvals, eigvecs = np.linalg.eigh(cov)
    order = np.argsort(eigvals)[::-1]
    eigvals = eigvals[order]
    eigvecs = eigvecs[:, order]
    vec = eigvecs[:, 0]
    if float(np.dot(vec, vec)) < 1e-18:
        return None, None, None
    vec = vec / np.linalg.norm(vec)
    angle = math.degrees(math.atan2(vec[1], vec[0])) % 180.0
    lam1 = float(max(eigvals[0], 0.0))
    lam2 = float(max(eigvals[1], 0.0))
    elong = _safe_elongation(lam1, lam2)
    if elong is None:
        return None, None, None
    return angle, elong, vec


def _orientation_diff_deg(ax, ay, bx, by):
    """Smallest angle between two undirected axes, 0=parallel, 90=perpendicular."""
    n1 = math.hypot(ax, ay)
    n2 = math.hypot(bx, by)
    if n1 < 1e-12 or n2 < 1e-12:
        return None
    cos = abs((ax * bx + ay * by) / (n1 * n2))
    cos = min(1.0, max(0.0, cos))
    return math.degrees(math.acos(cos))


def _as_lines(geom):
    if geom is None or geom.is_empty:
        return []
    gtype = geom.geom_type
    if gtype == "LineString":
        return [geom]
    if gtype == "LinearRing":
        return [LineString(geom.coords)]
    if gtype == "Polygon":
        return [geom.exterior]
    if gtype in ("MultiLineString", "MultiPolygon", "GeometryCollection"):
        lines = []
        for part in geom.geoms:
            lines.extend(_as_lines(part))
        return lines
    return []


def local_tangent(geom, point, window_m=_TANGENT_WINDOW_M):
    lines = _as_lines(geom)
    best_line = None
    best_dist = None
    for line in lines:
        if line.length <= 0:
            continue
        dist = line.distance(point)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_line = line
    if best_line is None or best_line.length < _MIN_TANGENT_SPAN_M:
        return None
    along = best_line.project(point)
    span = min(window_m, best_line.length / 2.0)
    start = max(0.0, along - span)
    end = min(best_line.length, along + span)
    if end - start < _MIN_TANGENT_SPAN_M:
        return None
    p1 = best_line.interpolate(start)
    p2 = best_line.interpolate(end)
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    if dx * dx + dy * dy < 1e-12:
        return None
    return dx, dy


def _local_component_direction(all_xy, sample_xy, global_vec, radius=_LOCAL_COMPONENT_RADIUS_M):
    delta = all_xy - sample_xy
    nearby = all_xy[np.sum(delta * delta, axis=1) <= radius * radius]
    if nearby.shape[0] >= 5:
        _angle, _elong, vec = component_pca(nearby)
        if vec is not None:
            return vec
    return global_vec


PARALLEL_OSM_FOLLOW100_MIN = 0.70
PARALLEL_OSM_PARALLEL15_MIN = 0.70
FERRY_SUPPRESS_MAX_M = 500.0
HEAT_HALO_BETWEEN_MIN = 1.0


def _parse_metric_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def follow_metrics_from_component_pixels(values, rows, cols, bbox_merc, pixel_size, lookup):
    """PCA + local OSM follow/parallel metrics for already extracted component pixels."""
    if values.size == 0 or bbox_merc is None:
        return None, None, _empty_follow_metrics()
    merc_xy = pixels_to_mercator(rows, cols, bbox_merc, pixel_size)
    angle, elong, axis_vec = component_pca(merc_xy)
    follow = _component_osm_follow_metrics(merc_xy, axis_vec, lookup)
    return angle, elong, follow


def component_osm_follow_metrics(
    heatmap, peak_row, peak_col, threshold, bbox_merc, pixel_size, lookup
):
    """Follow/parallel metrics for the 4-connected >= threshold component.

    Same definitions as the diagnostics CSV columns osm_follow_fraction_* and
    osm_parallel_fraction_*. Used by both diagnostics export and production
    --suppress-parallel-osm.
    """
    values, rows, cols = extract_component_pixels(
        heatmap, peak_row, peak_col, threshold
    )
    _angle, _elong, follow = follow_metrics_from_component_pixels(
        values, rows, cols, bbox_merc, pixel_size, lookup
    )
    return follow


def should_suppress_parallel_osm(follow_metrics):
    """True if follow100 >= 0.70 and parallel15 >= 0.70. Blank metrics do not match."""
    follow100 = _parse_metric_float((follow_metrics or {}).get("osm_follow_fraction_100m"))
    parallel15 = _parse_metric_float((follow_metrics or {}).get("osm_parallel_fraction_15deg"))
    return (
        follow100 is not None
        and follow100 >= PARALLEL_OSM_FOLLOW100_MIN
        and parallel15 is not None
        and parallel15 >= PARALLEL_OSM_PARALLEL15_MIN
    )


def nearest_ferry_distance_m(lookup, merc_x, merc_y):
    """Nearest route=ferry distance in metres via the existing ferry STRtree."""
    if lookup is None:
        return None
    found = lookup.nearest(merc_x, merc_y, "ferry")
    if found is None:
        return None
    _item, dist = found
    return None if dist is None else float(dist)


def should_suppress_ferry(ferry_distance_m):
    """True if nearest_ferry_distance_m is populated and <= 500. Blank does not match."""
    dist = _parse_metric_float(ferry_distance_m)
    return dist is not None and dist <= FERRY_SUPPRESS_MAX_M


def lateral_metrics_from_component_pixels(
    values,
    rows,
    cols,
    bbox_merc,
    pixel_size_m,
    lookup,
    heatmap_snapshot,
    heatmap_unmasked=None,
):
    """Lateral heatmap metrics for already extracted component pixels.

    Shared by diagnostics CSV export and production --suppress-heat-halo.
    Numerical behaviour matches the previous inline build_diagnostic_row path.
    """
    if values.size == 0 or bbox_merc is None:
        return _empty_lateral_metrics()
    merc_xy = pixels_to_mercator(rows, cols, bbox_merc, pixel_size_m)
    _pca_angle, _pca_elong, axis_vec = component_pca(merc_xy)
    cand_mean = float(np.mean(values)) if values.size else None
    cand_p90 = float(np.percentile(values, 90)) if values.size else None
    heat_for_corridor = heatmap_unmasked if heatmap_unmasked is not None else heatmap_snapshot
    try:
        return component_lateral_osm_metrics(
            merc_xy,
            axis_vec,
            lookup,
            heat_for_corridor,
            bbox_merc,
            pixel_size_m,
            cand_mean,
            cand_p90,
        )
    except Exception:
        return _empty_lateral_metrics()


def component_between_heat_ratio(
    heatmap,
    heatmap_unmasked,
    peak_row,
    peak_col,
    threshold,
    bbox_merc,
    pixel_size,
    lookup,
):
    """between_heat_ratio for a peak, same definition as the diagnostics CSV."""
    values, rows, cols = extract_component_pixels(
        heatmap, peak_row, peak_col, threshold
    )
    metrics = lateral_metrics_from_component_pixels(
        values,
        rows,
        cols,
        bbox_merc,
        pixel_size,
        lookup,
        heatmap,
        heatmap_unmasked,
    )
    return metrics.get("between_heat_ratio", "")


def should_suppress_heat_halo(between_heat_ratio):
    """True if between_heat_ratio >= 1.0. Blank metrics do not match."""
    value = _parse_metric_float(between_heat_ratio)
    return value is not None and value >= HEAT_HALO_BETWEEN_MIN


def _empty_follow_metrics():
    return {
        "osm_distance_min_m": "",
        "osm_distance_p25_m": "",
        "osm_distance_median_m": "",
        "osm_distance_p75_m": "",
        "osm_distance_p90_m": "",
        "osm_distance_max_m": "",
        "osm_distance_iqr_m": "",
        "osm_follow_fraction_50m": "",
        "osm_follow_fraction_75m": "",
        "osm_follow_fraction_100m": "",
        "osm_parallel_angle_median_deg": "",
        "osm_parallel_fraction_15deg": "",
        "osm_parallel_fraction_30deg": "",
    }


def _sample_component_xy(merc_xy, axis_vec):
    n = merc_xy.shape[0]
    if axis_vec is not None:
        order = np.argsort(merc_xy @ np.asarray(axis_vec, dtype=np.float64))
        return merc_xy[order[_sample_indices(n)]]
    return merc_xy[_sample_indices(n)]


def _nearest_local_geom(local_geoms, local_tree, point):
    if not local_geoms or local_tree is None:
        return None, None
    index = local_tree.nearest(point)
    if index is None:
        return None, None
    idx = int(np.asarray(index).reshape(-1)[0])
    geom = local_geoms[idx]
    return geom, float(geom.distance(point))


def _component_osm_follow_metrics(merc_xy, axis_vec, lookup):
    empty = _empty_follow_metrics()
    if lookup is None or merc_xy.size == 0:
        return empty

    minx, miny = merc_xy.min(axis=0)
    maxx, maxy = merc_xy.max(axis=0)
    local_geoms = lookup.geoms_near_envelope(minx, miny, maxx, maxy)
    samples = _sample_component_xy(merc_xy, axis_vec)
    n_samples = samples.shape[0]
    if n_samples == 0:
        return empty
    if not local_geoms:
        empty["osm_follow_fraction_50m"] = _fmt_float(0.0, 4)
        empty["osm_follow_fraction_75m"] = _fmt_float(0.0, 4)
        empty["osm_follow_fraction_100m"] = _fmt_float(0.0, 4)
        return empty

    local_tree = STRtree(local_geoms)
    distances = np.empty(n_samples, dtype=np.float64)
    angles = []
    for i, xy in enumerate(samples):
        point = Point(float(xy[0]), float(xy[1]))
        geom, dist = _nearest_local_geom(local_geoms, local_tree, point)
        if dist is None:
            distances[i] = np.nan
            continue
        distances[i] = dist
        tangent = local_tangent(geom, point) if geom is not None else None
        if tangent is None:
            continue
        local_vec = _local_component_direction(merc_xy, xy, axis_vec)
        if local_vec is None:
            continue
        angle = _orientation_diff_deg(local_vec[0], local_vec[1], tangent[0], tangent[1])
        if angle is not None:
            angles.append(angle)

    finite = distances[np.isfinite(distances)]
    follow = np.where(np.isfinite(distances), distances, np.inf)
    if finite.size == 0:
        empty["osm_follow_fraction_50m"] = _fmt_float(0.0, 4)
        empty["osm_follow_fraction_75m"] = _fmt_float(0.0, 4)
        empty["osm_follow_fraction_100m"] = _fmt_float(0.0, 4)
        return empty

    p25 = float(np.percentile(finite, 25))
    p75 = float(np.percentile(finite, 75))
    out = {
        "osm_distance_min_m": _fmt_float(float(np.min(finite)), 3),
        "osm_distance_p25_m": _fmt_float(p25, 3),
        "osm_distance_median_m": _fmt_float(float(np.median(finite)), 3),
        "osm_distance_p75_m": _fmt_float(p75, 3),
        "osm_distance_p90_m": _fmt_float(float(np.percentile(finite, 90)), 3),
        "osm_distance_max_m": _fmt_float(float(np.max(finite)), 3),
        "osm_distance_iqr_m": _fmt_float(p75 - p25, 3),
        "osm_follow_fraction_50m": _fmt_float(float(np.mean(follow <= 50.0)), 4),
        "osm_follow_fraction_75m": _fmt_float(float(np.mean(follow <= 75.0)), 4),
        "osm_follow_fraction_100m": _fmt_float(float(np.mean(follow <= 100.0)), 4),
        "osm_parallel_angle_median_deg": "",
        "osm_parallel_fraction_15deg": "",
        "osm_parallel_fraction_30deg": "",
    }
    if angles:
        ang = np.asarray(angles, dtype=np.float64)
        out["osm_parallel_angle_median_deg"] = _fmt_float(float(np.median(ang)), 2)
        out["osm_parallel_fraction_15deg"] = _fmt_float(float(np.mean(ang <= 15.0)), 4)
        out["osm_parallel_fraction_30deg"] = _fmt_float(float(np.mean(ang <= 30.0)), 4)
    return out


class DiagnosticOsmLookup:
    """Nearest-neighbor queries over the same relevant OSM universe used for masking."""

    def __init__(self, items, construction_items=None, open_area_items=None):
        self._all = self._prepare(items)
        ferry_items = [
            item for item in items
            if _item_tags(item).get("route") == "ferry"
        ]
        self._ferry = self._prepare(ferry_items)
        self._construction = self._prepare(construction_items or [])
        self.open_areas = OpenAreaLookup(open_area_items or [])

    @staticmethod
    def _prepare(items):
        geoms = []
        kept = []
        for item in items or []:
            geom = _item_geometry(item)
            if geom is None or geom.is_empty:
                continue
            geoms.append(geom)
            kept.append(item)
        tree = STRtree(geoms) if geoms else None
        return {"items": kept, "geoms": geoms, "tree": tree}

    def nearest(self, merc_x, merc_y, kind="all"):
        store = {"all": self._all, "ferry": self._ferry, "construction": self._construction}[kind]
        if not store["items"] or store["tree"] is None:
            return None
        point = Point(merc_x, merc_y)
        index = store["tree"].nearest(point)
        if index is None:
            return None
        idx = int(np.asarray(index).reshape(-1)[0])
        geom = store["geoms"][idx]
        item = store["items"][idx]
        return item, float(geom.distance(point))

    def geoms_near_envelope(self, minx, miny, maxx, maxy, padding=_OSM_NEIGHBOURHOOD_M):
        store = self._all
        if not store["items"] or store["tree"] is None:
            return []
        env = shapely_box(minx - padding, miny - padding, maxx + padding, maxy + padding)
        indices = store["tree"].query(env, predicate="intersects")
        idxs = np.asarray(indices).reshape(-1)
        if idxs.size == 0:
            return []
        return [store["geoms"][int(i)] for i in idxs]

    def items_geoms_near_envelope(self, minx, miny, maxx, maxy, padding=_LATERAL_OSM_SEARCH_M):
        """Diagnostic-only: OSM items+geoms near an envelope. Does not affect masking."""
        store = self._all
        if not store["items"] or store["tree"] is None:
            return []
        env = shapely_box(minx - padding, miny - padding, maxx + padding, maxy + padding)
        indices = store["tree"].query(env, predicate="intersects")
        idxs = np.asarray(indices).reshape(-1)
        if idxs.size == 0:
            return []
        return [(store["items"][int(i)], store["geoms"][int(i)]) for i in idxs]


def classify_open_area_tags(tags):
    """Return diagnostic open-area class or None. Does not imply suppression."""
    if not tags:
        return None
    leisure = (tags.get("leisure") or "").strip()
    natural = (tags.get("natural") or "").strip()
    amenity = (tags.get("amenity") or "").strip()
    highway = (tags.get("highway") or "").strip()
    area_highway = (tags.get("area:highway") or "").strip()
    sport = (tags.get("sport") or "").strip().lower()
    surface = (tags.get("surface") or "").strip()
    area_flag = (tags.get("area") or "").strip()

    if leisure == "golf_course":
        return "golf_course"
    if natural == "beach":
        return "beach"
    if natural == "sand":
        return "sand"
    if surface == "sand" and (area_flag == "yes" or leisure or natural):
        return "sand"
    if leisure == "pitch":
        return "pitch"
    if leisure == "sports_centre":
        return "sports_centre"
    if leisure == "stadium":
        return "stadium"
    if sport and (
        any(p == "climbing" or p.startswith("climbing") for p in sport.split(";"))
    ):
        return "sport_climbing"
    if sport and leisure not in ("track",):
        # sport=* on an area object (loader only keeps area-like geometries)
        return "sport_area"
    if amenity in ("parking", "parking_space"):
        return "parking"
    if highway == "pedestrian" and area_flag == "yes":
        return "pedestrian_area"
    if area_highway == "pedestrian":
        return "pedestrian_area"
    if leisure == "park":
        return "park"
    if leisure == "playground":
        return "playground"
    return None


def osm_open_area_tags_match(tags):
    return classify_open_area_tags(tags) is not None


def _empty_open_area_metrics():
    return {
        "open_area_class": "",
        "open_area_osm_id": "",
        "open_area_osm_type": "",
        "open_area_name": "",
        "open_area_tags": "",
        "open_area_center_inside": "",
        "open_area_distance_m": "",
        "open_area_component_inside_frac": "",
        "open_area_component_crosses_boundary": "",
        "open_area_component_stays_inside": "",
    }


class OpenAreaLookup:
    """Diagnostic-only spatial index over OSM area polygons/lines. Never masks heat."""

    def __init__(self, items):
        self._items = []
        self._geoms = []
        self._classes = []
        for item in items or []:
            geom = _item_geometry(item)
            if geom is None or geom.is_empty:
                continue
            tags = _item_tags(item)
            cls = classify_open_area_tags(tags)
            if cls is None:
                continue
            self._items.append(item)
            self._geoms.append(geom)
            self._classes.append(cls)
        self._tree = STRtree(self._geoms) if self._geoms else None

    def __len__(self):
        return len(self._items)

    def candidates_near(self, merc_x, merc_y, padding=_OPEN_AREA_SEARCH_M):
        if self._tree is None:
            return []
        env = shapely_box(
            merc_x - padding, merc_y - padding, merc_x + padding, merc_y + padding
        )
        indices = self._tree.query(env, predicate="intersects")
        idxs = np.asarray(indices).reshape(-1)
        out = []
        for i in idxs:
            idx = int(i)
            out.append((self._items[idx], self._geoms[idx], self._classes[idx]))
        return out


def open_area_metrics_for_candidate(
    merc_x,
    merc_y,
    component_merc_xy,
    open_area_lookup,
    preferred_class=None,
):
    """Pick the most relevant nearby open area and describe containment.

    Preference: containing polygon of preferred_class, else any containing
    polygon (golf/beach/sand/climbing first), else nearest by distance.
    Diagnostic only — does not suppress candidates.
    """
    empty = _empty_open_area_metrics()
    if open_area_lookup is None or len(open_area_lookup) == 0:
        return empty

    point = Point(merc_x, merc_y)
    nearby = open_area_lookup.candidates_near(merc_x, merc_y)
    if not nearby:
        # Fallback: global nearest among all open areas (still diagnostic).
        if open_area_lookup._tree is None:
            return empty
        index = open_area_lookup._tree.nearest(point)
        if index is None:
            return empty
        idx = int(np.asarray(index).reshape(-1)[0])
        nearby = [(
            open_area_lookup._items[idx],
            open_area_lookup._geoms[idx],
            open_area_lookup._classes[idx],
        )]

    priority = {
        "golf_course": 0,
        "beach": 1,
        "sand": 2,
        "sport_climbing": 3,
        "pitch": 4,
        "sports_centre": 5,
        "stadium": 6,
        "sport_area": 7,
        "parking": 8,
        "pedestrian_area": 9,
        "park": 10,
        "playground": 11,
    }

    scored = []
    for item, geom, cls in nearby:
        try:
            dist = float(geom.distance(point))
        except Exception:
            continue
        center_inside = False
        if geom.geom_type in ("Polygon", "MultiPolygon"):
            try:
                center_inside = bool(geom.contains(point) or geom.covers(point))
            except Exception:
                center_inside = False
        elif dist <= _OPEN_AREA_CROSS_EPS:
            center_inside = True
        pref = 0 if preferred_class and cls == preferred_class else 1
        scored.append((
            pref,
            0 if center_inside else 1,
            priority.get(cls, 99),
            dist,
            item,
            geom,
            cls,
            center_inside,
        ))
    if not scored:
        return empty
    scored.sort()
    _pref, _in, _pri, dist, item, geom, cls, center_inside = scored[0]
    tags = _item_tags(item)

    inside_frac = ""
    crosses = ""
    stays = ""
    if component_merc_xy is not None and len(component_merc_xy) > 0:
        n = len(component_merc_xy)
        inside_n = 0
        outside_n = 0
        if geom.geom_type in ("Polygon", "MultiPolygon"):
            for xy in component_merc_xy:
                pt = Point(float(xy[0]), float(xy[1]))
                try:
                    if geom.contains(pt) or geom.covers(pt) or geom.distance(pt) <= _OPEN_AREA_CROSS_EPS:
                        inside_n += 1
                    else:
                        outside_n += 1
                except Exception:
                    outside_n += 1
        else:
            # Line-like open areas: treat within 5 m as "inside" the feature band.
            band = 5.0
            for xy in component_merc_xy:
                pt = Point(float(xy[0]), float(xy[1]))
                try:
                    if geom.distance(pt) <= band:
                        inside_n += 1
                    else:
                        outside_n += 1
                except Exception:
                    outside_n += 1
        frac = inside_n / n if n else 0.0
        inside_frac = _fmt_float(frac, 4)
        crosses_flag = inside_n > 0 and outside_n > 0
        crosses = _bool_csv(crosses_flag)
        stays = _bool_csv((frac >= 0.9) and (not crosses_flag) and center_inside)

    return {
        "open_area_class": cls,
        "open_area_osm_id": str(item.osm_id),
        "open_area_osm_type": item.source,
        "open_area_name": tags.get("name", ""),
        "open_area_tags": json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
        "open_area_center_inside": _bool_csv(center_inside),
        "open_area_distance_m": _fmt_float(dist, 3),
        "open_area_component_inside_frac": inside_frac,
        "open_area_component_crosses_boundary": crosses,
        "open_area_component_stays_inside": stays,
    }


def _empty_lateral_metrics(zero_cover=False):
    cover = _fmt_float(0.0, 4) if zero_cover else ""
    return {
        "nearest_parallel_osm_distance_m": "",
        "nearest_parallel_osm_min_m": "",
        "nearest_parallel_osm_angle_deg": "",
        "nearest_parallel_osm_id": "",
        "nearest_parallel_osm_highway": "",
        "nearest_parallel_osm_name": "",
        "parallel_osm_cover_frac_50m": cover,
        "parallel_osm_cover_frac_75m": cover,
        "parallel_osm_cover_frac_100m": cover,
        "parallel_osm_cover_frac_150m": cover,
        "parallel_osm_cover_frac_200m": cover,
        "parallel_osm_cover_frac_250m": cover,
        "parallel_osm_cover_frac_250m_30deg": cover,
        "candidate_axis_heat_mean": "",
        "candidate_axis_heat_p90": "",
        "candidate_axis_heat_n": "",
        "parallel_osm_heat_mean": "",
        "parallel_osm_heat_p90": "",
        "parallel_osm_heat_n": "",
        "parallel_osm_heat_ratio_mean": "",
        "parallel_osm_heat_ratio_p90": "",
        "between_heat_median": "",
        "between_heat_ratio": "",
        "heat_halo_score": "",
    }


def _tile_box(bbox_merc):
    # bbox_merc = (max_y, min_x, min_y, max_x) from get_merc_bbox()
    return shapely_box(bbox_merc[1], bbox_merc[2], bbox_merc[3], bbox_merc[0])


def _merc_to_rowcol(mx, my, bbox_merc, pixel_size):
    col = (mx - bbox_merc[1]) / pixel_size
    row = (bbox_merc[0] - my) / pixel_size
    return row, col


def _sample_heatmap_merc(heatmap, mx, my, bbox_merc, pixel_size):
    if heatmap is None or bbox_merc is None:
        return None
    row, col = _merc_to_rowcol(mx, my, bbox_merc, pixel_size)
    r = int(round(row))
    c = int(round(col))
    height, width = heatmap.shape
    if r < 0 or c < 0 or r >= height or c >= width:
        return None
    return int(heatmap[r, c])


def _heat_array_stats(values):
    if not values:
        return None, None, None, 0
    arr = np.asarray(values, dtype=np.float64)
    return (
        float(np.mean(arr)),
        float(np.percentile(arr, 90)),
        float(np.median(arr)),
        int(arr.size),
    )


def _sample_line_heat(heatmap, geom, bbox_merc, pixel_size, max_samples=_LATERAL_HEAT_MAX_SAMPLES):
    if heatmap is None or geom is None or bbox_merc is None:
        return []
    clipped = geom.intersection(_tile_box(bbox_merc))
    if clipped is None or clipped.is_empty:
        return []
    values = []
    for line in _as_lines(clipped):
        length = line.length
        if length <= 0:
            continue
        n = min(max_samples, max(2, int(math.ceil(length / max(pixel_size, 1.0))) + 1))
        for i in range(n):
            frac = 0.0 if n == 1 else i / (n - 1)
            pt = line.interpolate(frac * length)
            sample = _sample_heatmap_merc(heatmap, pt.x, pt.y, bbox_merc, pixel_size)
            if sample is not None:
                values.append(sample)
            if len(values) >= max_samples:
                return values
    return values


def _sample_axis_heat(heatmap, merc_xy, axis_vec, bbox_merc, pixel_size):
    if heatmap is None or axis_vec is None or merc_xy.shape[0] < 2:
        return []
    vec = np.asarray(axis_vec, dtype=np.float64)
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        return []
    vec = vec / norm
    centroid = merc_xy.mean(axis=0)
    proj = (merc_xy - centroid) @ vec
    t0 = float(np.min(proj))
    t1 = float(np.max(proj))
    span = t1 - t0
    if span < max(pixel_size, 1.0):
        sample = _sample_heatmap_merc(
            heatmap, float(centroid[0]), float(centroid[1]), bbox_merc, pixel_size
        )
        return [] if sample is None else [sample]
    n = min(_LATERAL_HEAT_MAX_SAMPLES, max(2, int(round(span / max(pixel_size, 1.0))) + 1))
    values = []
    for i in range(n):
        t = t0 + span * i / (n - 1)
        pt = centroid + vec * t
        sample = _sample_heatmap_merc(heatmap, float(pt[0]), float(pt[1]), bbox_merc, pixel_size)
        if sample is not None:
            values.append(sample)
    return values


def _sample_segment_heat(heatmap, x0, y0, x1, y1, bbox_merc, pixel_size):
    try:
        line = LineString([(x0, y0), (x1, y1)])
    except Exception:
        return []
    return _sample_line_heat(heatmap, line, bbox_merc, pixel_size, max_samples=24)


def _cover_fractions(distances, n_samples, extra_wide=None):
    out = {}
    if n_samples <= 0:
        for band in _LATERAL_COVER_BANDS_M:
            out[f"parallel_osm_cover_frac_{int(band)}m"] = ""
        out["parallel_osm_cover_frac_250m_30deg"] = ""
        return out
    dist = np.asarray(distances, dtype=np.float64)
    for band in _LATERAL_COVER_BANDS_M:
        out[f"parallel_osm_cover_frac_{int(band)}m"] = _fmt_float(
            float(np.mean(dist <= band)), 4
        )
    if extra_wide is None:
        out["parallel_osm_cover_frac_250m_30deg"] = ""
    else:
        wide = np.asarray(extra_wide, dtype=np.float64)
        out["parallel_osm_cover_frac_250m_30deg"] = _fmt_float(
            float(np.mean(wide <= _LATERAL_OSM_SEARCH_M)), 4
        )
    return out


def component_lateral_osm_metrics(
    merc_xy,
    axis_vec,
    lookup,
    heatmap,
    bbox_merc,
    pixel_size,
    candidate_heat_mean,
    candidate_heat_p90,
):
    """Diagnostic-only: nearby parallel OSM vs heatmap intensity. Not used for suppression."""
    empty = _empty_lateral_metrics()
    if lookup is None or merc_xy.size == 0 or bbox_merc is None:
        return empty

    axis_vals = _sample_axis_heat(heatmap, merc_xy, axis_vec, bbox_merc, pixel_size)
    axis_mean, axis_p90, _axis_med, axis_n = _heat_array_stats(axis_vals)
    if axis_n:
        empty["candidate_axis_heat_mean"] = _fmt_float(axis_mean, 4)
        empty["candidate_axis_heat_p90"] = _fmt_float(axis_p90, 4)
        empty["candidate_axis_heat_n"] = _fmt_int(axis_n)

    minx, miny = merc_xy.min(axis=0)
    maxx, maxy = merc_xy.max(axis=0)
    nearby = lookup.items_geoms_near_envelope(
        float(minx), float(miny), float(maxx), float(maxy), padding=_LATERAL_OSM_SEARCH_M
    )
    samples = _sample_component_xy(merc_xy, axis_vec)
    n_samples = samples.shape[0]
    if n_samples == 0:
        return empty
    if not nearby:
        zeroed = _empty_lateral_metrics(zero_cover=True)
        zeroed["candidate_axis_heat_mean"] = empty["candidate_axis_heat_mean"]
        zeroed["candidate_axis_heat_p90"] = empty["candidate_axis_heat_p90"]
        zeroed["candidate_axis_heat_n"] = empty["candidate_axis_heat_n"]
        return zeroed

    local_items = [pair[0] for pair in nearby]
    local_geoms = [pair[1] for pair in nearby]
    local_tree = STRtree(local_geoms)

    dist_15 = np.full(n_samples, np.inf, dtype=np.float64)
    dist_30 = np.full(n_samples, np.inf, dtype=np.float64)
    votes = {}

    for i, xy in enumerate(samples):
        point = Point(float(xy[0]), float(xy[1]))
        env = shapely_box(
            point.x - _LATERAL_OSM_SEARCH_M,
            point.y - _LATERAL_OSM_SEARCH_M,
            point.x + _LATERAL_OSM_SEARCH_M,
            point.y + _LATERAL_OSM_SEARCH_M,
        )
        indices = local_tree.query(env)
        idxs = np.asarray(indices).reshape(-1)
        ranked = []
        for raw_idx in idxs:
            idx = int(raw_idx)
            geom = local_geoms[idx]
            dist = float(geom.distance(point))
            if dist <= _LATERAL_OSM_SEARCH_M:
                ranked.append((dist, idx))
        ranked.sort(key=lambda item: item[0])
        ranked = ranked[:_LATERAL_MAX_GEOMS_PER_SAMPLE]
        local_vec = _local_component_direction(merc_xy, xy, axis_vec)
        best15 = None
        best30 = None
        for dist, idx in ranked:
            geom = local_geoms[idx]
            tangent = local_tangent(geom, point)
            if tangent is None or local_vec is None:
                continue
            angle = _orientation_diff_deg(
                local_vec[0], local_vec[1], tangent[0], tangent[1]
            )
            if angle is None:
                continue
            if angle <= _LATERAL_PARALLEL_WIDE_DEG and (best30 is None or dist < best30[0]):
                best30 = (dist, angle, idx)
            if angle <= _LATERAL_PARALLEL_DEG and (best15 is None or dist < best15[0]):
                best15 = (dist, angle, idx)
        if best30 is not None:
            dist_30[i] = best30[0]
        if best15 is not None:
            dist_15[i] = best15[0]
            idx = best15[2]
            item = local_items[idx]
            key = (item.source, item.osm_id)
            rec = votes.setdefault(
                key,
                {
                    "item": item,
                    "geom": local_geoms[idx],
                    "dists": [],
                    "angles": [],
                    "count": 0,
                },
            )
            rec["dists"].append(best15[0])
            rec["angles"].append(best15[1])
            rec["count"] += 1

    cover = _cover_fractions(dist_15, n_samples, extra_wide=dist_30)
    out = _empty_lateral_metrics()
    out.update(cover)
    out["candidate_axis_heat_mean"] = empty["candidate_axis_heat_mean"]
    out["candidate_axis_heat_p90"] = empty["candidate_axis_heat_p90"]
    out["candidate_axis_heat_n"] = empty["candidate_axis_heat_n"]

    if not votes:
        return out

    winner = max(
        votes.values(),
        key=lambda rec: (rec["count"], -float(np.median(rec["dists"]))),
    )
    item = winner["item"]
    tags = _item_tags(item)
    out["nearest_parallel_osm_distance_m"] = _fmt_float(float(np.median(winner["dists"])), 3)
    out["nearest_parallel_osm_min_m"] = _fmt_float(float(np.min(winner["dists"])), 3)
    out["nearest_parallel_osm_angle_deg"] = _fmt_float(float(np.median(winner["angles"])), 2)
    out["nearest_parallel_osm_id"] = str(item.osm_id)
    out["nearest_parallel_osm_highway"] = tags.get("highway", "") or tags.get("route", "")
    out["nearest_parallel_osm_name"] = tags.get("name", "")

    osm_vals = _sample_line_heat(heatmap, winner["geom"], bbox_merc, pixel_size)
    osm_mean, osm_p90, _osm_med, osm_n = _heat_array_stats(osm_vals)
    if osm_n:
        out["parallel_osm_heat_mean"] = _fmt_float(osm_mean, 4)
        out["parallel_osm_heat_p90"] = _fmt_float(osm_p90, 4)
        out["parallel_osm_heat_n"] = _fmt_int(osm_n)

    cand_mean = candidate_heat_mean
    cand_p90 = candidate_heat_p90
    if cand_mean is None or cand_mean <= 0:
        cand_mean = axis_mean
    if cand_p90 is None or cand_p90 <= 0:
        cand_p90 = axis_p90
    if osm_n and cand_mean and cand_mean > 0:
        out["parallel_osm_heat_ratio_mean"] = _fmt_float(osm_mean / cand_mean, 4)
    if osm_n and cand_p90 and cand_p90 > 0:
        out["parallel_osm_heat_ratio_p90"] = _fmt_float(osm_p90 / cand_p90, 4)

    centroid = merc_xy.mean(axis=0)
    centroid_pt = Point(float(centroid[0]), float(centroid[1]))
    nearest_on_way = None
    best_line_dist = None
    between_vals = []
    for line in _as_lines(winner["geom"]):
        if line.length <= 0:
            continue
        dist = line.distance(centroid_pt)
        if best_line_dist is None or dist < best_line_dist:
            best_line_dist = dist
            nearest_on_way = line.interpolate(line.project(centroid_pt))
    if nearest_on_way is not None:
        between_vals = _sample_segment_heat(
            heatmap,
            float(centroid[0]),
            float(centroid[1]),
            float(nearest_on_way.x),
            float(nearest_on_way.y),
            bbox_merc,
            pixel_size,
        )
    _bmean, _bp90, between_med, between_n = _heat_array_stats(between_vals)
    if between_n:
        out["between_heat_median"] = _fmt_float(between_med, 4)
        cand_med = cand_mean
        if candidate_heat_mean is not None and candidate_heat_mean > 0:
            cand_med = candidate_heat_mean
        elif axis_mean is not None and axis_mean > 0:
            cand_med = axis_mean
        if cand_med and cand_med > 0:
            out["between_heat_ratio"] = _fmt_float(between_med / cand_med, 4)
        if osm_n and cand_p90 and cand_p90 > 0:
            # min(OSM corridor, in-between) / candidate: high when the candidate
            # sits in a hot lateral halo rather than an independent corridor.
            halo_num = min(osm_p90, between_med)
            out["heat_halo_score"] = _fmt_float(halo_num / cand_p90, 4)
    return out


def _osm_fields(item, distance_m):
    if item is None:
        return {
            "nearest_osm_distance_m": "",
            "nearest_osm_type": "",
            "nearest_osm_id": "",
            "nearest_osm_highway": "",
            "nearest_osm_railway": "",
            "nearest_osm_aeroway": "",
            "nearest_osm_leisure": "",
            "nearest_osm_route": "",
            "nearest_osm_construction": "",
            "nearest_osm_name": "",
            "nearest_osm_tags": "",
        }
    tags = _item_tags(item)
    return {
        "nearest_osm_distance_m": _fmt_float(distance_m, 3),
        "nearest_osm_type": item.source,
        "nearest_osm_id": str(item.osm_id),
        "nearest_osm_highway": tags.get("highway", ""),
        "nearest_osm_railway": tags.get("railway", ""),
        "nearest_osm_aeroway": tags.get("aeroway", ""),
        "nearest_osm_leisure": tags.get("leisure", ""),
        "nearest_osm_route": tags.get("route", ""),
        "nearest_osm_construction": tags.get("construction", ""),
        "nearest_osm_name": tags.get("name", ""),
        "nearest_osm_tags": json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
    }


def _named_distance_fields(prefix, item, distance_m):
    if item is None:
        return {
            f"{prefix}_distance_m": "",
            f"{prefix}_id": "",
            f"{prefix}_name": "",
        }
    tags = _item_tags(item)
    return {
        f"{prefix}_distance_m": _fmt_float(distance_m, 3),
        f"{prefix}_id": str(item.osm_id),
        f"{prefix}_name": tags.get("name", ""),
    }


def build_diagnostic_row(
    *,
    zoom,
    tile_x,
    tile_y,
    candidate_index,
    peak_row,
    peak_col,
    center_lon,
    center_lat,
    pixel_size_m,
    heatmap_snapshot,
    threshold,
    lookup,
    lon2x,
    lat2y,
    too_small,
    accepted,
    written_to_geojson,
    bbox_merc=None,
    heatmap_unmasked=None,
    suppressed_parallel_osm=False,
    suppressed_ferry=False,
    suppressed_heat_halo=False,
    inside_area=True,
):
    candidate_id = f"{zoom}/{tile_x}/{tile_y}/{peak_row}/{peak_col}"
    values, rows, cols = extract_component_pixels(
        heatmap_snapshot, peak_row, peak_col, threshold
    )
    n_pixels = int(values.size)
    if n_pixels:
        width_px = int(np.max(cols) - np.min(cols) + 1)
        height_px = int(np.max(rows) - np.min(rows) + 1)
        length_m = math.hypot(width_px, height_px) * pixel_size_m
        area_m2 = n_pixels * (pixel_size_m ** 2)
    else:
        width_px = ""
        height_px = ""
        length_m = ""
        area_m2 = ""

    row = {
        "candidate_id": candidate_id,
        "tile_z": zoom,
        "tile_x": tile_x,
        "tile_y": tile_y,
        "candidate_index_in_tile": candidate_index,
        "peak_row": peak_row,
        "peak_col": peak_col,
        "center_lon": _fmt_float(center_lon, 10),
        "center_lat": _fmt_float(center_lat, 10),
        "geometry_length_m": _fmt_float(length_m, 3) if length_m != "" else "",
        "geometry_area_m2": _fmt_float(area_m2, 3) if area_m2 != "" else "",
        "component_pixels": _fmt_int(n_pixels) if n_pixels else "",
        "component_width_px": _fmt_int(width_px) if width_px != "" else "",
        "component_height_px": _fmt_int(height_px) if height_px != "" else "",
    }
    row.update(component_strava_stats(values))

    merc_x = lon2x(center_lon)
    merc_y = lat2y(center_lat)
    nearest = lookup.nearest(merc_x, merc_y, "all") if lookup is not None else None
    ferry = lookup.nearest(merc_x, merc_y, "ferry") if lookup is not None else None
    construction = lookup.nearest(merc_x, merc_y, "construction") if lookup is not None else None

    if nearest is None:
        row.update(_osm_fields(None, None))
    else:
        item, dist = nearest
        row.update(_osm_fields(item, dist))
    if ferry is None:
        row.update(_named_distance_fields("nearest_ferry", None, None))
    else:
        item, dist = ferry
        row.update(_named_distance_fields("nearest_ferry", item, dist))
    if construction is None:
        row.update(_named_distance_fields("nearest_construction", None, None))
    else:
        item, dist = construction
        row.update(_named_distance_fields("nearest_construction", item, dist))

    angle, elong, follow = follow_metrics_from_component_pixels(
        values, rows, cols, bbox_merc, pixel_size_m, lookup
    )
    row["component_orientation_deg"] = _fmt_float(angle, 2) if angle is not None else ""
    row["component_elongation"] = _fmt_float(elong, 3) if elong is not None else ""
    row.update(follow)

    if n_pixels and bbox_merc is not None:
        row.update(
            lateral_metrics_from_component_pixels(
                values,
                rows,
                cols,
                bbox_merc,
                pixel_size_m,
                lookup,
                heatmap_snapshot,
                heatmap_unmasked,
            )
        )
    else:
        row.update(_empty_lateral_metrics())

    component_merc = None
    if n_pixels and bbox_merc is not None:
        component_merc = pixels_to_mercator(rows, cols, bbox_merc, pixel_size_m)
    open_lookup = None
    if lookup is not None:
        open_lookup = getattr(lookup, "open_areas", None)
    row.update(
        open_area_metrics_for_candidate(
            merc_x, merc_y, component_merc, open_lookup
        )
    )

    row["raw_candidate"] = _bool_csv(True)
    row["too_small"] = _bool_csv(too_small)
    row["accepted"] = _bool_csv(accepted)
    row["inside_area"] = _bool_csv(inside_area)
    row["written_to_geojson"] = _bool_csv(written_to_geojson)
    row["suppressed_parallel_osm"] = _bool_csv(suppressed_parallel_osm)
    row["suppressed_ferry"] = _bool_csv(suppressed_ferry)
    row["suppressed_heat_halo"] = _bool_csv(suppressed_heat_halo)
    return row


class DiagnosticWriter:
    def __init__(self, path):
        self.path = path
        self.rows_written = 0
        self._fh = open(path, "w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(
            self._fh,
            fieldnames=DIAGNOSTIC_COLUMNS,
            extrasaction="ignore",
        )
        self._writer.writeheader()
        self._fh.flush()

    def write_row(self, row):
        self._writer.writerow(row)
        self._fh.flush()
        self.rows_written += 1

    def close(self):
        if self._fh is not None:
            try:
                self._fh.flush()
            finally:
                self._fh.close()
                self._fh = None
