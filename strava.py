#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ******************************************************************************
#
import os
import argparse
import hashlib
import json
import math
import sys
import tempfile
import time
from datetime import datetime
import requests
from PIL import Image, ImageDraw
import numpy as np
import xml.etree.ElementTree as ET
from shapely.geometry.polygon import Polygon
from shapely.geometry import box, shape, Point
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree
import sqlite3
import signal

from diagnostics import (
    DiagnosticOsmLookup,
    DiagnosticWriter,
    build_diagnostic_row,
    component_osm_follow_metrics,
    nearest_ferry_distance_m,
    should_suppress_ferry,
    should_suppress_parallel_osm,
)


def print_debug(*args):
    if debug:
        print(*args)


def print_verbose(*args):
    if verbose:
        print(*args)


def _format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_int(value):
    return f"{int(value):,}"


class RunStats:
    def __init__(self):
        self.status = "SUCCESS"
        self.exit_code = 0
        self.run_started = None
        self.started_at = None
        self.finished_at = None
        self.elapsed = 0.0
        self.tiles_total = 0
        self.tiles_progress = 0
        self.tiles_processed = 0
        self.tiles_empty = 0
        self.tiles_below_threshold = 0
        self.tiles_with_detection = 0
        self.tiles_failed = 0
        self.strava_tiles_downloaded = 0
        self.strava_tiles_from_cache = 0
        self.strava_retries = 0
        self.detections_raw = 0
        self.detections_too_small = 0
        self.detections_outside_area = 0
        self.detections_accepted = 0
        self.parallel_osm_suppressed = 0
        self.ferry_suppressed = 0
        self.suppression_overlap = 0
        self.total_suppressed = 0
        self.geojson_features = 0
        self.warnings_total = 0
        self.osm_source = None
        self.osm_ways = 0
        self.osm_relations = 0
        self.osm_index_objects = 0
        self.activity = None
        self.strava_backend = None
        self.zoom = None
        self.threshold = None
        self.min_size = None
        self.distance = None
        self.offset = None
        self.area = None
        self.output = None
        self.tile = None
        self.diagnostics_path = None
        self.diagnostic_rows = 0
        self.suppress_parallel_osm = False
        self.suppress_ferry = False

    def note_warning(self):
        self.warnings_total += 1

    def note_tile_progress(self, tile_x, tile_y, zoom_level):
        self.tiles_progress += 1
        if not verbose:
            return
        total = self.tiles_total or 1
        n = self.tiles_progress
        pct = 100.0 * n / total
        line = (
            f"Tile {n}/{self.tiles_total} ({pct:.1f}%): "
            f"z{zoom_level}/{tile_x}/{tile_y}"
        )
        if self.run_started is not None and n >= 5:
            elapsed = time.perf_counter() - self.run_started
            eta = elapsed / n * max(0, total - n)
            line += f" - elapsed {_format_duration(elapsed)} - ETA {_format_duration(eta)}"
        print(line)

    def as_dict(self):
        started = (
            self.started_at.strftime("%Y-%m-%d %H:%M:%S") if self.started_at else None
        )
        finished = (
            self.finished_at.strftime("%Y-%m-%d %H:%M:%S") if self.finished_at else None
        )
        payload = {
            "status": self.status,
            "area": self.area,
            "tile": self.tile,
            "output": self.output,
            "activity": self.activity,
            "strava_backend": self.strava_backend,
            "zoom": self.zoom,
            "threshold": self.threshold,
            "min_size": self.min_size,
            "distance": self.distance,
            "offset": self.offset,
            "osm_source": self.osm_source,
            "osm_ways": self.osm_ways,
            "osm_relations": self.osm_relations,
            "osm_index_objects": self.osm_index_objects,
            "tiles_total": self.tiles_total,
            "tiles_processed": self.tiles_processed,
            "tiles_empty": self.tiles_empty,
            "tiles_below_threshold": self.tiles_below_threshold,
            "tiles_with_detection": self.tiles_with_detection,
            "tiles_failed": self.tiles_failed,
            "strava_tiles_downloaded": self.strava_tiles_downloaded,
            "strava_tiles_from_cache": self.strava_tiles_from_cache,
            "strava_retries": self.strava_retries,
            "detections_raw": self.detections_raw,
            "detections_too_small": self.detections_too_small,
            "detections_outside_area": self.detections_outside_area,
            "detections_accepted": self.detections_accepted,
            "parallel_osm_suppressed": self.parallel_osm_suppressed,
            "ferry_suppressed": self.ferry_suppressed,
            "suppression_overlap": self.suppression_overlap,
            "total_suppressed": self.total_suppressed,
            "suppress_parallel_osm": self.suppress_parallel_osm,
            "suppress_ferry": self.suppress_ferry,
            "geojson_features": self.geojson_features,
            "warnings_total": self.warnings_total,
            "started": started,
            "finished": finished,
            "runtime_seconds": int(round(self.elapsed)),
            "runtime": _format_duration(self.elapsed),
        }
        if self.diagnostics_path:
            payload["diagnostics_path"] = self.diagnostics_path
            payload["diagnostic_rows"] = self.diagnostic_rows
        return payload

    def print_summary(self, stream):
        def line(label, value):
            print(f"{label + ':':<24} {value}", file=stream)

        print("", file=stream)
        print("---", file=stream)
        print("", file=stream)
        print("## Run summary", file=stream)
        print("", file=stream)
        line("Run status", self.status)
        if self.area:
            line("Area", self.area)
        if self.tile:
            line("Tile", self.tile)
        line("Activity", self.activity)
        line("Strava backend", self.strava_backend)
        line("Zoom", self.zoom)
        line("Threshold", self.threshold)
        line("Minimum size", f"{self.min_size} px")
        line("Maximum OSM distance", f"{self.distance} m")
        if self.offset is not None:
            line("Offset", self.offset)
        print("", file=stream)
        if self.osm_source:
            line("OSM source", self.osm_source)
            line("Relevant OSM ways", _format_int(self.osm_ways))
            line("Relevant OSM relations", _format_int(self.osm_relations))
            line("Spatial index objects", _format_int(self.osm_index_objects))
            print("", file=stream)
        line("Tiles planned", _format_int(self.tiles_total))
        line("Tiles processed", _format_int(self.tiles_processed))
        line("Empty", _format_int(self.tiles_empty))
        line("Below threshold", _format_int(self.tiles_below_threshold))
        line("With detections", _format_int(self.tiles_with_detection))
        line("Failed", _format_int(self.tiles_failed))
        print("", file=stream)
        line("Strava downloaded", _format_int(self.strava_tiles_downloaded))
        line("Strava from cache", _format_int(self.strava_tiles_from_cache))
        if self.strava_retries:
            line("Strava retries", _format_int(self.strava_retries))
        print("", file=stream)
        line("Raw detections", _format_int(self.detections_raw))
        line("Rejected (too small)", _format_int(self.detections_too_small))
        line("Rejected (outside area)", _format_int(self.detections_outside_area))
        if self.suppress_parallel_osm and self.suppress_ferry:
            line("Accepted before suppression", _format_int(self.detections_accepted))
            line("Suppressed parallel to OSM", _format_int(self.parallel_osm_suppressed))
            line("Suppressed ferry traces", _format_int(self.ferry_suppressed))
            line("Suppression overlap", _format_int(self.suppression_overlap))
            line("Total suppressed", _format_int(self.total_suppressed))
        elif self.suppress_parallel_osm:
            line("Accepted before parallel suppression", _format_int(self.detections_accepted))
            line("Suppressed parallel to OSM", _format_int(self.parallel_osm_suppressed))
        elif self.suppress_ferry:
            line("Accepted before ferry suppression", _format_int(self.detections_accepted))
            line("Suppressed ferry traces", _format_int(self.ferry_suppressed))
        else:
            line("Accepted detections", _format_int(self.detections_accepted))
        print("", file=stream)
        line("GeoJSON features", _format_int(self.geojson_features))
        if self.output:
            line("Output", self.output)
        if self.diagnostics_path:
            line("Diagnostics", self.diagnostics_path)
            line("Diagnostic rows", _format_int(self.diagnostic_rows))
        print("", file=stream)
        line("Warnings", _format_int(self.warnings_total))
        line("Failed tiles", _format_int(self.tiles_failed))
        print("", file=stream)
        if self.started_at is not None:
            line("Started", self.started_at.strftime("%Y-%m-%d %H:%M:%S"))
        if self.finished_at is not None:
            line("Finished", self.finished_at.strftime("%Y-%m-%d %H:%M:%S"))
        line("Runtime", _format_duration(self.elapsed))
        print("----------------------------------", file=stream)


