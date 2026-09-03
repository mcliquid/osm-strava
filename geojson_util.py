#!/usr/bin/env python3
"""Load and write detector GeoJSON without depending on MapRoulette or mr-cli.

The detector writes GeoJSONSeq (one FeatureCollection per line, optional RS).
JOSM and the workflow review files use a single RFC 7946 FeatureCollection.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

_RS = "\x1e"
_EARTH_M = 6_371_000.0


def load_features(path):
    """Load Point features from GeoJSONSeq or a standard FeatureCollection."""
    text = Path(path).read_text(encoding="utf-8")
    text = text.lstrip(_RS).strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict) and parsed.get("type") == "FeatureCollection":
        return list(parsed.get("features") or [])
    if isinstance(parsed, dict) and parsed.get("type") == "Feature":
        return [parsed]
    if isinstance(parsed, list):
        return parsed

    features = []
    for raw_line in text.splitlines():
        line = raw_line.lstrip(_RS).strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("type") == "FeatureCollection":
            features.extend(obj.get("features") or [])
        elif obj.get("type") == "Feature":
            features.append(obj)
        else:
            raise ValueError(f"Unsupported GeoJSON object in {path}")
    return features


def feature_id(feature):
    props = feature.get("properties") or {}
    return str(props.get("id") or props.get("candidate_id") or "")


def feature_lonlat(feature):
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    if len(coords) >= 2:
        return float(coords[0]), float(coords[1])
    return float(props["longitude"]), float(props["latitude"])


def annotate_provenance(features, *, region, layer, heatmap_layer):
    """Copy features and add source-layer provenance. Does not change ids."""
    out = []
    for feature in features:
        props = dict(feature.get("properties") or {})
        candidate_id = feature_id(feature)
        if candidate_id:
            props.setdefault("id", candidate_id)
            props.setdefault("candidate_id", candidate_id)
        props["region"] = region
        props["source_layer"] = layer
        props["source_heatmap"] = heatmap_layer
        props["workflow"] = "osm-strava"
        geom = feature.get("geometry")
        if geom is None:
            lon, lat = feature_lonlat(feature)
            geom = {"type": "Point", "coordinates": [lon, lat]}
        out.append({
            "type": "Feature",
            "geometry": geom,
            "properties": props,
        })
    return out


def write_feature_collection(path, features):
    """Write a single RFC 7946 FeatureCollection for JOSM."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    collection = {"type": "FeatureCollection", "features": list(features)}
    path.write_text(
        json.dumps(collection, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def write_geojsonl(path, features):
    """Write one single-feature FeatureCollection per line (no RS prefix)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for feature in features:
        fid = feature_id(feature)
        obj = {"type": "FeatureCollection", "features": [feature]}
        if fid:
            obj["id"] = fid
        lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def haversine_m(lon1, lat1, lon2, lat2):
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * _EARTH_M * math.asin(math.sqrt(a))


def nearest_distance_m(feature, others):
    lon, lat = feature_lonlat(feature)
    best = float("inf")
    best_id = None
    for other in others:
        olon, olat = feature_lonlat(other)
        dist = haversine_m(lon, lat, olon, olat)
        if dist < best:
            best = dist
            best_id = feature_id(other)
    return best, best_id


def filter_farther_than(features, others, threshold_m):
    """Keep features whose nearest other-layer candidate is farther than threshold_m."""
    kept = []
    removed = []
    for feature in features:
        dist, other_id = nearest_distance_m(feature, others) if others else (float("inf"), None)
        if dist > threshold_m:
            kept.append(feature)
        else:
            removed.append((feature, dist, other_id))
    return kept, removed
