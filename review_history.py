#!/usr/bin/env python3
"""Persistent MapRoulette review history for a region.

Reuses strava.py -b / --tasks_db. That path queries:

    SELECT TaskStatus, Mapper, TaskLink FROM tasks WHERE TaskName='...'

and omits GeoJSON when TaskStatus is exactly ``Not_an_Issue`` (lowercase a)
or ``Too_Hard``. This module writes ONLY Not-an-Issue rows, using that exact
spelling, so Fixed / Already_Fixed / Too_Hard / Skipped stay detectable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from maproulette import MapRouletteError

REPO = Path(__file__).resolve().parent
STATE_DIR = REPO / "state"

# Exact string compared in strava.py (do not "fix" the capital A).
STRAVA_DB_NAI = "Not_an_Issue"

# MapRoulette Task.scala: STATUS_FALSE_POSITIVE = 2, NAME = "Not_An_Issue"
STATUS_BY_CODE = {
    0: "Created",
    1: "Fixed",
    2: "Not_An_Issue",
    3: "Skipped",
    4: "Deleted",
    5: "Already_Fixed",
    6: "Too_Hard",
    7: "Answered",
    8: "Validated",
    9: "Disabled",
}

_NAI_FOLDED = {
    "2",
    "not_an_issue",
    "notanissue",
    "false_positive",
    "falsepositive",
}

_FOLD_TO_CANONICAL = {
    "created": "Created",
    "0": "Created",
    "fixed": "Fixed",
    "1": "Fixed",
    "not_an_issue": "Not_An_Issue",
    "false_positive": "Not_An_Issue",
    "2": "Not_An_Issue",
    "skipped": "Skipped",
    "3": "Skipped",
    "deleted": "Deleted",
    "4": "Deleted",
    "already_fixed": "Already_Fixed",
    "alreadyfixed": "Already_Fixed",
    "5": "Already_Fixed",
    "too_hard": "Too_Hard",
    "toohard": "Too_Hard",
    "cant_complete": "Too_Hard",
    "can't_complete": "Too_Hard",
    "6": "Too_Hard",
}


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fold_status(value):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        code = int(value)
        return STATUS_BY_CODE.get(code, str(code)).lower().replace(" ", "_")
    text = str(value).strip()
    if not text:
        return ""
    if text.isdigit() or (text[0] == "-" and text[1:].isdigit()):
        code = int(text)
        return STATUS_BY_CODE.get(code, text).lower().replace(" ", "_")
    return text.lower().replace(" ", "_").replace("-", "_")


def canonical_status(value):
    folded = fold_status(value)
    if not folded:
        return ""
    if folded in _FOLD_TO_CANONICAL:
        return _FOLD_TO_CANONICAL[folded]
    compact = folded.replace("_", "")
    if compact in _FOLD_TO_CANONICAL:
        return _FOLD_TO_CANONICAL[compact]
    return str(value).strip()


def is_not_an_issue(value):
    folded = fold_status(value)
    return folded in _NAI_FOLDED or folded.replace("_", "") in _NAI_FOLDED


def is_smoke_challenge(entry):
    name = str((entry or {}).get("name") or (entry or {}).get("challenge_name") or "")
    comment = str((entry or {}).get("checkinComment") or "")
    blob = f"{name} {comment}".lower()
    return "api test" in blob or "smoke-test" in blob or "smoke_test" in blob


def history_path(region_id, state_dir=None):
    return Path(state_dir or STATE_DIR) / region_id / "review-history.json"


def tasks_db_path(region_id, state_dir=None):
    return Path(state_dir or STATE_DIR) / region_id / "tasks.sqlite"


def empty_history(region_id):
    return {
        "region_id": region_id,
        "updated_at": None,
        "challenges": {},
        "not_an_issue": {},
    }


def load_history(region_id, state_dir=None):
    path = history_path(region_id, state_dir)
    if not path.exists():
        return empty_history(region_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return empty_history(region_id)
    data.setdefault("region_id", region_id)
    data.setdefault("challenges", {})
    data.setdefault("not_an_issue", {})
    return data


def history_exists(region_id, state_dir=None):
    """True when a usable local NAI cache exists (not merely a challenge list)."""
    data = load_history(region_id, state_dir)
    if data.get("not_an_issue"):
        return True
    db = tasks_db_path(region_id, state_dir)
    return db.exists() and db.stat().st_size > 0


def save_history(region_id, data, state_dir=None):
    path = history_path(region_id, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["region_id"] = region_id
    data["updated_at"] = iso_now()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data


def task_candidate_id(task):
    name = (task or {}).get("name") or (task or {}).get("taskName") or ""
    if name:
        return str(name).strip()
    geometries = (task or {}).get("geometries") or {}
    features = geometries.get("features") if isinstance(geometries, dict) else None
    if not features and isinstance((task or {}).get("geometries"), list):
        features = task.get("geometries")
    for feature in features or []:
        props = (feature or {}).get("properties") or {}
        for key in ("id", "candidate_id", "name"):
            value = props.get(key)
            if value:
                return str(value).strip()
    return ""


def _infer_layer(name):
    text = str(name or "").lower()
    if " api test" in text:
        return None
    if text.endswith(" ride") or " ride " in text:
        return "ride"
    if text.endswith(" run") or " run " in text:
        return "run"
    if text.endswith(" all") or " all " in text:
        return "all"
    return None


def _challenge_key(challenge_id):
    return str(int(challenge_id))


def register_production_challenge(region_id, entry, state_dir=None):
    """Append-only registry. Never drops previously stored production IDs."""
    if not entry or not entry.get("id") and not entry.get("challenge_id"):
        return load_history(region_id, state_dir)
    if is_smoke_challenge(entry):
        return load_history(region_id, state_dir)
    cid = int(entry.get("id") or entry.get("challenge_id"))
    if cid <= 0 or cid == 99999:
        return load_history(region_id, state_dir)
    history = load_history(region_id, state_dir)
    key = _challenge_key(cid)
    previous = history["challenges"].get(key) or {}
    name = entry.get("name") or entry.get("challenge_name") or previous.get("name")
    record = {
        "layer": entry.get("layer") or previous.get("layer") or _infer_layer(name),
        "name": name,
        "created_at": previous.get("created_at") or entry.get("created_at") or iso_now(),
        "task_count": entry.get("task_count") if entry.get("task_count") is not None else previous.get("task_count"),
        "project_id": entry.get("project_id") or previous.get("project_id") or 54842,
        "smoke_test": False,
    }
    if previous.get("synced_at"):
        record["synced_at"] = previous["synced_at"]
    history["challenges"][key] = record
    return save_history(region_id, history, state_dir)


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _collect_from_payload(payload, found):
    if not isinstance(payload, dict):
        return
    if payload.get("phase1") or payload.get("phase2"):
        _collect_from_payload(payload.get("phase1"), found)
        _collect_from_payload(payload.get("phase2"), found)
    for item in payload.get("challenges") or []:
        cid = item.get("challenge_id") or item.get("id")
        if not cid:
            continue
        if is_smoke_challenge(item):
            continue
        if item.get("created") is False:
            continue
        try:
            cid_int = int(cid)
        except (TypeError, ValueError):
            continue
        if cid_int <= 0 or cid_int == 99999:
            continue
        key = _challenge_key(cid_int)
        prev = found.get(key) or {}
        name = item.get("challenge_name") or item.get("name") or prev.get("name")
        found[key] = {
            "layer": item.get("layer") or prev.get("layer") or _infer_layer(name),
            "name": name,
            "created_at": prev.get("created_at") or item.get("created_at"),
            "task_count": item.get("task_count") if item.get("task_count") is not None else prev.get("task_count"),
            "project_id": item.get("project_id") or prev.get("project_id") or 54842,
            "smoke_test": False,
        }


def discover_production_challenges(region_id, state_dir=None):
    """Union of durable history and leftover run/UI state. Smoke tests excluded."""
    root = Path(state_dir or STATE_DIR)
    found = {}
    history = load_history(region_id, state_dir)
    for key, record in (history.get("challenges") or {}).items():
        if is_smoke_challenge(record) or str(key) == "99999":
            continue
        found[str(key)] = dict(record)
    region_dir = root / region_id
    _collect_from_payload(_read_json(region_dir / "ui.json"), found)
    _collect_from_payload(_read_json(region_dir / "latest.json"), found)
    _collect_from_payload(_read_json(root / "current.json"), found)
    runs = region_dir / "runs"
    if runs.is_dir():
        for result_path in runs.glob("*/result.json"):
            _collect_from_payload(_read_json(result_path), found)
    return found


def write_tasks_sqlite(region_id, not_an_issue, state_dir=None):
    """Rebuild the read-only-compatible tasks table with NAI rows only."""
    path = tasks_db_path(region_id, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".sqlite.tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(tmp))
    try:
        con.execute(
            "CREATE TABLE tasks ("
            "TaskName TEXT PRIMARY KEY, "
            "TaskStatus TEXT NOT NULL, "
            "Mapper TEXT, "
            "TaskLink TEXT, "
            "ChallengeID INTEGER"
            ")"
        )
        con.execute("CREATE INDEX tasks_idx ON tasks(TaskName)")
        rows = []
        for name, meta in sorted((not_an_issue or {}).items()):
            cid = meta.get("last_seen_challenge") or meta.get("first_seen_challenge")
            link = meta.get("task_link") or ""
            if not link and cid:
                link = (
                    f"[[hyperlink URL link=https://maproulette.org/challenge/{int(cid)}"
                    f"/task/{meta.get('task_id') or ''}]]"
                )
            rows.append((
                name,
                STRAVA_DB_NAI,
                meta.get("mapper") or "",
                link,
                int(cid) if cid else None,
            ))
        con.executemany(
            "INSERT INTO tasks (TaskName, TaskStatus, Mapper, TaskLink, ChallengeID) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()
    tmp.replace(path)
    return path


def nai_ids_from_sqlite(region_id, state_dir=None):
    path = tasks_db_path(region_id, state_dir)
    if not path.exists():
        return set()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT TaskName, TaskStatus FROM tasks").fetchall()
    finally:
        con.close()
    return {name for name, status in rows if status == STRAVA_DB_NAI}


class HistorySyncResult:
    def __init__(self):
        self.ok = True
        self.used_cache = False
        self.challenges_checked = 0
        self.nai_count = 0
        self.warnings = []
        self.error = None
        self.history = None


def _note_nai(not_an_issue, candidate_id, challenge_id, task=None):
    previous = not_an_issue.get(candidate_id) or {}
    first = previous.get("first_seen_challenge") or challenge_id
    entry = {
        "first_seen_challenge": int(first),
        "last_seen_challenge": int(challenge_id),
    }
    if task:
        if task.get("id") is not None:
            entry["task_id"] = task.get("id")
        mapper = task.get("completedBy") or task.get("mapper")
        if isinstance(mapper, dict):
            mapper = mapper.get("name") or mapper.get("id")
        if mapper:
            entry["mapper"] = str(mapper)
        tid = task.get("id")
        entry["task_link"] = (
            f"[[hyperlink URL link=https://maproulette.org/challenge/{int(challenge_id)}"
            f"/task/{tid}]]"
        )
    elif previous.get("task_link"):
        entry["task_link"] = previous["task_link"]
        if previous.get("task_id") is not None:
            entry["task_id"] = previous["task_id"]
        if previous.get("mapper"):
            entry["mapper"] = previous["mapper"]
    not_an_issue[candidate_id] = entry


def sync_review_history(region_id, client, *, state_dir=None, progress=None):
    """Fetch task statuses for known production challenges. Never wipe on failure."""
    result = HistorySyncResult()
    history = load_history(region_id, state_dir)
    discovered = discover_production_challenges(region_id, state_dir)
    for key, record in discovered.items():
        if key not in history["challenges"]:
            history["challenges"][key] = record

    if client is None:
        result.used_cache = True
        result.nai_count = len(history.get("not_an_issue") or {})
        result.history = history
        if history_exists(region_id, state_dir):
            result.warnings.append(
                "MapRoulette-Historie: API nicht verfügbar — letzte lokale Historie wird verwendet."
            )
        else:
            result.ok = False
            result.error = (
                "MapRoulette-Historie fehlt und konnte nicht geladen werden. "
                "Ein neuer Lauf würde bereits als Not an Issue markierte Aufgaben erneut anlegen."
            )
        return result

    not_an_issue = dict(history.get("not_an_issue") or {})
    checked = 0
    failed = []

    for key in sorted(discovered, key=lambda item: int(item)):
        record = discovered[key]
        if is_smoke_challenge(record):
            continue
        try:
            challenge = client.get_challenge(int(key))
            tasks = client.list_tasks(int(key))
        except MapRouletteError as exc:
            failed.append(f"{key}: {exc}")
            if "HTTP 404" in str(exc) and not (history["challenges"].get(key) or {}).get("synced_at"):
                history["challenges"].pop(key, None)
            continue
        except Exception as exc:
            failed.append(f"{key}: {exc}")
            continue

        parent = challenge.get("parent")
        if isinstance(parent, dict):
            parent = parent.get("id")
        if parent is not None and int(parent) != 54842:
            result.warnings.append(
                f"Challenge {key} gehört zu Projekt {parent}, nicht 54842 — übersprungen."
            )
            continue

        checked += 1
        name = challenge.get("name") or record.get("name")
        stored = history["challenges"].setdefault(key, dict(record))
        stored["name"] = name
        stored["layer"] = record.get("layer") or stored.get("layer") or _infer_layer(name)
        stored["task_count"] = len(tasks)
        stored["synced_at"] = iso_now()
        stored["project_id"] = 54842
        stored["smoke_test"] = False
        if is_smoke_challenge({"name": name}):
            history["challenges"].pop(key, None)
            continue

        for task in tasks:
            if not is_not_an_issue(task.get("status") or task.get("statusName") or task.get("taskStatus")):
                continue
            candidate = task_candidate_id(task)
            if not candidate:
                continue
            _note_nai(not_an_issue, candidate, int(key), task)

    if checked == 0 and discovered and failed:
        result.used_cache = True
        result.warnings.append(
            "MapRoulette-Historie: Sync fehlgeschlagen — letzte lokale Historie bleibt erhalten. "
            + "; ".join(failed[:3])
        )
        if not history_exists(region_id, state_dir) and not not_an_issue:
            result.ok = False
            result.error = (
                "MapRoulette-Historie konnte nicht geladen werden. "
                "Bitte später erneut versuchen, bevor neue Aufgaben erzeugt werden."
            )
        result.challenges_checked = 0
        result.nai_count = len(history.get("not_an_issue") or {})
        result.history = history
        return result

    history["not_an_issue"] = not_an_issue
    save_history(region_id, history, state_dir)
    write_tasks_sqlite(region_id, not_an_issue, state_dir)
    result.challenges_checked = checked
    result.nai_count = len(not_an_issue)
    result.history = history
    if failed:
        result.warnings.append(
            "Einige Challenges konnten nicht gelesen werden; bekannte Historie bleibt. "
            + "; ".join(failed[:3])
        )
    if progress is not None:
        progress.log("MapRoulette-Historie aktualisiert")
        progress.log(f"{checked} Challenges geprüft")
        progress.log(f'{len(not_an_issue)} bekannte "Not an Issue"-Fälle')
    return result