run_stats = RunStats()
collect_diagnostic_osm = False
diagnostic_writer = None
diagnostic_osm = None
suppress_parallel_osm = False
suppress_ferry = False
requested_area = None
requested_area_prepared = None


def count_planned_strava_tiles(polygon_area, xul, yul, xlr, ylr, offset_x, offset_y, step, zoom):
    n = 0
    for tile_x in range(xul + offset_x, xlr, step):
        for tile_y in range(yul - offset_y, ylr, -step):
            (lat_ul, lon_ul, lat_lr, lon_lr) = get_geo_bbox(tile_x, tile_y, zoom)
            polygon_strava = Polygon((
                (lon_ul, lat_ul), (lon_lr, lat_ul),
                (lon_lr, lat_lr), (lon_ul, lat_lr), (lon_ul, lat_ul),
            ))
            if polygon_area is None or polygon_strava.intersects(polygon_area):
                n += 1
    return n


def _system_exit_code(exc):
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 1


def finalize_run(stats_json_path=None, summary_stream=None):
    if run_stats.run_started is None:
        return
    run_stats.finished_at = datetime.now()
    run_stats.elapsed = time.perf_counter() - run_stats.run_started
    if summary_stream is None:
        summary_stream = sys.stderr
    run_stats.print_summary(summary_stream)
    if stats_json_path:
        with open(stats_json_path, "w", encoding="utf-8") as stats_file:
            json.dump(run_stats.as_dict(), stats_file, indent=2)
            stats_file.write("\n")


# Convert geographical coordinates to tile number
def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 1 << zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile


# Convert tile number to geographical coordinates
def num2deg(xtile, ytile, zoom):
    n = 1 << zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg


RADIUS = 6378137.0  # in meters on the equator

# overpass-api.de rejects the default python-requests User-Agent with HTTP 406 (fair-use policy).
_OVERPASS_HTTP_HEADERS = {
    "User-Agent": "osm-strava/1.0 (OSM/Strava heatmap comparison; +https://github.com/osm-strava/osm-strava)",
}

_STRAVA_TILE_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# OSM tag filter shared by Overpass QL and local PBF import (keep value order stable).
_OSM_HIGHWAY_VALUES = (
    "bridleway", "corridor", "crossing", "cycleway", "escape", "footway",
    "living_street", "motorway", "motorway_link", "path", "pedestrian",
    "primary", "primary_link", "raceway", "residential", "road",
    "secondary", "secondary_link", "service", "steps", "tertiary",
    "tertiary_link", "track", "trunk", "trunk_link", "unclassified",
)
_OSM_AEROWAY_VALUES = ("runway", "taxiway")
_OSM_LEISURE_VALUES = ("track", "pitch")
_OSM_HIGHWAY_SET = frozenset(_OSM_HIGHWAY_VALUES)
_OSM_AEROWAY_SET = frozenset(_OSM_AEROWAY_VALUES)
_OSM_LEISURE_SET = frozenset(_OSM_LEISURE_VALUES)

def _overpass_value_regex(values):
    return "|".join(values)


_OVERPASS_FILTER = (
    f'(nwr[highway~"{_overpass_value_regex(_OSM_HIGHWAY_VALUES)}"];'
    'nwr[highway=construction];'
    f'nwr["area:highway"~"{_overpass_value_regex(_OSM_HIGHWAY_VALUES)}"];'
    'nwr[railway];'
    f'nwr[aeroway~"{_overpass_value_regex(_OSM_AEROWAY_VALUES)}"];'
    f'nwr["area:aeroway"~"{_overpass_value_regex(_OSM_AEROWAY_VALUES)}"];'
    f'nwr[leisure~"{_overpass_value_regex(_OSM_LEISURE_VALUES)}"];'
    'nwr[route=ferry];)'
)
_OVERPASS_QUERY_VERSION = "out geom v1"
_OVERPASS_DEFAULT_URL = "https://overpass-api.de/api/interpreter"
_OSM_BATCH_GRID = 4
_OSM_MAX_SUBDIVIDE_LEVEL = 3
_OVERPASS_BATCH_GAP_SEC = 2
_OSM_CACHE_LAYOUT_VERSION = "batch-4x4-subdiv3-v1"
overpass_url = _OVERPASS_DEFAULT_URL
_last_overpass_success = 0.0


# Convert latitude to northing (Pseudo-Mercator projection)
def lat2y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * RADIUS


# Convert longitude to easting (Pseudo-Mercator projection)
def lon2x(lon):
    return math.radians(lon) * RADIUS


# Convert northing (Pseudo-Mercator projection) to latitude
def y2lat(y):
    return math.degrees(2 * math.atan(math.exp(y / RADIUS)) - math.pi / 2.0)


# Convert easting (Pseudo-Mercator projection) to longitude
def x2lon(x):
    return math.degrees(x / RADIUS)


# Get bounding box of a Strava tile in geographical coordinates
def get_geo_bbox(x, y, zoom):
    (lat_ul, lon_ul) = num2deg(x, y, zoom)
    (lat_lr, lon_lr) = num2deg(x + 1, y + 1, zoom)
    return lat_ul, lon_ul, lat_lr, lon_lr


# Get bounding box of a Strava tile in pseudo-Mercator coordinates
def get_merc_bbox(x, y, zoom):
    (lat_ul, lon_ul, lat_lr, lon_lr) = get_geo_bbox(x, y, zoom)
    return lat2y(lat_ul), lon2x(lon_ul), lat2y(lat_lr), lon2x(lon_lr)


# Transforms projected coordinates to image coordinates
def transform(coords_merc, bbox_merc, pixel_size):
    transformed = []
    for coord in coords_merc:
        transformed.append((round((coord[0] - bbox_merc[1]) / pixel_size),
                            round((bbox_merc[0] - coord[1]) / pixel_size)))
    return transformed


# Transforms image coordinates to projected coordinates
def reverse_transform(coords, bbox_merc, pixel_size):
    return (x2lon(coords[1] * pixel_size + bbox_merc[1]),
            y2lat(bbox_merc[0] - coords[0] * pixel_size))


# Plot a black line on the image
def plot_line(draw, coords_merc, bbox, w, pixel_size):
    draw.line(transform(coords_merc, bbox, pixel_size), fill=0, width=w)


# Plot a polygon on the image
def plot_polygon(draw, coords_merc, bbox, pixel_size):
    draw.polygon(transform(coords_merc, bbox, pixel_size), outline=0, fill=0)


# Plot a circle on the image
def plot_circle(draw, center_merc, bbox, diameter, pixel_size):
    radius = diameter / 2
    centers = transform(center_merc, bbox, pixel_size)
    for center in centers:
        bbox = [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius]
        draw.ellipse(bbox, fill=0)


# Recursive routine to measure the area of the trace on heatmap
def check_trace_area(image, row, col, target_color, min_size, size):
    # Check if the current pixel is within the image boundaries
    if row < 0 or row >= len(image) or col < 0 or col >= len(image[0]):
        return size

    # Check if the color of the current pixel matches the target color
    if image[row][col] < target_color:
        return size

    # Change the color of the current pixel to the replacement color
    image[row][col] = 0
    size = size + 1
    if size > min_size:   # To avoid stack overflow
        return size

    # Recursively call check_trace_area on adjacent pixels
    size = check_trace_area(image, row + 1, col, target_color, min_size, size)  # Down
    size = check_trace_area(image, row - 1, col, target_color, min_size, size)  # Up
    size = check_trace_area(image, row, col + 1, target_color, min_size, size)  # Right
    size = check_trace_area(image, row, col - 1, target_color, min_size, size)  # Left
    return size


_STRAVA_COOKIE_MISSING = (
    "Strava authentication cookie missing. Create .strava-cookie in the repository directory."
)


def load_strava_cookie():
    env_cookie = os.environ.get("STRAVA_COOKIE")
    if env_cookie is not None and env_cookie.strip():
        return env_cookie.strip()
    cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".strava-cookie")
    try:
        with open(cookie_path, encoding="utf-8") as cookie_file:
            cookie = cookie_file.read().strip()
    except OSError:
        print(_STRAVA_COOKIE_MISSING, file=sys.stderr)
        sys.exit(1)
    if not cookie:
        print(_STRAVA_COOKIE_MISSING, file=sys.stderr)
        sys.exit(1)
    return cookie


