#!/usr/bin/env python3
"""Tiny local operator UI. Binds to 127.0.0.1 only.

    python webapp.py
    http://127.0.0.1:5000
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

from maproulette import redact
from regions import load_all_regions
from workflow import STATE_DIR, load_ui_state, read_json, run_workflow

REPO = Path(__file__).resolve().parent
app = Flask(__name__)

_lock = threading.Lock()
_busy = False
_current_region = None
_current_action = None


def _dry_run():
    return os.environ.get("OSM_STRAVA_DRY_RUN") == "1"


def _public_result(payload):
    if not payload:
        return None
    safe = {
        "ok": payload.get("ok"),
        "error": redact(payload.get("error") or ""),
        "failed_stage": payload.get("failed_stage"),
        "region_id": payload.get("region_id"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "runtime_s": payload.get("runtime_s"),
        "counts": payload.get("counts") or {},
        "challenges": [],
        "warnings": [redact(item) for item in (payload.get("warnings") or [])],
        "osm_updated_at": payload.get("osm_updated_at"),
        "osm": payload.get("osm") or {},
        "zoom": payload.get("zoom"),
        "phase": payload.get("phase"),
        "dry_run": payload.get("dry_run"),
        "uploaded": payload.get("uploaded"),
        "can_retry_upload": bool(payload.get("can_retry_upload")),
        "upload_only": bool(payload.get("upload_only")),
        "stages": payload.get("stages") or [],
        "logs": [redact(item) for item in (payload.get("logs") or [])],
        "running": bool(payload.get("running")),
        "run_dir": payload.get("run_dir"),
    }
    for item in payload.get("challenges") or []:
        safe["challenges"].append({
            "layer": item.get("layer"),
            "logical_name": item.get("logical_name"),
            "challenge_name": item.get("challenge_name"),
            "task_count": item.get("task_count") or 0,
            "created": item.get("created"),
            "skipped_empty": bool(item.get("skipped_empty")),
            "challenge_id": item.get("challenge_id"),
            "mapper_url": item.get("mapper_url"),
            "admin_url": item.get("admin_url"),
        })
    return safe


def _osm_mtime(extract_xml):
    path = Path(extract_xml)
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat()


def _retry_args(region_id):
    ui = load_ui_state(region_id)
    phase2 = ui.get("phase2") or {}
    phase1 = ui.get("phase1") or {}
    if phase2.get("can_retry_upload"):
        return "all", True
    if phase1.get("can_retry_upload"):
        return "ride-run", True
    if phase2.get("ok") is False:
        return "all", False
    return "ride-run", False


def _run_in_background(region_id, action):
    global _busy
    dry = _dry_run()
    try:
        if action == "retry":
            phase, upload_only = _retry_args(region_id)
            run_workflow(
                region_id,
                phase=phase,
                dry_run=dry,
                upload_only=upload_only,
            )
        elif action == "all":
            run_workflow(region_id, phase="all", dry_run=dry)
        else:
            run_workflow(region_id, phase="ride-run", dry_run=dry)
    finally:
        with _lock:
            _busy = False


@app.get("/")
def index():
    return HTML


@app.get("/api/regions")
def api_regions():
    items = []
    for region_id, data in load_all_regions().items():
        ui = load_ui_state(region_id)
        items.append({
            "id": region_id,
            "display_name": data["display_name"],
            "osm_updated_at": _osm_mtime(data["extract_xml"]),
            "phase1": _public_result(ui.get("phase1")),
            "phase2": _public_result(ui.get("phase2")),
        })
    return jsonify(items)


@app.get("/api/status")
def api_status():
    with _lock:
        running = _busy
        region_id = _current_region
        action = _current_action
    live_raw = read_json(STATE_DIR / "current.json", default=None)
    if not region_id and live_raw:
        region_id = live_raw.get("region_id")
    ui = load_ui_state(region_id) if region_id else None
    live = _public_result(live_raw)
    if live is not None:
        live["running"] = running
    payload = {
        "running": running,
        "region_id": region_id,
        "action": action,
        "current": live,
        "phase1": _public_result((ui or {}).get("phase1")) if ui else None,
        "phase2": _public_result((ui or {}).get("phase2")) if ui else None,
    }
    return jsonify(payload)


@app.post("/api/run")
def api_run():
    global _busy, _current_region, _current_action
    body = request.get_json(silent=True) or {}
    region_id = (body.get("region") or "").strip()
    action = (body.get("action") or "ride-run").strip()
    if action not in ("ride-run", "all", "retry"):
        return jsonify({"ok": False, "error": "Unbekannte Aktion."}), 400
    regions = load_all_regions()
    if region_id not in regions:
        return jsonify({"ok": False, "error": "Bitte eine gültige Region wählen."}), 400
    with _lock:
        if _busy:
            return jsonify({"ok": False, "error": "Es läuft bereits eine Analyse."}), 409
        _busy = True
        _current_region = region_id
        _current_action = action
    thread = threading.Thread(
        target=_run_in_background,
        args=(region_id, action),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "running": True, "region_id": region_id, "action": action})


HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenStreetMap Strava Detector</title>
  <style>
    :root {
      --bg: #f4f1ea;
      --card: #fffdf8;
      --ink: #1f1b16;
      --muted: #6b6258;
      --line: #e4d8c8;
      --ok: #2f7d4a;
      --wait: #b36b00;
      --err: #a33b2b;
      --btn: #1f4b7a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: Georgia, "Times New Roman", serif;
      background: var(--bg); color: var(--ink);
    }
    main { max-width: 720px; margin: 0 auto; padding: 48px 20px 80px; }
    h1 { font-size: 2rem; font-weight: 600; margin: 0 0 8px; }
    .lead { color: var(--muted); margin: 0 0 32px; }
    .card {
      background: var(--card); border: 1px solid var(--line);
      border-radius: 16px; padding: 24px; margin-bottom: 20px;
    }
    label { display: block; font-size: 0.95rem; margin-bottom: 8px; }
    select, button { font: inherit; width: 100%; }
    select {
      padding: 12px 14px; border-radius: 10px;
      border: 1px solid var(--line); background: white;
    }
    button.primary {
      margin-top: 18px; padding: 14px 16px; border: 0; border-radius: 12px;
      background: var(--btn); color: white; font-size: 1.15rem; cursor: pointer;
    }
    button.primary:disabled { opacity: 0.5; cursor: default; }
    button.link, a.link {
      width: auto; padding: 10px 14px; border-radius: 10px;
      background: var(--btn); color: white; border: 0; cursor: pointer;
      text-decoration: none; display: inline-block;
    }
    .stage { display: flex; gap: 12px; padding: 8px 0; color: var(--muted); }
    .stage.done { color: var(--ok); }
    .stage.running { color: var(--wait); font-weight: 600; }
    .stage.error { color: var(--err); }
    .result-row {
      display: flex; justify-content: space-between; align-items: center;
      gap: 16px; padding: 14px 0; border-top: 1px solid var(--line);
    }
    .result-row:first-child { border-top: 0; }
    .count { font-size: 1.25rem; }
    .meta { color: var(--muted); font-size: 0.95rem; line-height: 1.5; }
    .error { color: var(--err); }
    .next-hint { margin-top: 18px; color: var(--muted); }
    details { margin-top: 16px; color: var(--muted); }
    pre {
      white-space: pre-wrap; background: #f7f3ec; padding: 12px;
      border-radius: 8px; max-height: 280px; overflow: auto;
      font-size: 0.85rem;
    }
  </style>
</head>
<body>
<main>
  <h1>OpenStreetMap Strava Detector</h1>
  <p class="lead">Findet fehlende Wege aus der Strava-Heatmap und legt MapRoulette-Aufgaben an.</p>

  <section class="card">
    <label for="region">Region</label>
    <select id="region"></select>
    <button class="primary" id="start" type="button">Ride &amp; Run suchen</button>
    <p class="meta" id="region-meta"></p>
  </section>

  <section class="card" id="progress-card" hidden>
    <div id="stages"></div>
    <p class="error" id="error"></p>
    <button class="primary" id="retry" type="button" hidden>Erneut versuchen</button>
    <details>
      <summary>Technische Details</summary>
      <pre id="logs"></pre>
    </details>
  </section>

  <section class="card" id="result-card" hidden>
    <div id="results"></div>
    <p class="next-hint" id="all-hint" hidden>Wenn Ride &amp; Run bearbeitet sind:</p>
    <button class="primary" id="continue-all" type="button" hidden>Weiter mit All</button>
    <p class="meta" id="result-meta"></p>
  </section>
</main>
<script>
const STAGE_LABELS = {
  validate: "Konfiguration geprüft",
  verify_api: "MapRoulette-Zugang geprüft",
  osm: "OSM-Daten aktualisiert",
  ride: "Ride analysiert",
  run: "Run analysiert",
  all: "All analysiert",
  ride_challenge: "Ride-Challenge erstellt",
  run_challenge: "Run-Challenge erstellt",
  all_challenge: "All-Challenge erstellt",
  done: "Fertig"
};
const LAYER_LABELS = {ride: "Ride", run: "Run", all: "All"};

const regionSelect = document.getElementById("region");
const startBtn = document.getElementById("start");
const retryBtn = document.getElementById("retry");
const continueBtn = document.getElementById("continue-all");
const allHint = document.getElementById("all-hint");
const progressCard = document.getElementById("progress-card");
const resultCard = document.getElementById("result-card");
const stagesEl = document.getElementById("stages");
const logsEl = document.getElementById("logs");
const errorEl = document.getElementById("error");
const resultsEl = document.getElementById("results");
const resultMeta = document.getElementById("result-meta");
const regionMeta = document.getElementById("region-meta");

let regions = [];
let pollTimer = null;
let lastStatus = null;

function fmtTime(value) {
  if (!value) return "unbekannt";
  return value.replace("T", " ").replace("+00:00", " UTC");
}

function stageText(stage) {
  const base = STAGE_LABELS[stage.id] || stage.label || stage.id;
  if ((stage.id === "ride" || stage.id === "run" || stage.id === "all")
      && stage.counts && stage.counts.candidates != null)
    return `${base} — ${stage.counts.candidates} Kandidaten`;
  if (stage.detail) return `${base} — ${stage.detail}`;
  return base;
}

function renderStages(current) {
  const stages = (current && current.stages) || [];
  stagesEl.innerHTML = stages.filter(stage => STAGE_LABELS[stage.id]).map(stage => {
    const mark = stage.status === "done" ? "✓" : stage.status === "error" ? "!" : "…";
    return `<div class="stage ${stage.status}">${mark} ${stageText(stage)}</div>`;
  }).join("");
  logsEl.textContent = ((current && current.logs) || []).join("\n");
}

function layerRow(item) {
  const label = LAYER_LABELS[item.layer] || item.layer;
  const count = item.task_count || 0;
  if (item.skipped_empty || count === 0) {
    return `<div class="result-row"><div class="count">${label}: Keine neuen Aufgaben</div></div>`;
  }
  const url = item.mapper_url;
  const button = url
    ? `<a class="link" href="${url}" target="_blank" rel="noopener">In MapRoulette öffnen</a>`
    : `<span class="meta">Noch nicht hochgeladen</span>`;
  return `<div class="result-row"><div class="count">${label}: ${count} Aufgaben</div>${button}</div>`;
}

function combinedChallenges(phase1, phase2) {
  const rows = [];
  const seen = new Set();
  for (const source of [phase1, phase2]) {
    for (const item of (source && source.challenges) || []) {
      if (seen.has(item.layer)) continue;
      seen.add(item.layer);
      rows.push(item);
    }
  }
  const order = {ride: 0, run: 1, all: 2};
  rows.sort((a, b) => (order[a.layer] ?? 9) - (order[b.layer] ?? 9));
  return rows;
}

function renderResults(phase1, phase2, running) {
  const rows = combinedChallenges(phase1, phase2);
  const phase1Ok = phase1 && phase1.ok === true;
  const phase2Ok = phase2 && phase2.ok === true;
  if (!rows.length && !phase1Ok) {
    resultCard.hidden = true;
    continueBtn.hidden = true;
    allHint.hidden = true;
    return;
  }
  resultCard.hidden = false;
  resultsEl.innerHTML = rows.map(layerRow).join("");
  const showAll = phase1Ok && !running && !phase2Ok;
  allHint.hidden = !showAll;
  continueBtn.hidden = !showAll;
  continueBtn.disabled = running;
  const bits = [];
  const osm = (phase2 && phase2.osm && phase2.osm.timestamp_after)
    ? phase2.osm
    : ((phase1 && phase1.osm) || {});
  const osmTs = osm.timestamp_after || (phase2 && phase2.osm_updated_at) || (phase1 && phase1.osm_updated_at);
  if (osmTs) bits.push("OSM-Stand: " + fmtTime(osmTs));
  if (osm.mode) bits.push("OSM-Modus: " + osm.mode);
  if (osm.source_lag) bits.push("Lag: " + osm.source_lag);
  resultMeta.textContent = bits.join(" · ");
}

function activeSnapshot(status) {
  if (status && status.current && status.current.running) return status.current;
  if (status && status.current && status.running) return status.current;
  const p2 = status && status.phase2;
  const p1 = status && status.phase1;
  if (p2 && (p2.ok === false || (p2.stages && p2.stages.length))) return p2;
  return p1;
}

function showError(status) {
  const snap = activeSnapshot(status);
  const failed = snap && snap.ok === false && snap.error;
  if (failed && !(status && status.running)) {
    errorEl.textContent = snap.error;
    retryBtn.hidden = false;
    retryBtn.textContent = snap.can_retry_upload ? "Upload wiederholen" : "Erneut versuchen";
  } else {
    errorEl.textContent = "";
    retryBtn.hidden = true;
  }
}

function setRunning(running, action) {
  startBtn.disabled = running;
  continueBtn.disabled = running;
  retryBtn.disabled = running;
  regionSelect.disabled = running;
  if (running) {
    startBtn.textContent = action === "all" ? "All läuft …" : "Analyse läuft …";
  } else {
    startBtn.textContent = "Ride & Run suchen";
  }
}

function selectedRegion() {
  return regions.find(item => item.id === regionSelect.value);
}

function applyRegionState(region, running) {
  if (!region) return;
  regionMeta.textContent = region.osm_updated_at
    ? "Letztes OSM-Update: " + fmtTime(region.osm_updated_at)
    : "Noch kein lokales OSM-Update vorhanden.";
  const p1 = region.phase1;
  const p2 = region.phase2;
  const has = (p1 && p1.stages && p1.stages.length) || (p2 && p2.stages && p2.stages.length);
  if (!has) return;
  progressCard.hidden = false;
  const snap = (p2 && p2.stages && p2.stages.length) ? p2 : p1;
  renderStages(snap);
  renderResults(p1, p2, running);
  showError({phase1: p1, phase2: p2, running});
}

async function loadRegions() {
  const res = await fetch("/api/regions");
  regions = await res.json();
  const previous = regionSelect.value;
  regionSelect.innerHTML = regions.map(item =>
    `<option value="${item.id}">${item.display_name}</option>`
  ).join("");
  if (previous) regionSelect.value = previous;
  applyRegionState(selectedRegion(), false);
}

async function refreshStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  lastStatus = data;
  setRunning(data.running, data.action);
  const live = data.current;
  if (live && (data.running || (live.stages && live.stages.length))) {
    progressCard.hidden = false;
    renderStages(live);
  }
  const region = selectedRegion() || {};
  const phase1 = data.phase1 || region.phase1;
  const phase2 = data.phase2 || region.phase2;
  renderResults(phase1, phase2, data.running);
  showError(data);
  if (!data.running && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
    loadRegions();
  }
}

async function startAction(action) {
  errorEl.textContent = "";
  retryBtn.hidden = true;
  progressCard.hidden = false;
  setRunning(true, action);
  const res = await fetch("/api/run", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({region: regionSelect.value, action})
  });
  const data = await res.json();
  if (res.status === 409) {
    if (!pollTimer) pollTimer = setInterval(refreshStatus, 1500);
    return;
  }
  if (!data.ok) {
    errorEl.textContent = data.error || "Start fehlgeschlagen.";
    retryBtn.hidden = false;
    setRunning(false);
    return;
  }
  if (!pollTimer) pollTimer = setInterval(refreshStatus, 1500);
}

regionSelect.addEventListener("change", () => applyRegionState(selectedRegion(), false));
startBtn.addEventListener("click", () => startAction("ride-run"));
continueBtn.addEventListener("click", () => startAction("all"));
retryBtn.addEventListener("click", () => startAction("retry"));
loadRegions().then(refreshStatus);
</script>
</body>
</html>
"""


def main():
    print("OpenStreetMap Strava Detector")
    print("Nur lokal: http://127.0.0.1:5000")
    if _dry_run():
        print("OSM_STRAVA_DRY_RUN=1 — MapRoulette wird nicht beschrieben")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
