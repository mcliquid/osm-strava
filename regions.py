#!/usr/bin/env python3
"""Parse osm-regions.conf. New regions are added there, not in Python code."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent
DEFAULT_CONF = REPO / "osm-regions.conf"

_REQUIRED = (
    "geofabrik_url",
    "source_pbf",
    "boundary",
    "extract_pbf",
    "extract_xml",
    "bbox_lon_min",
    "bbox_lon_max",
    "bbox_lat_min",
    "bbox_lat_max",
)

_LAYER_KEYS = {
    "ride": "challenge_ride",
    "run": "challenge_run",
    "all": "challenge_all",
}


def _abs_path(root, value):
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def load_all_regions(conf_path=None):
    conf_path = Path(conf_path) if conf_path else DEFAULT_CONF
    root = conf_path.parent
    regions = {}
    current = None
    with open(conf_path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1].strip()
                regions[current] = {"id": current}
                continue
            if current is None or "=" not in line:
                continue
            key, value = line.split("=", 1)
            regions[current][key.strip()] = value.strip()

    for region_id, data in regions.items():
        missing = [key for key in _REQUIRED if key not in data]
        if missing:
            raise ValueError(f"Region {region_id} fehlt: {', '.join(missing)}")
        display = data.get("display_name") or region_id.replace("_", " ").title()
        data["display_name"] = display
        data["boundary"] = str(_abs_path(root, data["boundary"]))
        data["extract_pbf"] = str(_abs_path(root, data["extract_pbf"]))
        data["extract_xml"] = str(_abs_path(root, data["extract_xml"]))
        data["source_pbf"] = str(_abs_path(root, data["source_pbf"]))
        data["challenge_names"] = {
            "ride": data.get("challenge_ride") or f"Strava {display} Ride",
            "run": data.get("challenge_run") or f"Strava {display} Run",
            "all": data.get("challenge_all") or f"Strava {display} All",
        }
        data["zoom"] = int(data.get("zoom") or 14)
    return regions


def load_region(region_id, conf_path=None):
    regions = load_all_regions(conf_path)
    if region_id not in regions:
        known = ", ".join(sorted(regions))
        raise KeyError(f"Unbekannte Region: {region_id}. Bekannt: {known}")
    return regions[region_id]


def list_region_ids(conf_path=None):
    return list(load_all_regions(conf_path).keys())