def print_strava_tile_debug(url, response, cache_file_path):
    if not verbose or strava_tile_backend != "strava":
        return
    print_verbose("Requested URL:", url)
    print_verbose("HTTP status:", response.status_code)
    content_length = response.headers.get("Content-Length")
    if content_length is None:
        content_length = len(response.content)
    print_verbose("Content-Length:", content_length)
    try:
        with Image.open(cache_file_path) as image:
            print_verbose("Image size:", image.size)
            print_verbose("Image mode:", image.mode)
            pixels = np.array(image)
            gray = np.array(image.convert("L"))
    except Exception:
        return
    print_verbose("min pixel:", int(np.min(pixels)))
    max_pixel = int(np.max(pixels))
    print_verbose("max pixel:", max_pixel)
    print_verbose("mean pixel:", float(np.mean(pixels)))
    positive = gray[gray > 0]
    print_verbose("pixels > 0:", int(positive.size))
    if max_pixel == 0 or positive.size == 0:
        print_verbose("Strava heatmap tile is completely empty")
        return
    print_verbose("min > 0:", int(np.min(positive)))
    print_verbose("P10:", float(np.percentile(positive, 10)))
    print_verbose("P25:", float(np.percentile(positive, 25)))
    print_verbose("Median:", float(np.percentile(positive, 50)))
    print_verbose("P75:", float(np.percentile(positive, 75)))
    print_verbose("P90:", float(np.percentile(positive, 90)))
    print_verbose("P95:", float(np.percentile(positive, 95)))
    print_verbose("P99:", float(np.percentile(positive, 99)))
    print_verbose("Maximum:", int(np.max(positive)))


# Check if Strava file is available in cache and download it if not in cache
def fetch_strava_tile(zoom, x, y):
    cache_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "cache",
        "strava"
    )
    cache_file_path = os.path.join(
        cache_dir, activity, strava_tile_backend, str(zoom), str(x), str(y) + '.png'
    )
    if os.path.isfile(cache_file_path):
        if os.path.getsize(cache_file_path) > 0:
            print_verbose("Tile in cache:", cache_file_path)
            run_stats.strava_tiles_from_cache += 1
            return cache_file_path
        else:
            print_verbose("Empty tile in cache :", cache_file_path)
            run_stats.strava_tiles_from_cache += 1
            run_stats.tiles_processed += 1
            return None
    dir1 = os.path.join(cache_dir, activity, strava_tile_backend, str(zoom))
    if not os.path.isdir(dir1):
        os.makedirs(dir1, exist_ok=True)
    dir2 = os.path.join(dir1, str(x))
    if not os.path.isdir(dir2):
        os.mkdir(dir2)

    headers = dict(_STRAVA_TILE_BROWSER_HEADERS)
    if strava_tile_backend == "strava":
        url = (
            "https://content-a.strava.com/identified/globalheat/sport_Ride/grayscale/"
            f"{zoom}/{x}/{y}@2x.png?v=20&missing=empty"
        )
        headers["Origin"] = "https://www.strava.com"
        headers["Referer"] = "https://www.strava.com/"
        headers["Cookie"] = strava_cookie
    elif strava_tile_backend == "nakarte":
        url = (
            "https://proxy.nakarte.me/https/heatmap-external-a.strava.com/tiles-auth/"
            f"{activity}/hot/{zoom}/{x}/{y}.png?px=256"
        )
    else:
        url = f"https://strava-heatmap.tiles.freemap.sk/{activity}/hot/{zoom}/{x}/{y}.png"
    print_verbose("Downloading Strava tile at", url)
    retries = 10
    while retries > 0:
        try:
            r = requests.get(url, allow_redirects=True, headers=headers)
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print_debug("Status code =", e.response.status_code)
            if e.response.status_code == 404:
                print_verbose("Strava tile missing (404), skipping", zoom, x, y)
                return None
            if strava_tile_backend == "strava" and e.response.status_code in (401, 403):
                print(
                    "Strava authentication failed. The cookie is probably expired; "
                    "renew .strava-cookie from a current browser session.",
                    file=sys.stderr,
                )
                sys.exit(1)
            elif e.response.status_code == 403:
                print("Wait for 10 seconds", file=sys.stderr)
                time.sleep(10)
            print(e, file=sys.stderr)
            run_stats.note_warning()
        except requests.exceptions.RequestException as e:
            print(e, file=sys.stderr)
            run_stats.note_warning()
        else:
            body = r.content
            if len(body) >= 24 and body.startswith(_PNG_SIGNATURE):
                open(cache_file_path, 'wb').write(body)
                print_strava_tile_debug(url, r, cache_file_path)
                break
            print(
                f"Warning: Strava response is not a PNG ({len(body)} bytes), retrying",
                file=sys.stderr,
            )
            run_stats.note_warning()
        retries = retries - 1
        run_stats.strava_retries += 1
        print("Retries: ", retries, file=sys.stderr)
        time.sleep(1)

    if retries == 0:
        print("Network problem, aborting", file=sys.stderr)
        run_stats.tiles_failed += 1
        exit(1)

    run_stats.strava_tiles_downloaded += 1
    return cache_file_path


def overpass_ql(south, west, north, east):
    return (
        f'[bbox:{south},{west},{north},{east}];'
        f'{_OVERPASS_FILTER};out geom;'
    )


def _envelope_from_coords(coords):
    if not coords:
        return None
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    if minx == maxx:
        minx -= 0.01
        maxx += 0.01
    if miny == maxy:
        miny -= 0.01
        maxy += 0.01
    return box(minx, miny, maxx, maxy)


def _nd_coords_merc(element):
    coords = []
    for node in element.iter('nd'):
        if "lon" in node.attrib and "lat" in node.attrib:
            coords.append((lon2x(float(node.attrib["lon"])), lat2y(float(node.attrib["lat"]))))
    return coords


def _xml_tag_pairs(element):
    return [(tag.attrib["k"], tag.attrib["v"]) for tag in element.iter("tag")]


def _tag_pairs_get(pairs, key):
    for k, v in pairs:
        if k == key:
            return v
    return None


def _tag_pairs_has_key(pairs, key):
    return any(k == key for k, v in pairs)


def osm_tags_match_pairs(pairs):
    highway = _tag_pairs_get(pairs, "highway")
    if highway in _OSM_HIGHWAY_SET:
        return True
    # osm-strava detects missing geometry. highway=construction is already
    # mapped OSM geometry, so it is masked at the normal distance (default 35 m).
    # This is not a suppression rule and does not include highway=proposed.
    if highway == "construction":
        return True
    if _tag_pairs_get(pairs, "area:highway") in _OSM_HIGHWAY_SET:
        return True
    if _tag_pairs_has_key(pairs, "railway"):
        return True
    if _tag_pairs_get(pairs, "aeroway") in _OSM_AEROWAY_SET:
        return True
    if _tag_pairs_get(pairs, "area:aeroway") in _OSM_AEROWAY_SET:
        return True
    if _tag_pairs_get(pairs, "leisure") in _OSM_LEISURE_SET:
        return True
    if _tag_pairs_get(pairs, "route") == "ferry":
        return True
    return False


def _is_construction_pairs(pairs):
    return _tag_pairs_get(pairs, "highway") == "construction"


def _is_area_from_tags(pairs, is_relation):
    area = False
    for k, v in pairs:
        if (k == "area" and v == "yes") or \
           k.startswith("area:") or \
           (k == "leisure" and v != "track"):
            area = True
        if is_relation and k == "type" and v == "multipolygon":
            area = True
        if k == "area" and v == "no":
            area = False
    return area


def _is_area_way(element):
    return _is_area_from_tags(_xml_tag_pairs(element), False)


def _is_area_relation(element):
    return _is_area_from_tags(_xml_tag_pairs(element), True)


def _merge_outer_rings(member_coords, relation_id):
    polygons = []
    leftover = [coords for coords in member_coords if coords]
    while leftover:
        coords = leftover.pop(0)
        finished = False
        while not finished:
            for coord in leftover:
                if coords[-1] == coord[0]:
                    leftover.remove(coord)
                    coords = coords + coord
                    break
                elif coords[-1] == coord[-1]:
                    leftover.remove(coord)
                    coord.reverse()
                    coords = coords + coord
                    break
            else:
                finished = True
        if coords[0] != coords[-1]:
            print("Error: Polygon not closed in relation", relation_id, file=sys.stderr)
            print("Please fix the problem and restart this program", file=sys.stderr)
            sys.exit(1)
        polygons.append(coords)
    return polygons


