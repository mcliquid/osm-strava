#!/usr/bin/env python3
"""One-command Strava detection workflow. Orchestrates existing tools.

    python workflow.py mallorca --dry-run
    python workflow.py mallorca --no-upload
    python workflow.py mallorca
    python workflow.py mallorca --phase ride-run
    python workflow.py mallorca --phase all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from geojson_util import (
    annotate_provenance,
    feature_id,
    filter_farther_than,
    load_features,
    write_feature_collection,
    write_geojsonl,
)
from maproulette import MapRouletteClient, MapRouletteConfig, MapRouletteError, load_api_key, redact
from regions import load_all_regions, load_region
from review_history import (
    history_exists,
    register_production_challenge,
    sync_review_history,
    tasks_db_path,
)

REPO = Path(__file__).resolve().parent
STATE_DIR = REPO / "state"

# Validated production settings. Do not change silently.
# Direct Strava tiles were validated at zoom 14; never fall back to
# strava.py's CLI default (15). Region config may set zoom, default 14.
DEFAULT_ZOOM = 14
THRESHOLD = 100
DISTANCE_M = 35
MINSIZE = 20
ALL_DEDUPE_M = 25
STRAVA_TILES = "strava"

LAYERS = ("ride", "run", "all")
HEATMAP = {
    "ride": "sport_Ride",
    "run": "sport_Run",
    "all": "all",
}
LAYER_LABEL = {
    "ride": "Ride",
    "run": "Run",
    "all": "All",
}

PHASE_LAYERS = {
    "oneshot": ("ride", "run", "all"),
    "ride-run": ("ride", "run"),
    "all": ("all",),
}
UPLOAD_LAYERS = {
    "oneshot": ("ride", "run", "all"),
    "ride-run": ("ride", "run"),
    "all": ("all",),
}


class WorkflowError(Exception):
    """German, operator-facing workflow failure."""


def utc_now():
    return datetime.now(timezone.utc)


def utc_stamp():
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def iso_now():
    return utc_now().replace(microsecond=0).isoformat()


def ensure_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def osm_mtime(extract_xml):
    path = Path(extract_xml)
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat()


def artifact(run_dir, filename):
    """Plain filename in the run directory. Avoid pathlib splitting ride.raw.geojson."""
    return os.path.normpath(os.path.join(str(run_dir), filename))


def region_zoom(region):
    try:
        zoom = int(region.get("zoom") or DEFAULT_ZOOM)
    except (TypeError, ValueError):
        zoom = DEFAULT_ZOOM
    return zoom


def working_state_path(region):
    source = Path(region["source_pbf"])
    name = source.name
    if name.endswith(".osm.pbf"):
        name = f"{name[:-8]}-working.state"
    else:
        name = f"{name}-working.state"
    return source.with_name(name)


def read_key_values(path):
    data = {}
    path = Path(path)
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _search_line(pattern, text):
    match = re.search(pattern, text, re.M)
    return match.group(1).strip() if match else None


def parse_osm_update_output(text):
    info = {
        "mode": _search_line(r"^Update mode:\s+(\S+)", text),
        "status": _search_line(r"^Status:\s+(\S+)", text),
        "timestamp_before": _search_line(r"^Extract before:\s+timestamp=(\S+)", text),
        "replication_start": _search_line(r"^Replication start:\s+(\S+)", text),
        "replication_result": _search_line(r"^Replication result:\s+(\S+)", text),
        "planet_timestamp": _search_line(r"^Planet minutely:\s+(\S+)", text),
        "source_lag": _search_line(r"^Source lag:\s+(.+)$", text),
        "lag_behind_planet": _search_line(r"^Lag behind planet:\s+(\S+)", text),
    }
    info["timestamp_after"] = info.get("replication_result") or _search_line(
        r"^Source timestamp:\s+(\S+)", text
    )
    nodes = re.search(r"^Nodes:\s+([\d,]+)\s+->\s+([\d,]+)", text, re.M)
    if nodes:
        info["nodes_before"] = nodes.group(1)
        info["nodes_after"] = nodes.group(2)
    ways = re.search(r"^Ways:\s+([\d,]+)\s+->\s+([\d,]+)", text, re.M)
    if ways:
        info["ways_before"] = ways.group(1)
        info["ways_after"] = ways.group(2)
    return {key: value for key, value in info.items() if value}


def find_source_geojson(directory, region_id, layer):
    directory = Path(directory)
    candidates = [
        directory / f"{region_id}-fresh-{layer}.geojson",
        directory / f"{layer}.geojson",
        directory / f"{region_id}-{layer}.geojson",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


class Progress:
    def __init__(self, callback=None):
        self.callback = callback
        self.stages = []
        self.current = None
        self.failed_stage = None
        self.logs = []

    def emit(self, **payload):
        if self.callback:
            self.callback(payload)

    def log(self, message):
        line = redact(message)
        self.logs.append(line)
        print(line, flush=True)
        self.emit(type="log", message=line)

    def start(self, stage_id, label):
        self.current = {"id": stage_id, "label": label, "status": "running"}
        self.emit(type="stage", **self.current)
        self.log(label)

    def finish(self, detail=None, counts=None):
        if self.current is None:
            return
        self.current["status"] = "done"
        if detail:
            self.current["detail"] = detail
        if counts:
            self.current["counts"] = counts
        self.stages.append(dict(self.current))
        self.emit(type="stage", **self.current)
        self.current = None

    def fail(self, message):
        self.failed_stage = (self.current or {}).get("id")
        if self.current is not None:
            self.current["status"] = "error"
            self.current["detail"] = message
            self.stages.append(dict(self.current))
            self.emit(type="stage", **self.current)
            self.current = None
        self.log(f"FEHLER: {message}")


def detector_command(region, layer, raw_path, stats_path, zoom, tasks_db=None):
    cmd = [
        sys.executable,
        str(REPO / "strava.py"),
        "-c", layer,
        "--strava-tiles", STRAVA_TILES,
        "-z", str(zoom),
        "-m", str(THRESHOLD),
        "-d", str(DISTANCE_M),
        "-s", str(MINSIZE),
        "-a", region["boundary"],
        "--osm-file", region["extract_xml"],
        "-g", os.path.normpath(str(raw_path)),
        "--stats-json", os.path.normpath(str(stats_path)),
        "--suppress-parallel-osm",
        "--suppress-ferry",
        "--suppress-heat-halo",
        "-q",
    ]
    if layer == "all":
        cmd.append("--suppress-golf")
    if tasks_db and Path(tasks_db).exists():
        cmd.extend(["-b", os.path.normpath(str(tasks_db))])
    return cmd


def osm_update_command(region_id, *, fresh=True):
    extra = ["--fresh"] if fresh else []
    if os.name == "nt":
        return [
            "powershell",
            "-ExecutionPolicy", "Bypass",
            "-File", str(REPO / "update-osm.ps1"),
            region_id,
            *extra,
        ]
    return ["bash", str(REPO / "update-osm.sh"), region_id, *extra]


def run_osm_update(region_id, progress, *, fresh=True, log_path=None):
    cmd = osm_update_command(region_id, fresh=fresh)
    progress.log("OSM-Update: " + " ".join(cmd))
    if fresh:
        progress.log("OSM-Modus: fresh (planet minutely)")
    else:
        progress.log("OSM-Modus: geofabrik (Debug — nicht Produktion)")
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as log_file:
            result = subprocess.run(
                cmd,
                cwd=str(REPO),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        output = Path(log_path).read_text(encoding="utf-8", errors="replace")
    else:
        result = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
        output = (result.stdout or "") + (result.stderr or "")
    parsed = parse_osm_update_output(output)
    if parsed.get("mode"):
        progress.log(f"Updater-Modus: {parsed['mode']}")
    if parsed.get("timestamp_before"):
        progress.log(f"OSM vorher: {parsed['timestamp_before']}")
    if parsed.get("timestamp_after"):
        progress.log(f"OSM nachher: {parsed['timestamp_after']}")
    if parsed.get("source_lag"):
        progress.log(f"OSM-Lag: {parsed['source_lag']}")
    if parsed.get("lag_behind_planet"):
        progress.log(f"Lag hinter Planet: {parsed['lag_behind_planet']}")
    if result.returncode != 0:
        err = output.strip() or "unbekannter Fehler"
        raise WorkflowError(f"OSM-Update fehlgeschlagen: {redact(err[-800:])}")
    return parsed, output


def run_detector(region, layer, raw_path, stats_path, progress, zoom, tasks_db=None):
    cmd = detector_command(region, layer, raw_path, stats_path, zoom, tasks_db=tasks_db)
    progress.log("Detektor: " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
    if result.stdout:
        progress.log(result.stdout.rstrip())
    if result.stderr:
        progress.log(result.stderr.rstrip())
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unbekannter Fehler").strip()
        raise WorkflowError(
            f"{LAYER_LABEL[layer]}-Analyse fehlgeschlagen: {redact(err[-800:])}"
        )
    if not Path(raw_path).exists():
        raise WorkflowError(f"{LAYER_LABEL[layer]}-Analyse hat keine GeoJSON erzeugt.")


def unique_ids(features, label):
    ids = [feature_id(f) for f in features]
    if any(not value for value in ids):
        raise WorkflowError(f"{label}: mindestens ein Kandidat hat keine id.")
    if len(ids) != len(set(ids)):
        raise WorkflowError(f"{label}: candidate_id ist nicht eindeutig.")


def dated_challenge_name(logical_name, existing_names):
    stamp = utc_now().strftime("%Y-%m-%d %H%M")
    base = f"{logical_name} {stamp}"
    if base not in existing_names:
        return base
    candidate = f"{base}{utc_now().strftime('%S')}"
    suffix = 2
    while candidate in existing_names:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def challenge_copy(region, layer):
    display = region["display_name"]
    label = LAYER_LABEL[layer]
    heatmap = HEATMAP[layer]
    return {
        "instruction": (
            f"Prüfe, ob hier ein in OSM fehlender Weg zur Strava-Heatmap passt "
            f"({display}, Layer {label} / {heatmap}). "
            "Kartiere den Weg nur, wenn er wirklich existiert. "
            "Markiere 'Not an Issue', wenn die Heatmap kein Weg ist "
            "(z. B. Fläche, GPS-Rauschen, bereits gemappt)."
        ),
        "description": (
            f"Automatisch erzeugte Strava-Detektionen für {display}, "
            f"Heatmap {heatmap}. Zoom {region_zoom(region)}, threshold {THRESHOLD}, "
            f"distance {DISTANCE_M} m, minsize {MINSIZE}."
        ),
        "blurb": f"Fehlende OSM-Wege aus der Strava-{label}-Heatmap ({display})",
        "checkinComment": f"#osm-strava #{region['id']} #{layer}",
    }


def planned_uploads(region, features_by_layer, upload_layers, existing_names):
    plans = []
    reserved = set(existing_names)
    for layer in upload_layers:
        feats = features_by_layer.get(layer) or []
        logical = region["challenge_names"][layer]
        name = dated_challenge_name(logical, reserved)
        reserved.add(name)
        copy = challenge_copy(region, layer)
        plans.append({
            "layer": layer,
            "logical_name": logical,
            "challenge_name": name,
            "task_count": len(feats),
            "enabled": False,
            "project_id": None,
            "instruction": copy["instruction"],
        })
    return plans


CHALLENGE_STAGE = {
    "ride": "ride_challenge",
    "run": "run_challenge",
    "all": "all_challenge",
}


def resolve_existing_run_dir(value):
    if not value or str(value).strip() in ("", "."):
        return None
    path = Path(value)
    if path.exists() and path.is_dir():
        return path
    return None


def ui_state_path(region_id):
    return STATE_DIR / region_id / "ui.json"


def load_ui_state(region_id):
    data = read_json(ui_state_path(region_id), default=None) or {}
    data.setdefault("region_id", region_id)
    data.setdefault("phase1", None)
    data.setdefault("phase2", None)
    data.setdefault("running", False)
    data.setdefault("running_phase", None)
    return data


def save_ui_state(region_id, data):
    data["region_id"] = region_id
    data["updated_at"] = iso_now()
    write_json(ui_state_path(region_id), data)
    return data


def ui_phase_key(phase):
    if phase in ("ride-run", "oneshot"):
        return "phase1"
    if phase == "all":
        return "phase2"
    return "phase1"


def apply_result_to_ui(region_id, result, progress=None, *, running=False):
    ui = load_ui_state(region_id)
    payload = result.as_dict()
    if progress is not None:
        stages = list(progress.stages)
        if progress.current:
            stages.append(dict(progress.current))
        payload["stages"] = stages
        payload["logs"] = list(progress.logs)
    payload["running"] = running
    key = ui_phase_key(result.phase)
    previous = ui.get(key) or {}
    for field in ("challenges", "files", "counts", "osm"):
        if previous.get(field) and not payload.get(field):
            payload[field] = previous[field]
    if previous.get("run_dir") and payload.get("run_dir") in (None, "", "."):
        payload["run_dir"] = previous["run_dir"]
    ui[key] = payload
    ui["running"] = running
    ui["running_phase"] = result.phase if running else None
    ui["last_error"] = None if result.ok or running else redact(result.error or "")
    return save_ui_state(region_id, ui)


def load_previous_ride_run_features(region_id):
    """Optional spatial filter inputs from the last Ride/Run phase. OSM mask is primary."""
    features = []
    ui = load_ui_state(region_id)
    phase1 = ui.get("phase1") or {}
    files = phase1.get("files") or {}
    run_dir = phase1.get("run_dir")
    for layer in ("ride", "run"):
        path = files.get(f"{layer}_josm") or files.get(f"{layer}_raw")
        if not path and run_dir:
            path = str(Path(run_dir) / f"{layer}.geojson")
        if path and Path(path).exists():
            features.extend(load_features(path))
    return features


def print_upload_plan(plans, project_id, will_write):
    print()
    print("MapRoulette-Plan")
    print("----------------")
    for plan in plans:
        if plan["task_count"] == 0:
            print(f"ÜBERSPRUNGEN: {LAYER_LABEL[plan['layer']]} — 0 Aufgaben, keine leere Challenge")
            print()
            continue
        action = "WÜRDE ERZEUGEN" if not will_write else "ERZEUGT"
        print(f"{action}: {plan['challenge_name']}")
        print(f"  Projekt: {project_id}")
        print(f"  Layer: {LAYER_LABEL[plan['layer']]}")
        print(f"  Aufgaben: {plan['task_count']}")
        print("  Sichtbar/öffentlich: nein (enabled=false)")
        print("  Bestehende Challenges: unverändert (keine Löschung, kein Rebuild)")
        print()


def already_created_challenge(existing_challenges, layer):
    for item in existing_challenges or []:
        if item.get("layer") == layer and item.get("created") and item.get("challenge_id"):
            return item
    return None


def upload_layer_challenges(
    *,
    plans,
    features_by_layer,
    progress,
    will_upload,
    dry_run,
    client,
    region,
    existing_challenges,
    result=None,
):
    challenges = list(existing_challenges or [])

    def remember(entry):
        layer = entry.get("layer")
        replaced = False
        for index, item in enumerate(challenges):
            if item.get("layer") == layer:
                challenges[index] = entry
                replaced = True
                break
        if not replaced:
            challenges.append(entry)
        if result is not None:
            result.challenges = list(challenges)
        return entry

    for plan in plans:
        layer = plan["layer"]
        stage_id = CHALLENGE_STAGE.get(layer, "maproulette")
        progress.start(stage_id, f"{LAYER_LABEL[layer]}-Challenge erstellen")
        already = already_created_challenge(challenges, layer)
        if already:
            progress.log(
                f"{LAYER_LABEL[layer]}: Challenge {already['challenge_id']} "
                "ist in diesem Lauf schon angelegt — kein Duplikat."
            )
            remember(already)
            progress.finish("bereits vorhanden")
            continue
        if plan["task_count"] == 0:
            remember({
                **plan,
                "created": False,
                "skipped_empty": True,
                "mapper_url": None,
                "admin_url": None,
                "challenge_id": None,
            })
            progress.finish("keine neuen Aufgaben")
            continue
        if not will_upload:
            remember({
                **plan,
                "created": False,
                "skipped_empty": False,
                "mapper_url": None,
                "admin_url": None,
                "challenge_id": None,
            })
            progress.finish("Dry-Run")
            continue
        if client is None:
            raise WorkflowError("MapRoulette-Client nicht verfügbar.")
        features = features_by_layer.get(layer) or []
        progress.log(
            f"Lege Challenge an: {plan['challenge_name']} ({len(features)} Aufgaben)"
        )
        copy = challenge_copy(region, layer)
        created = client.create_challenge(
            name=plan["challenge_name"],
            instruction=copy["instruction"],
            description=copy["description"],
            blurb=copy["blurb"],
            extra={"checkinComment": copy["checkinComment"]},
        )
        challenge_id = created["id"]
        register_production_challenge(region["id"], {
            "id": challenge_id,
            "layer": layer,
            "name": plan["challenge_name"],
            "challenge_name": plan["challenge_name"],
            "task_count": plan["task_count"],
            "project_id": plan.get("project_id") or 54842,
            "created_at": iso_now(),
        })
        entry = remember({
            **plan,
            "created": True,
            "skipped_empty": False,
            "challenge_id": challenge_id,
            "mapper_url": None,
            "admin_url": None,
        })
        client.add_tasks(challenge_id, {
            "type": "FeatureCollection",
            "features": features,
        })
        status = client.challenge_status(challenge_id)
        entry["mapper_url"] = status["mapper_url"]
        entry["admin_url"] = status["admin_url"]
        entry["status"] = status.get("status")
        remember(entry)
        progress.log(f"  Mapper: {status['mapper_url']}")
        progress.finish(f"Challenge {challenge_id}")
    return challenges


class WorkflowResult:
    def __init__(self):
        self.ok = False
        self.error = None
        self.failed_stage = None
        self.region_id = None
        self.run_dir = None
        self.started_at = None
        self.finished_at = None
        self.runtime_s = 0
        self.counts = {}
        self.challenges = []
        self.files = {}
        self.warnings = []
        self.osm_updated_at = None
        self.osm = {}
        self.zoom = DEFAULT_ZOOM
        self.phase = None
        self.dry_run = False
        self.uploaded = False
        self.can_retry_upload = False
        self.upload_only = False

    def as_dict(self):
        return {
            "ok": self.ok,
            "error": self.error,
            "failed_stage": self.failed_stage,
            "region_id": self.region_id,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "runtime_s": self.runtime_s,
            "counts": self.counts,
            "challenges": self.challenges,
            "files": {key: str(value) for key, value in self.files.items()},
            "warnings": self.warnings,
            "osm_updated_at": self.osm_updated_at,
            "osm": self.osm,
            "zoom": self.zoom,
            "phase": self.phase,
            "dry_run": self.dry_run,
            "uploaded": self.uploaded,
            "can_retry_upload": self.can_retry_upload,
            "upload_only": self.upload_only,
        }


def run_workflow(
    region_id,
    *,
    phase="oneshot",
    dry_run=False,
    no_upload=False,
    skip_osm=False,
    skip_detect=False,
    from_geojson_dir=None,
    geofabrik=False,
    upload_only=False,
    reuse_run_dir=None,
    progress_callback=None,
):
    progress = Progress(progress_callback)
    result = WorkflowResult()
    result.region_id = region_id
    result.phase = phase
    result.dry_run = dry_run
    result.upload_only = upload_only
    result.started_at = iso_now()
    started = utc_now()
    will_upload = not dry_run and not no_upload
    existing_challenges = []

    def persist_live():
        payload = result.as_dict()
        stages = list(progress.stages)
        if progress.current:
            stages.append(dict(progress.current))
        payload["stages"] = stages
        payload["logs"] = list(progress.logs)
        payload["running"] = True
        if result.run_dir:
            write_json(Path(result.run_dir) / "result.json", payload)
        write_json(STATE_DIR / "current.json", payload)
        apply_result_to_ui(region_id, result, progress, running=True)

    original_emit = progress.emit

    def emit_and_persist(**payload):
        original_emit(**payload)
        persist_live()

    try:
        if phase not in PHASE_LAYERS:
            raise WorkflowError(f"Unbekannte Phase: {phase}")
        region = load_region(region_id)
        zoom = region_zoom(region)
        result.zoom = zoom
        detect_layers = list(PHASE_LAYERS[phase])
        upload_layers = list(UPLOAD_LAYERS[phase])
        if not Path(region["boundary"]).exists():
            raise WorkflowError(f"Gebietsgrenze fehlt: {region['boundary']}")
        prev_ui = load_ui_state(region_id)
        prev_phase = prev_ui.get(ui_phase_key(phase)) or {}
        if not upload_only and phase in ("ride-run", "oneshot"):
            prev_ui["phase2"] = None
            save_ui_state(region_id, prev_ui)
        if upload_only:
            skip_osm = True
            skip_detect = True
            run_dir = resolve_existing_run_dir(reuse_run_dir) or resolve_existing_run_dir(
                prev_phase.get("run_dir")
            )
            if run_dir is None:
                raise WorkflowError(
                    "Upload-Wiederholung braucht einen vorhandenen Lauf. "
                    "Bitte zuerst Ride & Run bzw. All erzeugen."
                )
            existing_challenges = list(prev_phase.get("challenges") or [])
            result.files = dict(prev_phase.get("files") or {})
            result.counts = dict(prev_phase.get("counts") or {})
        else:
            if skip_detect and not from_geojson_dir:
                raise WorkflowError("--skip-detect braucht --from-geojson-dir.")
            if from_geojson_dir and not Path(from_geojson_dir).is_dir():
                raise WorkflowError(f"GeoJSON-Verzeichnis nicht gefunden: {from_geojson_dir}")
            run_dir = ensure_dir(STATE_DIR / region_id / "runs" / utc_stamp())
        result.run_dir = run_dir
        progress.emit = emit_and_persist
        progress.start("validate", "Konfiguration prüfen")
        progress.finish("Region und Verzeichnisse gültig")

        progress.start("verify_api", "MapRoulette-Zugang prüfen")
        config = MapRouletteConfig()
        client = None
        existing_names = set()
        if will_upload:
            if not config.has_api_key():
                raise WorkflowError(
                    "Für den Upload wird ein MapRoulette-API-Schlüssel benötigt."
                )
            client = MapRouletteClient(config)
            info = client.verify_api_key()
            progress.log(f"Projekt {info['project_id']}: {info['project_name']}")
        elif config.has_api_key():
            try:
                client = MapRouletteClient(config)
                info = client.verify_api_key()
                progress.log(f"Projekt {info['project_id']}: {info['project_name']} (nur Prüfung)")
            except MapRouletteError as exc:
                result.warnings.append(str(exc))
                progress.log(f"Hinweis: API-Prüfung übersprungen ({exc})")
                client = None
        else:
            result.warnings.append("Kein MapRoulette-API-Schlüssel vorhanden (Dry-Run / kein Upload).")
            progress.log("Kein API-Schlüssel — Upload wird nicht ausgeführt.")
        if client is not None:
            day = utc_now().strftime("%Y-%m-%d")
            for layer in LAYERS:
                logical = region["challenge_names"][layer]
                for name in (logical, f"{logical} {day}"):
                    found = client.find_challenge_by_name(name)
                    if found:
                        existing_names.add(found.get("name") or name)
        progress.finish()

        if not upload_only:
            progress.start("review_history", "MapRoulette-Historie synchronisieren")
            sync = sync_review_history(region_id, client)
            result.files["review_history"] = str(STATE_DIR / region_id / "review-history.json")
            db_path = tasks_db_path(region_id)
            if db_path.exists():
                result.files["tasks_db"] = str(db_path)
            result.counts["review_challenges"] = sync.challenges_checked
            result.counts["not_an_issue"] = sync.nai_count
            for warning in sync.warnings:
                result.warnings.append(warning)
                progress.log(warning)
            if not sync.ok:
                if history_exists(region_id):
                    result.warnings.append(sync.error or "Historie-Sync unvollständig.")
                    progress.log(sync.error or "Historie-Sync unvollständig — lokale Historie bleibt.")
                elif not skip_detect:
                    raise WorkflowError(sync.error or "MapRoulette-Historie konnte nicht geladen werden.")
                else:
                    result.warnings.append(sync.error or "Keine lokale MapRoulette-Historie.")
                    progress.log(sync.error or "Keine lokale MapRoulette-Historie.")
            elif sync.used_cache:
                progress.log("Letzte lokale Historie wird verwendet.")
            progress.finish(
                f"{sync.challenges_checked} Challenges, {sync.nai_count} Not an Issue"
            )

        progress.start("osm", "OSM-Daten aktualisieren")
        osm_info = {
            "mode": "geofabrik" if geofabrik else "fresh",
            "fresh": not geofabrik,
            "skipped": bool(skip_osm),
            "zoom": zoom,
            "updater_command": " ".join(osm_update_command(region_id, fresh=not geofabrik)),
        }
        working = read_key_values(working_state_path(region))
        if working.get("result_timestamp"):
            osm_info["working_timestamp"] = working["result_timestamp"]
        if skip_osm or upload_only:
            progress.log("OSM-Update übersprungen.")
            if not upload_only:
                progress.log(f"Geplanter OSM-Befehl wäre: {osm_info['updater_command']}")
            osm_info["skipped"] = True
        else:
            log_path = artifact(run_dir, "osm-update.log")
            parsed, _output = run_osm_update(
                region_id,
                progress,
                fresh=not geofabrik,
                log_path=log_path,
            )
            osm_info.update(parsed)
            osm_info["log"] = log_path
            result.files["osm_update_log"] = log_path
        if not upload_only and not skip_detect and not Path(region["extract_xml"]).exists():
            raise WorkflowError(
                f"OSM-Datei fehlt: {region['extract_xml']}. "
                "Bitte zuerst das OSM-Update ausführen."
            )
        result.osm = osm_info
        result.osm_updated_at = osm_info.get("timestamp_after") or osm_mtime(region["extract_xml"])
        progress.finish(result.osm_updated_at or "vorhanden")

        features_by_layer = {}
        raw_counts = {}
        if upload_only:
            for layer in upload_layers:
                josm = result.files.get(f"{layer}_josm") or artifact(run_dir, f"{layer}.geojson")
                if not Path(josm).exists():
                    raise WorkflowError(
                        f"{LAYER_LABEL[layer]}-Datei für Upload-Wiederholung fehlt."
                    )
                features = load_features(josm)
                features_by_layer[layer] = features
                raw_counts[layer] = len(features)
                result.counts[layer] = len(features)
                progress.start(layer, f"{LAYER_LABEL[layer]} analysieren")
                progress.finish(
                    f"{len(features)} Kandidaten (vorhanden)",
                    {"candidates": len(features)},
                )
        else:
            for layer in detect_layers:
                label = LAYER_LABEL[layer]
                progress.start(layer, f"{label} analysieren")
                raw_path = artifact(run_dir, f"{layer}.raw.geojson")
                stats_path = artifact(run_dir, f"{layer}-stats.json")
                if skip_detect:
                    source = find_source_geojson(from_geojson_dir, region_id, layer)
                    if source is None:
                        raise WorkflowError(
                            f"Keine {label}-GeoJSON in {from_geojson_dir} gefunden."
                        )
                    shutil.copy2(source, raw_path)
                    progress.log(f"Übernehme vorhandene Datei: {source}")
                else:
                    run_detector(
                        region,
                        layer,
                        raw_path,
                        stats_path,
                        progress,
                        zoom,
                        tasks_db=tasks_db_path(region_id),
                    )
                features = annotate_provenance(
                    load_features(raw_path),
                    region=region_id,
                    layer=layer,
                    heatmap_layer=HEATMAP[layer],
                )
                unique_ids(features, label)
                features_by_layer[layer] = features
                raw_counts[layer] = len(features)
                result.files[f"{layer}_raw"] = raw_path
                if Path(stats_path).exists():
                    result.files[f"{layer}_stats"] = stats_path
                progress.finish(f"{len(features)} Kandidaten", {"candidates": len(features)})

        all_raw_count = raw_counts.get("all")
        if "all" in features_by_layer and not upload_only:
            others = (features_by_layer.get("ride") or []) + (features_by_layer.get("run") or [])
            if not others:
                others = load_previous_ride_run_features(region_id)
                if others:
                    progress.log(
                        "Optionale 25-m-Filterung gegen letzte Ride/Run-Ausgabe. "
                        "Die OSM-Maske bleibt die primäre Deduplizierung."
                    )
                else:
                    progress.log("Keine vorherigen Ride/Run-Punkte; OSM-Maske ist die Deduplizierung.")
            if others:
                kept, removed = filter_farther_than(
                    features_by_layer["all"], others, ALL_DEDUPE_M
                )
                features_by_layer["all"] = kept
                removed_path = artifact(run_dir, "all-removed-near-ride-run.geojson")
                write_feature_collection(removed_path, [item[0] for item in removed])
                result.files["all_removed"] = removed_path
                result.counts["all_raw"] = all_raw_count
                result.counts["all_deduped"] = len(kept)
                result.counts["all_removed_near_ride_run"] = len(removed)
                progress.log(
                    f"All: {all_raw_count} nach OSM-Maske, {len(removed)} nahe Ride/Run, "
                    f"{len(kept)} All-only"
                )
            else:
                result.counts["all_raw"] = all_raw_count
                result.counts["all_deduped"] = all_raw_count

            all_final = len(features_by_layer["all"])
            for stage in reversed(progress.stages):
                if stage.get("id") == "all":
                    stage["detail"] = f"{all_final} Kandidaten"
                    stage["counts"] = {"candidates": all_final}
                    break

        if not upload_only:
            for layer, features in features_by_layer.items():
                josm_path = artifact(run_dir, f"{layer}.geojson")
                geojsonl_path = artifact(run_dir, f"{layer}.geojsonl")
                write_feature_collection(josm_path, features)
                write_geojsonl(geojsonl_path, features)
                result.files[f"{layer}_josm"] = josm_path
                result.files[f"{layer}_geojsonl"] = geojsonl_path
                result.counts[layer] = len(features)
                progress.log(f"{LAYER_LABEL[layer]}: {len(features)} → {os.path.basename(josm_path)}")
            result.can_retry_upload = True

        plans = planned_uploads(region, features_by_layer, upload_layers, existing_names)
        for plan in plans:
            plan["project_id"] = config.project_id
        print_upload_plan(plans, config.project_id, will_upload)

        challenges = upload_layer_challenges(
            plans=plans,
            features_by_layer=features_by_layer,
            progress=progress,
            will_upload=will_upload,
            dry_run=dry_run,
            client=client,
            region=region,
            existing_challenges=existing_challenges,
            result=result,
        )
        result.challenges = challenges
        result.uploaded = will_upload and any(item.get("created") for item in challenges)
        result.can_retry_upload = False
        result.ok = True
        progress.start("done", "Fertig")
        progress.finish()

    except (WorkflowError, MapRouletteError, KeyError, ValueError) as exc:
        result.ok = False
        result.error = redact(str(exc))
        result.failed_stage = progress.failed_stage or (progress.current or {}).get("id")
        result.can_retry_upload = any(
            Path(str(path)).exists() and str(path).endswith(".geojson")
            for path in result.files.values()
        )
        progress.fail(result.error)
    except Exception as exc:
        result.ok = False
        result.error = f"Unerwarteter Fehler: {redact(exc)}"
        result.failed_stage = progress.failed_stage or (progress.current or {}).get("id")
        result.can_retry_upload = any(
            Path(str(path)).exists() and str(path).endswith(".geojson")
            for path in result.files.values()
        )
        progress.fail(result.error)
        progress.log(redact(traceback.format_exc()))
    finally:
        result.finished_at = iso_now()
        result.runtime_s = int((utc_now() - started).total_seconds())
        payload = result.as_dict()
        payload["stages"] = progress.stages
        payload["logs"] = progress.logs
        payload["running"] = False
        if result.run_dir:
            write_json(Path(result.run_dir) / "result.json", payload)
            write_json(STATE_DIR / result.region_id / "latest.json", payload)
        write_json(STATE_DIR / "current.json", payload)
        if result.region_id:
            apply_result_to_ui(result.region_id, result, progress, running=False)

    return result


def print_summary(result):
    print()
    print("=" * 60)
    print("ZUSAMMENFASSUNG")
    print("=" * 60)
    print(f"Region: {result.region_id}")
    print(f"Phase: {result.phase}")
    print(f"Zoom: {result.zoom} (explizit, nicht strava.py-Default)")
    print(f"Laufzeit: {result.runtime_s}s")
    osm = result.osm or {}
    if osm.get("updater_command"):
        print(f"OSM-Befehl: {osm['updater_command']}")
    if osm.get("mode"):
        print(f"OSM-Modus: {osm['mode']}")
    if osm.get("timestamp_before"):
        print(f"OSM-Zeitstempel vorher: {osm['timestamp_before']}")
    if osm.get("timestamp_after") or result.osm_updated_at:
        print(f"OSM-Zeitstempel nachher: {osm.get('timestamp_after') or result.osm_updated_at}")
    if osm.get("source_lag"):
        print(f"OSM-Lag: {osm['source_lag']}")
    if osm.get("lag_behind_planet"):
        print(f"Lag hinter Planet: {osm['lag_behind_planet']}")
    if osm.get("nodes_before") or osm.get("nodes_after"):
        print(f"OSM Nodes: {osm.get('nodes_before', '-')} -> {osm.get('nodes_after', '-')}")
    if osm.get("ways_before") or osm.get("ways_after"):
        print(f"OSM Ways: {osm.get('ways_before', '-')} -> {osm.get('ways_after', '-')}")
    counts = result.counts
    print(f"Ride: {counts.get('ride', '-')}")
    print(f"Run: {counts.get('run', '-')}")
    if "all_raw" in counts:
        print(f"All (roh): {counts['all_raw']}")
        print(f"All (nach 25m-Filter): {counts.get('all', '-')}")
    else:
        print(f"All: {counts.get('all', '-')}")
    if result.challenges:
        print()
        for item in result.challenges:
            url = item.get("mapper_url") or "(nicht hochgeladen)"
            print(f"{LAYER_LABEL[item['layer']]}: {item['task_count']} Aufgaben")
            print(f"  {item['challenge_name']}")
            print(f"  {url}")
    if result.warnings:
        print()
        print("Hinweise:")
        for warning in result.warnings:
            print(f"- {warning}")
    if not result.ok:
        print()
        print(f"Fehlgeschlagen in Stufe: {result.failed_stage}")
        print(result.error)
        return 1
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Strava-Detektion für eine Region ausführen und MapRoulette-Challenges anlegen.",
    )
    parser.add_argument("region", help="Regions-ID aus osm-regions.conf, z. B. mallorca")
    parser.add_argument(
        "--phase",
        choices=("oneshot", "ride-run", "all"),
        default="oneshot",
        help="oneshot=alles; ride-run=nur Ride+Run; all=All nach frischem OSM",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Produktion: fresh-OSM und Detektion laufen. Lokale Dateien werden "
            "geschrieben. MapRoulette wird nicht verändert."
        ),
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Wie --dry-run für MapRoulette: lokal erzeugen, nicht hochladen",
    )
    parser.add_argument("--skip-osm", action="store_true", help="OSM-Update überspringen")
    parser.add_argument(
        "--geofabrik",
        action="store_true",
        help="Debug: Geofabrik-Tagesstand statt --fresh. Nicht Produktion.",
    )
    parser.add_argument(
        "--skip-detect",
        action="store_true",
        help="Detektor nicht starten; vorhandene GeoJSON verwenden",
    )
    parser.add_argument(
        "--from-geojson-dir",
        help="Verzeichnis mit vorhandenen Layer-GeoJSON (für --skip-detect)",
    )
    parser.add_argument(
        "--retry-upload",
        action="store_true",
        help="Nur MapRoulette-Upload des letzten Laufs wiederholen, ohne OSM/Detektion.",
    )
    parser.add_argument("--list-regions", action="store_true", help="Bekannte Regionen anzeigen")
    parser.add_argument(
        "--maproulette-smoke-test",
        action="store_true",
        help=(
            "Nur API-Schreibpfad prüfen: neue temporäre Challenge mit 1–2 Ride-Aufgaben. "
            "Keine Produktions-Challenges, kein Run/All, kein Löschen."
        ),
    )
    parser.add_argument(
        "--sync-history",
        action="store_true",
        help="Nur MapRoulette-Review-Historie synchronisieren (kein Detektor, kein Upload).",
    )
    return parser


def latest_ride_geojson(region_id):
    latest = read_json(STATE_DIR / region_id / "latest.json", default=None)
    if not latest:
        raise WorkflowError(
            "Kein letzter Lauf gefunden. Zuerst z. B. "
            "python workflow.py mallorca --dry-run ausführen."
        )
    files = latest.get("files") or {}
    for key in ("ride_josm", "ride_raw"):
        path = files.get(key)
        if path and Path(path).exists():
            return Path(path)
    run_dir = latest.get("run_dir")
    if run_dir:
        for name in ("ride.geojson", "ride.raw.geojson"):
            path = Path(run_dir) / name
            if path.exists():
                return Path(path)
    raise WorkflowError("Keine Ride-Ausgabe im letzten Lauf gefunden.")


def _challenge_parent_id(challenge):
    parent = challenge.get("parent")
    if isinstance(parent, dict):
        return parent.get("id")
    return parent


def _task_identity(task):
    names = []
    name = task.get("name") or task.get("taskName")
    if name:
        names.append(str(name))
    geometries = task.get("geometries") or {}
    features = geometries.get("features") if isinstance(geometries, dict) else None
    if not features and isinstance(task.get("geometries"), list):
        features = task.get("geometries")
    for feature in features or []:
        props = (feature or {}).get("properties") or {}
        for key in ("id", "candidate_id", "name"):
            value = props.get(key)
            if value:
                names.append(str(value))
    return names


def run_maproulette_smoke_test(region_id):
    region = load_region(region_id)
    ride_path = latest_ride_geojson(region_id)
    features = load_features(ride_path)
    if not features:
        raise WorkflowError(f"Ride-Datei ist leer: {ride_path}")
    sample = features[:2]
    expected_ids = [feature_id(item) for item in sample]
    if any(not value for value in expected_ids):
        raise WorkflowError("Ride-Kandidaten ohne id/candidate_id.")

    config = MapRouletteConfig()
    if config.project_id != 54842:
        raise WorkflowError(
            f"Smoke-Test erwartet Projekt 54842, konfiguriert ist {config.project_id}."
        )
    client = MapRouletteClient(config)
    info = client.verify_api_key()
    if int(info["project_id"]) != 54842:
        raise WorkflowError(
            f"API lieferte Projekt {info['project_id']}, erwartet 54842."
        )
    print(f"Projekt: {info['project_id']} ({info.get('project_name')})")
    print(f"Ride-Quelle: {ride_path}")
    print(f"Test-Kandidaten: {len(sample)}")
    for cid in expected_ids:
        print(f"  - {cid}")

    stamp = utc_now().strftime("%Y-%m-%d %H%M")
    name = f"Strava {region['display_name']} API Test {stamp}"
    existing = client.find_challenge_by_name(name)
    if existing:
        name = f"{name}{utc_now().strftime('%S')}"
        existing = client.find_challenge_by_name(name)
        if existing:
            raise WorkflowError(
                f"Temporärer Challenge-Name existiert bereits: {name}. Bitte Sekunde später erneut versuchen."
            )

    print()
    print("Lege neue temporäre Challenge an (keine bestehende Challenge wird geändert).")
    print(f"Name: {name}")
    created = client.create_challenge(
        name=name,
        instruction=(
            "API-Smoke-Test von osm-strava. Diese Challenge ist nicht für die "
            "Produktion. Bitte nicht als normale Mapping-Runde abarbeiten."
        ),
        description="Temporary MapRoulette API smoke test. Safe to ignore or archive.",
        blurb="osm-strava API smoke test",
        extra={"checkinComment": f"#osm-strava-smoke-test #{region['id']}"},
    )
    challenge_id = created["id"]
    print(f"Challenge-ID: {challenge_id}")

    for feature in sample:
        client.add_tasks(challenge_id, {
            "type": "FeatureCollection",
            "features": [feature],
        })
    challenge, tasks = client.wait_until_ready(
        challenge_id, timeout_s=90, expected_tasks=len(sample)
    )
    if challenge is None:
        raise WorkflowError("Challenge nach dem Upload nicht lesbar.")

    parent_id = _challenge_parent_id(challenge)
    enabled = challenge.get("enabled")
    found_ids = []
    for task in tasks:
        found_ids.extend(_task_identity(task))
    missing = [cid for cid in expected_ids if cid not in found_ids]
    actions = challenge.get("actions") or {}
    reported_count = actions.get("total")
    actual_count = len(tasks)
    if reported_count is None:
        reported_count = actual_count

    errors = []
    if int(challenge.get("id") or 0) != int(challenge_id):
        errors.append("Challenge-ID stimmt nach dem Lesen nicht.")
    if parent_id is not None and int(parent_id) != 54842:
        errors.append(f"parent ist {parent_id}, erwartet 54842.")
    if enabled not in (False, 0, "false", "False"):
        errors.append(f"enabled={enabled!r}, erwartet false.")
    if actual_count < len(sample):
        errors.append(
            f"Aufgabenanzahl {actual_count}, erwartet {len(sample)}."
        )
    if missing:
        errors.append("TaskName/candidate_id fehlt: " + ", ".join(missing))
    if errors:
        raise WorkflowError("Smoke-Test fehlgeschlagen: " + " ".join(errors))

    mapper = client.mapper_url(challenge_id)
    print()
    print("SMOKE-TEST ERFOLGREICH")
    print(f"challenge id:   {challenge_id}")
    print(f"challenge name: {challenge.get('name') or name}")
    print(f"mapper URL:     {mapper}")
    print(f"task count:     {actual_count}")
    print("enabled:        false")
    print("project id:     54842")
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_regions:
        for region_id, data in load_all_regions().items():
            print(f"{region_id}\t{data['display_name']}")
        return 0
    if args.maproulette_smoke_test:
        try:
            return run_maproulette_smoke_test(args.region)
        except (WorkflowError, MapRouletteError) as exc:
            print(f"FEHLER: {redact(exc)}", file=sys.stderr)
            return 1
    if args.sync_history:
        try:
            config = MapRouletteConfig()
            client = MapRouletteClient(config) if config.has_api_key() else None
            sync = sync_review_history(args.region, client)
            print(f"Challenges geprüft: {sync.challenges_checked}")
            print(f'Bekannte "Not an Issue"-Fälle: {sync.nai_count}')
            print(f"Historie: {STATE_DIR / args.region / 'review-history.json'}")
            print(f"tasks.sqlite: {tasks_db_path(args.region)}")
            for warning in sync.warnings:
                print(f"Hinweis: {warning}")
            if not sync.ok:
                print(f"FEHLER: {sync.error}", file=sys.stderr)
                return 1
            return 0
        except (WorkflowError, MapRouletteError) as exc:
            print(f"FEHLER: {redact(exc)}", file=sys.stderr)
            return 1
    result = run_workflow(
        args.region,
        phase=args.phase,
        dry_run=args.dry_run,
        no_upload=args.no_upload,
        skip_osm=args.skip_osm,
        skip_detect=args.skip_detect,
        from_geojson_dir=args.from_geojson_dir,
        geofabrik=args.geofabrik,
        upload_only=args.retry_upload,
    )
    return print_summary(result)


if __name__ == "__main__":
    sys.exit(main())