class OsmDrawItem:
    __slots__ = ("source", "osm_id", "fill_polygon", "coords", "envelope", "tags")

    def __init__(self, source, osm_id, fill_polygon, coords, envelope, tags=None):
        self.source = source
        self.osm_id = osm_id
        self.fill_polygon = fill_polygon
        self.coords = coords
        self.envelope = envelope
        self.tags = tags


class OsmIndex:
    def __init__(self, items, n_ways, n_relations):
        self.items = items
        self.n_ways = n_ways
        self.n_relations = n_relations
        self.construction_items = []
        self._tree = STRtree([item.envelope for item in items])

    def query(self, tile_env):
        if not self.items:
            return []
        indices = self._tree.query(tile_env, predicate="intersects")
        return [self.items[int(i)] for i in indices]


def _item_tags_payload(tags):
    if not collect_diagnostic_osm:
        return None
    return {k: v for k, v in tags}


def _draw_items_from_way(osm_id, tags, coords):
    envelope = _envelope_from_coords(coords)
    if envelope is None:
        return []
    return [OsmDrawItem(
        "way",
        str(osm_id),
        _is_area_from_tags(tags, False),
        coords,
        envelope,
        _item_tags_payload(tags),
    )]


def _draw_items_from_relation(osm_id, tags, way_members):
    items = []
    area = _is_area_from_tags(tags, True)
    tag_payload = _item_tags_payload(tags)
    if area:
        outer_coords = [coords for role, coords in way_members if role == "outer" and coords]
        for ring in _merge_outer_rings(outer_coords, osm_id):
            envelope = _envelope_from_coords(ring)
            if envelope is None:
                continue
            items.append(OsmDrawItem("relation", str(osm_id), True, ring, envelope, tag_payload))
    else:
        for _role, coords in way_members:
            envelope = _envelope_from_coords(coords)
            if envelope is None:
                continue
            items.append(OsmDrawItem("relation", str(osm_id), False, coords, envelope, tag_payload))
    return items


def build_osm_index(way_records, relation_records):
    items = []
    for osm_id, tags, coords in way_records:
        items.extend(_draw_items_from_way(osm_id, tags, coords))
    for osm_id, tags, way_members in relation_records:
        items.extend(_draw_items_from_relation(osm_id, tags, way_members))
    return OsmIndex(items, len(way_records), len(relation_records))


def parse_osm_root(osm_root):
    way_records = []
    for way in osm_root.findall('way'):
        coords = _nd_coords_merc(way)
        if not coords:
            continue
        way_records.append((way.attrib.get("id", ""), _xml_tag_pairs(way), coords))
    relation_records = []
    for relation in osm_root.findall('relation'):
        osm_id = relation.attrib.get("id", "")
        way_members = []
        for member in relation.iter('member'):
            if member.attrib.get('type') != 'way':
                continue
            coords = _nd_coords_merc(member)
            if not coords:
                continue
            way_members.append((member.attrib.get("role", ""), coords))
        relation_records.append((osm_id, _xml_tag_pairs(relation), way_members))
    return build_osm_index(way_records, relation_records)


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".osm-", suffix=".xml", dir=directory)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _safe_area_stem(area_path):
    stem = os.path.splitext(os.path.basename(area_path))[0]
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
    return cleaned or "area"


def osm_cache_dir(area_path, south, west, north, east):
    payload = (
        f"{south:.8f},{west:.8f},{north:.8f},{east:.8f}|"
        f"{_OVERPASS_FILTER}|{_OVERPASS_QUERY_VERSION}|{_OSM_CACHE_LAYOUT_VERSION}"
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "cache",
        "osm",
        f"{_safe_area_stem(area_path)}-{digest}",
    )


def _batch_xml_path(cache_dir, name):
    return os.path.join(cache_dir, name + ".xml")


def _read_cached_osm(path):
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return None
    try:
        with open(path, "rb") as cache_fh:
            return ET.fromstring(cache_fh.read())
    except ET.ParseError:
        return None


def _child_names(name):
    return [f"{name}-nw", f"{name}-ne", f"{name}-sw", f"{name}-se"]


def _split_quad(name, south, west, north, east):
    mid_lon = (west + east) / 2.0
    mid_lat = (south + north) / 2.0
    return [
        (f"{name}-nw", mid_lat, west, north, mid_lon),
        (f"{name}-ne", mid_lat, mid_lon, north, east),
        (f"{name}-sw", south, west, mid_lat, mid_lon),
        (f"{name}-se", south, mid_lon, mid_lat, east),
    ]


def _iter_grid_batches(south, west, north, east, n):
    dlon = (east - west) / n
    dlat = (north - south) / n
    batches = []
    for iy in range(n):
        for ix in range(n):
            b_west = west + ix * dlon
            b_east = east if ix == n - 1 else west + (ix + 1) * dlon
            b_south = south + iy * dlat
            b_north = north if iy == n - 1 else south + (iy + 1) * dlat
            batches.append((f"{ix}-{iy}", b_south, b_west, b_north, b_east))
    return batches


def _any_descendant_cache(cache_dir, name, level):
    if os.path.isfile(_batch_xml_path(cache_dir, name)):
        return True
    if level >= _OSM_MAX_SUBDIVIDE_LEVEL:
        return False
    return any(
        _any_descendant_cache(cache_dir, child, level + 1)
        for child in _child_names(name)
    )


def _wait_inter_batch_gap():
    if not _last_overpass_success:
        return
    elapsed = time.monotonic() - _last_overpass_success
    if elapsed < _OVERPASS_BATCH_GAP_SEC:
        time.sleep(_OVERPASS_BATCH_GAP_SEC - elapsed)


def overpass_download(south, west, north, east, timeout, abort=True):
    global _last_overpass_success
    endpoint = overpass_url.rstrip("/")
    url = endpoint + "?data=" + requests.utils.quote(
        overpass_ql(south, west, north, east)
    )
    _wait_inter_batch_gap()
    backoff = 5
    for retries in range(10):
        try:
            r = requests.get(
                url,
                allow_redirects=True,
                stream=True,
                headers=_OVERPASS_HTTP_HEADERS,
                timeout=timeout,
            )
        except requests.exceptions.RequestException as e:
            print_verbose("Overpass network error:", e)
            print_debug("Overpass network error:", e)
            run_stats.note_warning()
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        if r.status_code != 200:
            wait = backoff
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(1, int(retry_after))
                    except ValueError:
                        wait = 30
                else:
                    wait = 30
            else:
                backoff = min(backoff * 2, 30)
            print_verbose("Overpass HTTP", r.status_code, "- retrying")
            print_debug("Overpass HTTP", r.status_code, "- retrying")
            run_stats.note_warning()
            time.sleep(wait)
            continue

        raw = r.content
        try:
            osm_root = ET.fromstring(raw)
        except ET.ParseError as exc:
            print_verbose("Overpass returned invalid XML, retrying:", exc)
            print_debug("Overpass returned invalid XML, retrying:", exc)
            run_stats.note_warning()
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if osm_root.find('meta') is not None:
            _last_overpass_success = time.monotonic()
            return osm_root, raw
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)
    if abort:
        print("Overpass request failed after 10 attempts, aborting", file=sys.stderr)
        sys.exit(1)
    print_verbose("Overpass request failed after 10 attempts")
    return None


def overpass_request(lat_ul_merc, lon_ul_merc, lat_lr_merc, lon_lr_merc, timeout=60):
    lat_lr = y2lat(lat_lr_merc)
    lon_ul = x2lon(lon_ul_merc)
    lat_ul = y2lat(lat_ul_merc)
    lon_lr = x2lon(lon_lr_merc)
    osm_root, _raw = overpass_download(lat_lr, lon_ul, lat_ul, lon_lr, timeout, abort=True)
    return osm_root


def merge_osm_roots(roots):
    combined = ET.Element("osm")
    ET.SubElement(combined, "meta")
    ways = {}
    rels = {}
    for root in roots:
        for way in root.findall("way"):
            wid = way.attrib.get("id")
            if wid not in ways:
                ways[wid] = way
        for rel in root.findall("relation"):
            rid = rel.attrib.get("id")
            if rid not in rels:
                rels[rid] = rel
            else:
                seen = {
                    (member.get("type"), member.get("ref"), member.get("role"))
                    for member in rels[rid].findall("member")
                }
                for member in list(rel.findall("member")):
                    key = (member.get("type"), member.get("ref"), member.get("role"))
                    if key not in seen:
                        rels[rid].append(member)
                        seen.add(key)
    for way in ways.values():
        combined.append(way)
    for rel in rels.values():
        combined.append(rel)
    return combined


def ensure_osm_batch(cache_dir, name, south, west, north, east, level, refresh, batch_label=None):
    path = _batch_xml_path(cache_dir, name)
    label = batch_label or name

    if not refresh:
        cached = _read_cached_osm(path)
        if cached is not None:
            print_verbose("Using cached OSM batch:", path)
            return [cached]
        if level < _OSM_MAX_SUBDIVIDE_LEVEL and _any_descendant_cache(cache_dir, name, level):
            roots = []
            for child_name, c_south, c_west, c_north, c_east in _split_quad(
                name, south, west, north, east
            ):
                roots.extend(ensure_osm_batch(
                    cache_dir, child_name, c_south, c_west, c_north, c_east,
                    level + 1, refresh, batch_label=None,
                ))
            return roots

    print_verbose(f"Downloading OSM batch {label}...")
    result = overpass_download(south, west, north, east, timeout=300, abort=False)
    if result is not None:
        osm_root, raw = result
        _atomic_write(path, raw)
        return [osm_root]

    if level < _OSM_MAX_SUBDIVIDE_LEVEL:
        print_verbose(f"OSM batch failed; subdividing bbox at level {level + 1}")
        roots = []
        for child_name, c_south, c_west, c_north, c_east in _split_quad(
            name, south, west, north, east
        ):
            roots.extend(ensure_osm_batch(
                cache_dir, child_name, c_south, c_west, c_north, c_east,
                level + 1, refresh, batch_label=None,
            ))
        return roots

    print(
        "Overpass batch failed after retries and maximum subdivision. "
        f"BBox: south={south}, west={west}, north={north}, east={east}",
        file=sys.stderr,
    )
    sys.exit(1)


def load_area_osm_index(area_path, south, west, north, east, refresh):
    cache_dir = osm_cache_dir(area_path, south, west, north, east)
    os.makedirs(cache_dir, exist_ok=True)
    batches = _iter_grid_batches(south, west, north, east, _OSM_BATCH_GRID)
    print_verbose(f"Preparing OSM batches: {len(batches)}")
    roots = []
    for i, (name, b_south, b_west, b_north, b_east) in enumerate(batches, start=1):
        label = f"{i}/{len(batches)}"
        print_verbose(
            f"OSM batch {label}: bbox {b_south}, {b_west}, {b_north}, {b_east}"
        )
        roots.extend(ensure_osm_batch(
            cache_dir, name, b_south, b_west, b_north, b_east,
            0, refresh, batch_label=label,
        ))
    print_verbose(f"Loaded OSM data from {len(roots)} batches")
    merged = merge_osm_roots(roots)
    osm_index = parse_osm_root(merged)
    run_stats.osm_source = "Overpass"
    run_stats.osm_ways = osm_index.n_ways
    run_stats.osm_relations = osm_index.n_relations
    run_stats.osm_index_objects = len(osm_index.items)
    print_verbose(f"Built spatial index with {len(osm_index.items)} drawing objects")
    return osm_index


def _analysis_bbox_merc(south, west, north, east):
    return box(lon2x(west), lat2y(south), lon2x(east), lat2y(north))


def _coords_intersect_bbox(coords, bbox_merc):
    envelope = _envelope_from_coords(coords)
    return envelope is not None and envelope.intersects(bbox_merc)


def _xml_local_name(tag):
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _iterparse_osm_elements(path, wanted):
    wanted = set(wanted)
    context = ET.iterparse(path, events=("end",))
    for _event, elem in context:
        tag = _xml_local_name(elem.tag)
        if tag in wanted:
            yield tag, elem
        if tag in ("node", "way", "relation"):
            elem.clear()


def _way_node_refs(elem):
    return [nd.attrib.get("ref", "") for nd in elem.findall("nd")]


def load_osm_index_from_xml(osm_path, south, west, north, east):
    if not os.path.isfile(osm_path):
        print(f"OSM XML file not found: {osm_path}", file=sys.stderr)
        sys.exit(1)
    print_verbose("Loading local OSM XML:", osm_path)
    bbox_merc = _analysis_bbox_merc(south, west, north, east)

    member_way_ids = set()
    relations = []
    construction_relations = []
    for _tag, elem in _iterparse_osm_elements(osm_path, ("relation",)):
        pairs = _xml_tag_pairs(elem)
        is_relevant = osm_tags_match_pairs(pairs)
        is_construction = collect_diagnostic_osm and _is_construction_pairs(pairs)
        if not is_relevant and not is_construction:
            continue
        osm_id = elem.attrib.get("id", "")
        members = []
        for member in elem.findall("member"):
            if member.attrib.get("type") != "way":
                continue
            ref = member.attrib.get("ref", "")
            members.append((ref, member.attrib.get("role", "")))
            member_way_ids.add(ref)
        if is_relevant:
            relations.append((osm_id, pairs, members))
        if is_construction:
            construction_relations.append((osm_id, pairs, members))

    needed_node_ids = set()
    matching_ways = {}
    member_way_refs = {}
    construction_ways = {}
    for _tag, elem in _iterparse_osm_elements(osm_path, ("way",)):
        osm_id = elem.attrib.get("id", "")
        pairs = _xml_tag_pairs(elem)
        node_refs = _way_node_refs(elem)
        matches = osm_tags_match_pairs(pairs)
        is_construction = collect_diagnostic_osm and _is_construction_pairs(pairs)
        if matches or is_construction or osm_id in member_way_ids:
            needed_node_ids.update(node_refs)
        if matches:
            matching_ways[osm_id] = (pairs, node_refs)
        elif osm_id in member_way_ids:
            member_way_refs[osm_id] = node_refs
        if is_construction:
            construction_ways[osm_id] = (pairs, node_refs)

    node_coords = {}
    for _tag, elem in _iterparse_osm_elements(osm_path, ("node",)):
        osm_id = elem.attrib.get("id", "")
        if osm_id not in needed_node_ids:
            continue
        lon = elem.attrib.get("lon")
        lat = elem.attrib.get("lat")
        if lon is None or lat is None:
            continue
        node_coords[osm_id] = (lon2x(float(lon)), lat2y(float(lat)))

    def refs_to_coords(node_refs):
        coords = []
        for ref in node_refs:
            point = node_coords.get(ref)
            if point is not None:
                coords.append(point)
        return coords

    way_geom = {}
    way_records = []
    for osm_id, (pairs, node_refs) in matching_ways.items():
        coords = refs_to_coords(node_refs)
        way_geom[osm_id] = coords
        if not coords or not _coords_intersect_bbox(coords, bbox_merc):
            continue
        way_records.append((osm_id, pairs, coords))
    for osm_id, node_refs in member_way_refs.items():
        way_geom[osm_id] = refs_to_coords(node_refs)

    relation_records = []
    for rel_id, tags, members in relations:
        way_members = []
        intersects = False
        for ref, role in members:
            coords = way_geom.get(ref)
            if not coords:
                continue
            if _coords_intersect_bbox(coords, bbox_merc):
                intersects = True
            way_members.append((role, coords))
        if not intersects or not way_members:
            continue
        relation_records.append((rel_id, tags, way_members))

    osm_index = build_osm_index(way_records, relation_records)
    if collect_diagnostic_osm:
        construction_items = []
        for osm_id, pairs, node_refs in (
            (wid, pdata[0], pdata[1]) for wid, pdata in construction_ways.items()
        ):
            coords = way_geom.get(osm_id)
            if coords is None:
                coords = refs_to_coords(node_refs)
            if not coords or not _coords_intersect_bbox(coords, bbox_merc):
                continue
            construction_items.extend(_draw_items_from_way(osm_id, pairs, coords))
        for rel_id, tags, members in construction_relations:
            way_members = []
            intersects = False
            for ref, role in members:
                coords = way_geom.get(ref)
                if not coords:
                    continue
                if _coords_intersect_bbox(coords, bbox_merc):
                    intersects = True
                way_members.append((role, coords))
            if not intersects or not way_members:
                continue
            construction_items.extend(_draw_items_from_relation(rel_id, tags, way_members))
        osm_index.construction_items = construction_items
        print_verbose(
            f"Diagnostic construction objects: {len(construction_items)} "
            f"(highway=construction is also masked as existing OSM geometry)"
        )
    run_stats.osm_source = f"local XML ({os.path.basename(osm_path)})"
    run_stats.osm_ways = osm_index.n_ways
    run_stats.osm_relations = osm_index.n_relations
    run_stats.osm_index_objects = len(osm_index.items)
    print_verbose(
        f"Loaded {osm_index.n_ways} relevant ways and {osm_index.n_relations} relevant relations"
    )
    print_verbose(f"Built spatial index with {len(osm_index.items)} drawing objects")
    return osm_index


def compute_area_tile_range_and_osm_bbox(bbox_area, zoom, distance):
    (xul, yul) = deg2num(bbox_area[1], bbox_area[0], zoom)
    (xlr, ylr) = deg2num(bbox_area[3], bbox_area[2], zoom)
    xlr = xlr + 1
    ylr = ylr - 1
    lat_ul, lon_ul = num2deg(xul, ylr + 1, zoom)
    lat_lr, lon_lr = num2deg(xlr, yul + 1, zoom)
    north_m = lat2y(lat_ul) + distance
    west_m = lon2x(lon_ul) - distance
    south_m = lat2y(lat_lr) - distance
    east_m = lon2x(lon_lr) + distance
    osm_bbox = (y2lat(south_m), x2lon(west_m), y2lat(north_m), x2lon(east_m))
    return (xul, yul, xlr, ylr), osm_bbox


def osm_bbox_for_tile(tile_x, tile_y, zoom, distance):
    lat_ul_merc, lon_ul_merc, lat_lr_merc, lon_lr_merc = get_merc_bbox(tile_x, tile_y, zoom)
    return (
        y2lat(lat_lr_merc - distance),
        x2lon(lon_ul_merc - distance),
        y2lat(lat_ul_merc + distance),
        x2lon(lon_lr_merc + distance),
    )


def _polygonal_parts(geom):
    if geom is None or geom.is_empty:
        return []
    gtype = geom.geom_type
    if gtype == "Polygon":
        return [geom]
    if gtype == "MultiPolygon":
        return list(geom.geoms)
    if gtype == "GeometryCollection":
        parts = []
        for part in geom.geoms:
            parts.extend(_polygonal_parts(part))
        return parts
    return []


def load_area_polygon(area_path):
    # The GeoJSON polygon/multipolygon is authoritative for candidate inclusion.
    # Holes are preserved. Do not approximate this with a bounding box.
    with open(area_path, encoding="utf-8") as f:
        payload = json.load(f)
    features = []
    payload_type = payload.get("type")
    if payload_type == "FeatureCollection":
        features = payload.get("features") or []
    elif payload_type == "Feature":
        features = [payload]
    else:
        features = [{"geometry": payload}]
    geoms = []
    for feature in features:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if geometry is None:
            continue
        geom = shape(geometry).buffer(0)
        geoms.extend(_polygonal_parts(geom))
    if not geoms:
        raise ValueError(f"No polygon/multipolygon geometry in {area_path}")
    merged = unary_union(geoms)
    polygonal = _polygonal_parts(merged)
    if not polygonal:
        raise ValueError(f"No polygon/multipolygon geometry in {area_path}")
    return unary_union(polygonal) if len(polygonal) > 1 else polygonal[0]


def set_requested_area(geom):
    global requested_area, requested_area_prepared
    requested_area = geom
    requested_area_prepared = prep(geom) if geom is not None else None


def candidate_inside_requested_area(lon, lat):
    # covers() keeps a point exactly on the polygon boundary.
    # Without -a (single-tile debug), every candidate is treated as in-area.
    if requested_area_prepared is None:
        return True
    return bool(requested_area_prepared.covers(Point(lon, lat)))


def plot_osm_items(items, draw, width, pixel_size):
    tile_bbox = get_merc_bbox(x, y, zoom)
    for item in items:
        if item.fill_polygon:
            plot_polygon(draw, item.coords, tile_bbox, pixel_size)
        plot_line(draw, item.coords, tile_bbox, width, pixel_size)
        plot_circle(draw, item.coords, tile_bbox, width, pixel_size)


def setup_diagnostic_osm(loaded_index):
    global diagnostic_osm
    need_lookup = (
        diagnostic_writer is not None or suppress_parallel_osm or suppress_ferry
    )
    if not need_lookup or loaded_index is None:
        diagnostic_osm = None
        return
    construction_items = getattr(loaded_index, "construction_items", None) or []
    diagnostic_osm = DiagnosticOsmLookup(loaded_index.items, construction_items)
    print_verbose(
        f"Diagnostic OSM lookup: {len(loaded_index.items)} relevant objects, "
        f"{len(construction_items)} construction objects"
    )

# This routine check if a strava heatmap tile contains a way not in OSM
# ---------------------------------------------------------------------
def check_strava_tile(polygon_area, x, y, zoom):
    # Get bounding box of strava tile in geographical coordinates
    (lat_ul, lon_ul, lat_lr, lon_lr) = get_geo_bbox(x, y, zoom)

    # Create a polygon for the bounding box
    polygon_strava = Polygon(((lon_ul, lat_ul), (lon_lr, lat_ul),
                             (lon_lr, lat_lr), (lon_ul, lat_lr), (lon_ul, lat_ul)))

    # Checks if the tile is in the area
    if polygon_area is None or polygon_strava.intersects(polygon_area):
        run_stats.note_tile_progress(x, y, zoom)
        strava_tile = fetch_strava_tile(zoom, x, y)         # Get Strava tile
        if strava_tile is None:
            run_stats.tiles_empty += 1
            return
        try:
            image = Image.open(strava_tile)
            if strava_tile_backend == "strava":
                image = image.convert("L")
        except Exception:
            print(f"Warning: Invalid Strava tile {strava_tile}", file=sys.stderr)
            run_stats.note_warning()
            run_stats.tiles_failed += 1
            os.remove(strava_tile)
            return
        run_stats.tiles_processed += 1
        data = np.array(image)
        maximum = np.max(data)
        if maximum < threshold:
            if maximum == 0:
                run_stats.tiles_empty += 1
            else:
                run_stats.tiles_below_threshold += 1
            print_verbose("No Strava pixels above threshold, skipping Overpass")
            return
        draw = ImageDraw.Draw(image)

#        if debug:
#            # Fill with white pixels to display the mask
#            draw.rectangle((0,0,511,511), fill=255, outline=255)

        # Get bounding box of strava tile in Mercator coordinates
        bbox_merc = get_merc_bbox(x, y, zoom)
        (lat_ul_merc, lon_ul_merc, lat_lr_merc, lon_lr_merc) = bbox_merc
        pixel_size = (lat_ul_merc - lat_lr_merc) / image.size[0]
        print_debug("Pixel size =", pixel_size)
        width = round(distance / pixel_size) * 2 + 1
        print_debug("Line width =", width)

        query_env = box(
            lon_ul_merc - distance,
            lat_lr_merc - distance,
            lon_lr_merc + distance,
            lat_ul_merc + distance,
        )
        if osm_index is not None:
            osm_items = osm_index.query(query_env)
            n_ways = sum(1 for item in osm_items if item.source == "way")
            n_rels = len({item.osm_id for item in osm_items if item.source == "relation"})
            print_verbose(f"OSM candidates for tile: {n_ways} ways, {n_rels} relations")
        else:
            osm_root = overpass_request(
                lat_ul_merc + distance, lon_ul_merc - distance,
                lat_lr_merc - distance, lon_lr_merc + distance,
            )
            osm_items = parse_osm_root(osm_root).items

        # Keep the raw Strava tile for diagnostic corridor sampling. Detection
        # continues to use the OSM-masked image below; this does not change
        # masking, thresholds, or candidate IDs.
        heatmap_unmasked = np.array(image) if diagnostic_writer is not None else None
        plot_osm_items(osm_items, draw, width, pixel_size)

        if debug:
            image.save(f"test_{zoom}_{x}_{y}.png")  # For debugging

        data = np.array(image)
        tile_had_accepted = False
        tile_lookup = diagnostic_osm
        need_component_geometry = diagnostic_writer is not None or suppress_parallel_osm
        need_lookup = need_component_geometry or suppress_ferry
        if need_lookup and tile_lookup is None:
            tile_lookup = DiagnosticOsmLookup(osm_items, [])
        candidate_index = 0
        # Loop while the lighter pixel in the Strava tile is above the threshold
        while np.max(data) >= threshold:
            maximum = np.max(data)
            max_index = np.unravel_index(np.argmax(data), data.shape)
            result = reverse_transform(max_index, bbox_merc, pixel_size)
            heatmap_snapshot = data.copy() if need_component_geometry else None
            size = 0
            size = check_trace_area(data, max_index[0], max_index[1], threshold, min_size, size)
            run_stats.detections_raw += 1
            written_to_geojson = False
            suppressed_parallel = False
            suppressed_ferry_flag = False
            too_small = size <= min_size
            inside_area = candidate_inside_requested_area(result[0], result[1])
            accepted = (not too_small) and inside_area
            if too_small:
                run_stats.detections_too_small += 1
            elif not inside_area:
                run_stats.detections_outside_area += 1
                print_verbose(
                    f"Rejected outside area: "
                    f"{zoom}/{x}/{y}/{int(max_index[0])}/{int(max_index[1])}"
                )
            else:
                # Accepted: passed size AND requested-area tests.
                # Suppression runs only on accepted in-area candidates.
                run_stats.detections_accepted += 1
                tile_had_accepted = True
                if suppress_parallel_osm:
                    try:
                        follow_metrics = component_osm_follow_metrics(
                            heatmap_snapshot,
                            int(max_index[0]),
                            int(max_index[1]),
                            threshold,
                            bbox_merc,
                            pixel_size,
                            tile_lookup,
                        )
                        suppressed_parallel = should_suppress_parallel_osm(follow_metrics)
                    except Exception as exc:
                        print(
                            f"Warning: parallel-OSM suppression failed: {exc}",
                            file=sys.stderr,
                        )
                        run_stats.note_warning()
                        suppressed_parallel = False
                    if suppressed_parallel:
                        run_stats.parallel_osm_suppressed += 1
                        print_verbose(
                            f"Suppressed parallel OSM: "
                            f"{zoom}/{x}/{y}/{int(max_index[0])}/{int(max_index[1])}"
                        )
                if suppress_ferry:
                    try:
                        ferry_dist = nearest_ferry_distance_m(
                            tile_lookup, lon2x(result[0]), lat2y(result[1])
                        )
                        suppressed_ferry_flag = should_suppress_ferry(ferry_dist)
                    except Exception as exc:
                        print(
                            f"Warning: ferry suppression failed: {exc}",
                            file=sys.stderr,
                        )
                        run_stats.note_warning()
                        suppressed_ferry_flag = False
                    if suppressed_ferry_flag:
                        run_stats.ferry_suppressed += 1
                        print_verbose(
                            f"Suppressed ferry: "
                            f"{zoom}/{x}/{y}/{int(max_index[0])}/{int(max_index[1])}"
                        )
                if suppressed_parallel and suppressed_ferry_flag:
                    run_stats.suppression_overlap += 1
                if suppressed_parallel or suppressed_ferry_flag:
                    run_stats.total_suppressed += 1
                # print(f"geo:{result[1]},{result[0]}?z={zoom}")
                print_verbose(f"https://www.openstreetmap.org/?mlat={result[1]}&"
                              f"mlon={result[0]}#map={zoom}/{result[1]}/{result[0]}&layers=N")

                id = f"{zoom}/{x}/{y}/{max_index[0]}/{max_index[1]}"   # Unique ID for the MR task
                status = ""
                if tasks_db is not None:
                    # Check if this task has already been processed
                    res = cur.execute("SELECT TaskStatus,Mapper,TaskLink FROM tasks "
                                      f"WHERE TaskName='{id}'").fetchone()
                    if res is not None:
                        status = res[0]
                        print_verbose(status, ":", res[2][21:-2])

                if status == "Fixed" or status == "Already_Fixed":
                    print(f"Warning: This task has been marked as fixed by {res[1]},"
                          f" but it seems it is not: {res[2][21:-2]}/inspect", file=sys.stderr)
                    run_stats.note_warning()

                if (
                    not suppressed_parallel
                    and not suppressed_ferry_flag
                    and status != "Too_Hard"
                    and status != "Not_an_Issue"
                ):
                    run_stats.geojson_features += 1
                    written_to_geojson = True
                    # print GEOJSON line for MapRoulette
                    RS = chr(30)  # Record Separator ASCII control character
                    if diagnostic_writer is not None:
                        print(f'{RS}{{"type":"FeatureCollection","features":[{{"type":"Feature",'
                              f'"geometry":{{"type":"Point","coordinates":[{result[0]}, {result[1]}]}},'
                              f'"properties":{{"id":"{id}","candidate_id":"{id}","longitude":"{result[0]}",'
                              f'"latitude":"{result[1]}","distance":"{distance}",'
                              f'"threshold":"{threshold}","maximum":"{maximum}",'
                              f'"min_size":"{min_size}","size":"{size}"}}}}],'
                              f'"id":"{id}"}}', file=geojson_file)
                    else:
                        print(f'{RS}{{"type":"FeatureCollection","features":[{{"type":"Feature",'
                              f'"geometry":{{"type":"Point","coordinates":[{result[0]}, {result[1]}]}},'
                              f'"properties":{{"id":"{id}","longitude":"{result[0]}",'
                              f'"latitude":"{result[1]}","distance":"{distance}",'
                              f'"threshold":"{threshold}","maximum":"{maximum}",'
                              f'"min_size":"{min_size}","size":"{size}"}}}}],'
                              f'"id":"{id}"}}', file=geojson_file)

            if not too_small:
                # Flood fill to disable the leftover heatmap component, including
                # large traces rejected only because they lie outside the area.
                if debug:
                    image.save(f"before_flood_{zoom}_{x}_{y}.png")  # For debugging
                print_debug(x, y, max_index, maximum)
                ImageDraw.floodfill(image, (max_index[1], max_index[0]), 0, thresh=maximum - 1)
                data = np.array(image)
                if debug:
                    image.save(f"after_flood_{zoom}_{x}_{y}.png")  # For debugging
            if diagnostic_writer is not None:
                try:
                    row = build_diagnostic_row(
                        zoom=zoom,
                        tile_x=x,
                        tile_y=y,
                        candidate_index=candidate_index,
                        peak_row=int(max_index[0]),
                        peak_col=int(max_index[1]),
                        center_lon=result[0],
                        center_lat=result[1],
                        pixel_size_m=pixel_size,
                        heatmap_snapshot=heatmap_snapshot,
                        heatmap_unmasked=heatmap_unmasked,
                        threshold=threshold,
                        lookup=tile_lookup,
                        lon2x=lon2x,
                        lat2y=lat2y,
                        too_small=too_small,
                        accepted=accepted,
                        inside_area=inside_area,
                        written_to_geojson=written_to_geojson,
                        bbox_merc=bbox_merc,
                        suppressed_parallel_osm=suppressed_parallel,
                        suppressed_ferry=suppressed_ferry_flag,
                    )
                    diagnostic_writer.write_row(row)
                    run_stats.diagnostic_rows = diagnostic_writer.rows_written
                except Exception as exc:
                    print(f"Warning: diagnostics row failed: {exc}", file=sys.stderr)
                    run_stats.note_warning()
            candidate_index += 1
        if tile_had_accepted:
            run_stats.tiles_with_detection += 1


# Parse command line arguments
# ----------------------------
parser = argparse.ArgumentParser()

parser.add_argument(
    "-a", "--area",
    help="Area of interest (GeoJSON polygon/multipolygon). Tiles that intersect "
         "the area are processed; only candidate points inside the polygon are emitted.",
)
parser.add_argument("-m", "--minlevel", type=int, default=100,
                    help="Minimum Strava level (0-255)")
parser.add_argument("-d", "--distance", type=int, default=35,
                    help="Maximum distance between Strava hot point and OSM way")
parser.add_argument("-s", "--size", type=int, default=20,
                    help="Minimum size of Strava trace (in pixels)")
parser.add_argument("-z", "--zoom", type=int, default=15,
                    help="Strava zoom level (10-15)")
parser.add_argument("-c", "--activity", default='run',
                    help="Strava activity (default=run). Use e.g. 'all' for combined heatmap with --strava-tiles nakarte.")
parser.add_argument(
    "--strava-tiles",
    dest="strava_tile_backend",
    choices=("freemap", "nakarte", "strava"),
    default="freemap",
    help="Strava tile server: 'freemap' (default), 'nakarte' (proxy to heatmap-external-a.strava.com, px=256), or 'strava' (direct Global Heatmap, requires .strava-cookie).",
)
parser.add_argument("-o", "--offset", type=int,
                    help="Strava tile offset (0-3)")
parser.add_argument("-b", "--tasks_db",
                    help="Tasks database")
parser.add_argument("-g", "--geojson",
                    help="Output file")
parser.add_argument('-v', '--verbose', action='store_true',
                    help="Display more information")
parser.add_argument('-q', '--quiet', action='store_true',
                    help="Do not display progress")
parser.add_argument('-x', '--x', type=int,
                    help="Strava Tile x coordinate")
parser.add_argument('-y', '--y', type=int,
                    help="Strava Tile y coordinate")
parser.add_argument('--debug', action='store_true',
                    help="Debug mode")
parser.add_argument(
    "--refresh-osm",
    action="store_true",
    help="Ignore cached OSM data and download again from Overpass",
)
parser.add_argument(
    "--overpass-url",
    default=_OVERPASS_DEFAULT_URL,
    help="Overpass interpreter URL (default: https://overpass-api.de/api/interpreter)",
)
parser.add_argument(
    "--osm-file",
    help="Local OSM XML extract used instead of Overpass",
)
parser.add_argument(
    "--stats-json",
    metavar="FILE",
    help="Write run statistics as JSON to FILE",
)
parser.add_argument(
    "--diagnostics",
    metavar="FILE",
    help="Write per-candidate diagnostic CSV for calibration (does not change detection)",
)
parser.add_argument(
    "--suppress-parallel-osm",
    action="store_true",
    help=(
        "Opt-in: omit accepted candidates whose heatmap component follows local OSM "
        "(follow100>=0.70 and parallel15>=0.70). Default off; does not change masking "
        "or detection thresholds."
    ),
)
parser.add_argument(
    "--suppress-ferry",
    action="store_true",
    help=(
        "Opt-in: omit accepted candidates whose peak is within 500 m of a route=ferry "
        "object. Default off; candidate-level only, does not widen the OSM mask buffer."
    ),
)

args = parser.parse_args()

verbose = args.verbose
debug = args.debug
distance = args.distance
print_verbose("Maximum distance = ", distance)
threshold = args.minlevel
print_verbose("Threshold = ", threshold)
zoom = args.zoom
min_size = args.size
print_verbose("Minimum size = ", min_size)
activity = args.activity
print_verbose("Activity = ", activity)
strava_tile_backend = args.strava_tile_backend
print_verbose("Strava tiles =", strava_tile_backend)
if args.osm_file:
    print_verbose("OSM file =", args.osm_file)
else:
    overpass_url = args.overpass_url
    print_verbose("Overpass URL =", overpass_url)
strava_cookie = None
if strava_tile_backend == "strava":
    if activity != "ride":
        print(
            "Error: --strava-tiles strava currently supports only -c ride.",
            file=sys.stderr,
        )
        sys.exit(1)
    strava_cookie = load_strava_cookie()
tasks_db = args.tasks_db
osm_index = None
con = None

run_stats.activity = activity
run_stats.strava_backend = strava_tile_backend
run_stats.zoom = zoom
run_stats.threshold = threshold
run_stats.min_size = min_size
run_stats.distance = distance
run_stats.offset = args.offset
if args.area:
    run_stats.area = os.path.basename(args.area)
if args.x is not None and args.y is not None:
    run_stats.tile = f"{zoom}/{args.x}/{args.y}"
run_stats.output = args.geojson
if args.diagnostics:
    collect_diagnostic_osm = True
    diagnostic_writer = DiagnosticWriter(args.diagnostics)
    run_stats.diagnostics_path = args.diagnostics
    print_verbose("Diagnostics =", args.diagnostics)
suppress_parallel_osm = bool(args.suppress_parallel_osm)
run_stats.suppress_parallel_osm = suppress_parallel_osm
if suppress_parallel_osm:
    print_verbose("Suppress parallel OSM = on (follow100>=0.70 and parallel15>=0.70)")
suppress_ferry = bool(args.suppress_ferry)
run_stats.suppress_ferry = suppress_ferry
if suppress_ferry:
    print_verbose("Suppress ferry = on (nearest_ferry_distance_m<=500)")

# Create output file
if args.geojson is not None:
    geojson_file = open(args.geojson, "w")
else:
    geojson_file = sys.stdout

if tasks_db is not None:
    # Create connection to "not_an_issue" database, and create a cursor
    con = sqlite3.connect(f"file:{tasks_db}?mode=ro", uri=True)
    cur = con.cursor()

if args.x is not None or args.y is not None:
    if args.x is None or args.y is None:
        print("Error: you must provide both x and y tile coordinates")
        sys.exit(1)

if args.x is None and args.area is None:
    print("Error: you must provide either an area, either tile coordinates")
    sys.exit(1)

run_stats.run_started = time.perf_counter()
run_stats.started_at = datetime.now()
try:
    def _interrupt_handler(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _interrupt_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _interrupt_handler)
    if args.x is not None and args.y is not None:
        x = args.x
        y = args.y
        polygon_area = None
        if args.area is not None:
            polygon_area = load_area_polygon(args.area)
            set_requested_area(polygon_area)
        if args.osm_file:
            if polygon_area is not None:
                _range, osm_bbox = compute_area_tile_range_and_osm_bbox(
                    polygon_area.bounds, zoom, distance
                )
                osm_south, osm_west, osm_north, osm_east = osm_bbox
            else:
                osm_south, osm_west, osm_north, osm_east = osm_bbox_for_tile(
                    x, y, zoom, distance
                )
            osm_index = load_osm_index_from_xml(
                args.osm_file, osm_south, osm_west, osm_north, osm_east
            )
            setup_diagnostic_osm(osm_index)
        run_stats.tiles_total = 1
        check_strava_tile(None, x, y, zoom)
    else:
        # Get polygon of area. Tiles may cross the boundary; candidate points
        # are later tested against this geometry, not the bounding box.
        polygon_area = load_area_polygon(args.area)
        set_requested_area(polygon_area)
        bbox_area = polygon_area.bounds
        print_verbose("Area bounding box:", bbox_area)

        (xul, yul, xlr, ylr), (osm_south, osm_west, osm_north, osm_east) = (
            compute_area_tile_range_and_osm_bbox(bbox_area, zoom, distance)
        )
        if args.osm_file:
            osm_index = load_osm_index_from_xml(
                args.osm_file, osm_south, osm_west, osm_north, osm_east
            )
        else:
            osm_index = load_area_osm_index(
                args.area, osm_south, osm_west, osm_north, osm_east, args.refresh_osm
            )
        setup_diagnostic_osm(osm_index)

        if not verbose and not debug and args.geojson is not None and not args.quiet:
            progress = True
        else:
            progress = False

        offset_x = 0
        offset_y = 0
        if args.offset is not None:
            if args.offset == 1:
                offset_x = 0
                offset_y = 1
            elif args.offset == 2:
                offset_x = 1
                offset_y = 0
            elif args.offset == 3:
                offset_x = 1
                offset_y = 1
            step = 2
        else:
            step = 1

        run_stats.tiles_total = count_planned_strava_tiles(
            polygon_area, xul, yul, xlr, ylr, offset_x, offset_y, step, zoom
        )

        for x in range(xul + offset_x, xlr, step):
            if progress:
                print(".", end='', flush=True)
            for y in range(yul - offset_y, ylr, -step):
                print_debug(x, y)
                check_strava_tile(polygon_area, x, y, zoom)
            if progress:
                print("")
    run_stats.status = "SUCCESS"
except KeyboardInterrupt:
    run_stats.status = "INTERRUPTED"
    run_stats.exit_code = 130
    print("", file=sys.stderr)
except SystemExit as e:
    run_stats.exit_code = _system_exit_code(e)
    if run_stats.exit_code != 0:
        run_stats.status = "FAILED"
except Exception:
    run_stats.status = "FAILED"
    run_stats.exit_code = 1
    raise
finally:
    if diagnostic_writer is not None:
        try:
            diagnostic_writer.close()
            run_stats.diagnostic_rows = diagnostic_writer.rows_written
        except Exception:
            pass
    if args.geojson is not None:
        try:
            geojson_file.close()
        except Exception:
            pass
    if con is not None:
        try:
            con.close()
        except Exception:
            pass
    summary_stream = sys.stdout if args.geojson is not None else sys.stderr
    finalize_run(args.stats_json, summary_stream)

if run_stats.exit_code:
    sys.exit(run_stats.exit_code)