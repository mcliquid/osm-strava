#!/usr/bin/env bash
# Refresh local OSM detector extracts from Geofabrik, then clip with
# osmium extract --strategy=complete_ways.
#
#   ./update-osm.sh mallorca
#   ./update-osm.sh bodenseekreis
#   ./update-osm.sh mallorca --fresh
#
# Default mode uses the Geofabrik daily extract. --fresh applies official
# planet.openstreetmap.org replication to an unclipped working copy of that
# extract, then still clips with osmium extract --strategy=complete_ways.
# Never pass osmupdate -B: clipping diffs reintroduces missing node refs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONF_FILE="${SCRIPT_DIR}/osm-regions.conf"
OSM_DIR="${SCRIPT_DIR}/osm-data"
TMP_ROOT="${OSM_DIR}/.update-tmp"
MAX_BACKUPS=3
EXTRACT_STRATEGY="complete_ways"
SIZE_WARN_FACTOR="1.5"
SIZE_FAIL_FACTOR="3.0"

FORCE=0
FRESH=0
BOOTSTRAP=0
SHOW_CONFIG=0
LIST_ONLY=0
REGION_ID=""
UPDATE_MODE="geofabrik"

STATUS="FAILED"
SOURCE_STATUS="FAILED"
EXTRACT_STATUS="SKIPPED"
XML_STATUS="SKIPPED"
SUMMARY_PRINTED=0
RUN_STARTED="$(date -u +%s)"
SOURCE_RUNTIME=0
EXTRACT_RUNTIME=0
XML_RUNTIME=0
TOTAL_RUNTIME=0

GEOFABRIK_URL=""
SOURCE_PBF=""
BOUNDARY=""
EXTRACT_PBF=""
EXTRACT_XML=""
BBOX_LON_MIN=""
BBOX_LON_MAX=""
BBOX_LAT_MIN=""
BBOX_LAT_MAX=""
SOURCE_MIN_BYTES=0
SOURCE_MAX_BYTES=0
MIN_NODES=0
MIN_WAYS=0
MIN_RELS=0

SOURCE_META=""
STAMP_FILE=""
BACKUP_DIR=""
SOURCE_TMP=""
WORKING_PBF=""
WORKING_TMP=""
OSMUPDATE_TEMP=""
EXTRACT_INPUT_PBF=""
EXTRACT_TMP=""
XML_TMP=""
CHECKREFS_LOG=""
CLEANUP_SOURCE_TMP=1
CLEANUP_WORKING_TMP=1
CLEANUP_EXTRACT_TMP=1
CLEANUP_XML_TMP=1

FAIL_REASON=""
BACKUP_PATH=""
REMOTE_ETAG=""
REMOTE_LAST_MODIFIED=""
REMOTE_LENGTH=""
SOURCE_ETAG=""
SOURCE_LAST_MODIFIED=""
SOURCE_SIZE=0
EXTRACT_SIZE=0
XML_SIZE=0
BEFORE_TS=""
AFTER_TS=""
BEFORE_NODES=0
AFTER_NODES=0
BEFORE_WAYS=0
AFTER_WAYS=0
BEFORE_RELS=0
AFTER_RELS=0
AFTER_DATA_TS_FIRST=""
AFTER_DATA_TS_LAST=""
XML_TS=""
FRESH_START_TS=""
FRESH_AFTER_TS=""
FRESH_DIFFS=""
PLANET_TS=""
REPLICATION_NOTE="Geofabrik, not minutely"
WORKING_STATE=""

usage() {
  cat <<'EOF'
Usage:
  update-osm.sh <region> [--force]
  update-osm.sh <region> --fresh [--bootstrap] [--force]
  update-osm.sh --show-config <region>
  update-osm.sh --list
  update-osm.sh --help

Refresh a detector OSM extract and clip it with
osmium extract --strategy=complete_ways.

Regions are defined in osm-regions.conf.

  --force       Rebuild the regional extract and XML even if the source
                and boundary file have not changed.
  --fresh       Advance an unclipped working copy of the Geofabrik source
                with planet.openstreetmap.org replication, then extract.
                The production workflow always passes --fresh. Direct
                updater default remains Geofabrik-daily.
  --bootstrap   With --fresh, replace the working copy from Geofabrik
                before applying replication.
EOF
}

format_int() {
  python3 -c 'import sys; print(f"{int(sys.argv[1]):,}")' "$1"
}

format_mib() {
  python3 -c 'import sys; print(f"{int(sys.argv[1]) / 1048576:.1f} MiB")' "$1"
}

format_hms() {
  local seconds="$1"
  if (( seconds < 0 )); then
    seconds=0
  fi
  local hours=$((seconds / 3600))
  local minutes=$(((seconds % 3600) / 60))
  local secs=$((seconds % 60))
  printf '%02d:%02d:%02d' "$hours" "$minutes" "$secs"
}

ts_to_epoch() {
  date -u -d "$1" +%s
}

compact_ts() {
  local ts="$1"
  ts="${ts//-/}"
  ts="${ts//:/}"
  printf '%s' "$ts"
}

file_mtime() {
  stat -c %Y "$1"
}

file_size() {
  stat -c %s "$1"
}

relpath() {
  local path="$1"
  if [[ "$path" == "$SCRIPT_DIR"/* ]]; then
    printf '%s' "${path#"$SCRIPT_DIR"/}"
  else
    printf '%s' "$path"
  fi
}

print_summary() {
  TOTAL_RUNTIME=$(( $(date -u +%s) - RUN_STARTED ))
  echo "------------------------------------------------------------"
  echo "OSM update summary"
  echo "------------------------------------------------------------"
  echo "Status:              ${STATUS}"
  echo "Region:              ${REGION_ID:-unknown}"
  echo "Update mode:         ${UPDATE_MODE}"
  echo "Source status:       ${SOURCE_STATUS}"
  echo "Extract status:      ${EXTRACT_STATUS}"
  echo "XML status:          ${XML_STATUS}"
  if [[ -n "$FAIL_REASON" ]]; then
    echo "Reason:              ${FAIL_REASON}"
  fi
  if [[ "$STATUS" != "SUCCESS" ]]; then
    echo "Known-good detector files were NOT replaced."
  fi
  if [[ -n "$FRESH_START_TS" ]]; then
    echo "Replication start:   ${FRESH_START_TS}"
  fi
  if [[ -n "$FRESH_AFTER_TS" ]]; then
    echo "Replication result:  ${FRESH_AFTER_TS}"
  fi
  if [[ -n "$PLANET_TS" ]]; then
    echo "Planet minutely:     ${PLANET_TS}"
  fi
  local lag_ts="${FRESH_AFTER_TS:-$AFTER_TS}"
  if [[ -n "$lag_ts" ]]; then
    echo "Source timestamp:    ${lag_ts}"
    local now_epoch after_epoch lag live_epoch live_lag
    now_epoch="$(date -u +%s)"
    if after_epoch="$(ts_to_epoch "$lag_ts" 2>/dev/null)"; then
      lag=$((now_epoch - after_epoch))
      echo "Source lag:          $(format_hms "$lag") (${REPLICATION_NOTE})"
      if [[ -n "$PLANET_TS" ]] && live_epoch="$(ts_to_epoch "$PLANET_TS" 2>/dev/null)"; then
        live_lag=$((live_epoch - after_epoch))
        if (( live_lag < 0 )); then
          live_lag=0
        fi
        echo "Lag behind planet:   $(format_hms "$live_lag")"
      fi
    fi
  fi
  if [[ -n "$FRESH_DIFFS" ]]; then
    echo "Replication diffs:   ${FRESH_DIFFS}"
  fi
  if [[ "$UPDATE_MODE" == "fresh" && -n "$WORKING_PBF" && -f "$WORKING_PBF" ]]; then
    echo "Working PBF:         $(relpath "$WORKING_PBF")"
  fi
  if (( SOURCE_SIZE > 0 )); then
    echo "Source PBF size:     $(format_mib "$SOURCE_SIZE")"
  fi
  if (( EXTRACT_SIZE > 0 )); then
    echo "Extract PBF size:    $(format_mib "$EXTRACT_SIZE")"
  fi
  if (( XML_SIZE > 0 )); then
    echo "XML size:            $(format_mib "$XML_SIZE")"
  fi
  if (( BEFORE_NODES > 0 || AFTER_NODES > 0 )); then
    echo "Nodes:               $(format_int "$BEFORE_NODES") -> $(format_int "$AFTER_NODES")"
    echo "Ways:                $(format_int "$BEFORE_WAYS") -> $(format_int "$AFTER_WAYS")"
    echo "Relations:           $(format_int "$BEFORE_RELS") -> $(format_int "$AFTER_RELS")"
  fi
  if [[ -n "$BACKUP_PATH" ]]; then
    echo "Backup:              $(relpath "$BACKUP_PATH")"
  fi
  if [[ -n "$SOURCE_PBF" ]]; then
    echo "Source PBF:          $(relpath "$SOURCE_PBF")"
  fi
  if [[ -n "$EXTRACT_PBF" ]]; then
    echo "Extract PBF:         $(relpath "$EXTRACT_PBF")"
  fi
  if [[ -n "$EXTRACT_XML" ]]; then
    echo "Extract XML:         $(relpath "$EXTRACT_XML")"
  fi
  echo "Source runtime:      $(format_hms "$SOURCE_RUNTIME")"
  echo "Extract runtime:     $(format_hms "$EXTRACT_RUNTIME")"
  echo "XML conversion:      $(format_hms "$XML_RUNTIME")"
  echo "Total runtime:       $(format_hms "$TOTAL_RUNTIME")"
  echo "------------------------------------------------------------"
}

fail() {
  FAIL_REASON="$*"
  STATUS="FAILED"
  print_summary
  SUMMARY_PRINTED=1
  echo "ERROR: $FAIL_REASON" >&2
  exit 1
}

# Never replace a newer promoted extract with an older Geofabrik/working source.
reject_downgrade() {
  local candidate_ts="$1"
  local candidate_label="$2"
  if [[ -z "$BEFORE_TS" ]]; then
    return 0
  fi
  local before_epoch candidate_epoch
  if ! before_epoch="$(ts_to_epoch "$BEFORE_TS" 2>/dev/null)"; then
    echo "WARNING: could not parse current extract timestamp ${BEFORE_TS}" >&2
    return 0
  fi
  if [[ -z "$candidate_ts" ]]; then
    fail "OSM update rejected because it would replace newer local data with an older source. Current extract timestamp=${BEFORE_TS}; ${candidate_label} has no timestamp."
  fi
  if ! candidate_epoch="$(ts_to_epoch "$candidate_ts" 2>/dev/null)"; then
    fail "OSM update rejected because it would replace newer local data with an older source. Could not parse ${candidate_label} timestamp (${candidate_ts})."
  fi
  if (( candidate_epoch < before_epoch )); then
    fail "OSM update rejected because it would replace newer local data with an older source. Current extract timestamp=${BEFORE_TS}; ${candidate_label} timestamp=${candidate_ts}."
  fi
}

on_exit() {
  local rc=$?
  if [[ "$CLEANUP_SOURCE_TMP" -eq 1 && -n "$SOURCE_TMP" && -f "$SOURCE_TMP" ]]; then
    rm -f "$SOURCE_TMP"
  fi
  if [[ "$CLEANUP_WORKING_TMP" -eq 1 && -n "$WORKING_TMP" && -f "$WORKING_TMP" ]]; then
    rm -f "$WORKING_TMP"
  fi
  if [[ "$CLEANUP_EXTRACT_TMP" -eq 1 && -n "$EXTRACT_TMP" && -f "$EXTRACT_TMP" ]]; then
    rm -f "$EXTRACT_TMP"
  fi
  if [[ "$CLEANUP_XML_TMP" -eq 1 && -n "$XML_TMP" && -f "$XML_TMP" ]]; then
    rm -f "$XML_TMP"
  fi
  if [[ "$SUMMARY_PRINTED" -eq 0 ]]; then
    if [[ -z "$FAIL_REASON" ]]; then
      FAIL_REASON="Update aborted (exit ${rc})"
    fi
    STATUS="FAILED"
    print_summary || true
    SUMMARY_PRINTED=1
  fi
  exit "$rc"
}

trap on_exit EXIT

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Required command not found: $1"
  fi
}

list_regions() {
  python3 - "$CONF_FILE" <<'PY'
import sys
path = sys.argv[1]
regions = []
with open(path, encoding="utf-8") as handle:
    for raw in handle:
        line = raw.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            regions.append(line[1:-1].strip())
if not regions:
    sys.exit("No regions found in " + path)
print("\n".join(regions))
PY
}

load_region() {
  local want="$1"
  local parsed
  set +e
  parsed="$(
    python3 - "$CONF_FILE" "$want" "$SCRIPT_DIR" <<'PY'
import os, sys
conf, want, root = sys.argv[1], sys.argv[2], sys.argv[3]
required = [
    "geofabrik_url", "source_pbf", "boundary", "extract_pbf", "extract_xml",
    "bbox_lon_min", "bbox_lon_max", "bbox_lat_min", "bbox_lat_max",
    "source_min_bytes", "source_max_bytes", "min_nodes", "min_ways", "min_relations",
]
data = {}
current = None
found = False
with open(conf, encoding="utf-8") as handle:
    for raw in handle:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current != want or "=" not in line:
            continue
        found = True
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
if not found:
    sys.stderr.write(f"Unknown region: {want}\n")
    sys.exit(2)
missing = [key for key in required if key not in data]
if missing:
    sys.stderr.write(f"Region {want} is missing keys: {', '.join(missing)}\n")
    sys.exit(2)

def abs_path(value):
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(root, value))

mapping = {
    "GEOFABRIK_URL": data["geofabrik_url"],
    "SOURCE_PBF": abs_path(data["source_pbf"]),
    "BOUNDARY": abs_path(data["boundary"]),
    "EXTRACT_PBF": abs_path(data["extract_pbf"]),
    "EXTRACT_XML": abs_path(data["extract_xml"]),
    "BBOX_LON_MIN": data["bbox_lon_min"],
    "BBOX_LON_MAX": data["bbox_lon_max"],
    "BBOX_LAT_MIN": data["bbox_lat_min"],
    "BBOX_LAT_MAX": data["bbox_lat_max"],
    "SOURCE_MIN_BYTES": data["source_min_bytes"],
    "SOURCE_MAX_BYTES": data["source_max_bytes"],
    "MIN_NODES": data["min_nodes"],
    "MIN_WAYS": data["min_ways"],
    "MIN_RELS": data["min_relations"],
}
for key, value in mapping.items():
    print(f"{key}={value!r}")
PY
  )"
  local rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    fail "Unknown region or invalid configuration: ${want}"
  fi
  eval "$parsed"
}

print_region_config() {
  echo "Region:            ${REGION_ID}"
  echo "Geofabrik URL:     ${GEOFABRIK_URL}"
  echo "Source PBF:        $(relpath "$SOURCE_PBF")"
  echo "Boundary:          $(relpath "$BOUNDARY")"
  echo "Extract PBF:       $(relpath "$EXTRACT_PBF")"
  echo "Extract XML:       $(relpath "$EXTRACT_XML")"
  echo "Extract strategy:  ${EXTRACT_STRATEGY}"
  echo "Update mode:       ${UPDATE_MODE}"
  if [[ -n "$WORKING_PBF" ]]; then
    echo "Working PBF:       $(relpath "$WORKING_PBF")"
  fi
  echo "Safety bbox:       lon ${BBOX_LON_MIN}..${BBOX_LON_MAX}, lat ${BBOX_LAT_MIN}..${BBOX_LAT_MAX} (requested boundary, not complete_ways nodes)"
  echo "Source size range: $(format_mib "$SOURCE_MIN_BYTES") .. $(format_mib "$SOURCE_MAX_BYTES")"
  echo "Min objects:       nodes>=$(format_int "$MIN_NODES") ways>=$(format_int "$MIN_WAYS") relations>=$(format_int "$MIN_RELS")"
}

INFO_ERROR=""

try_load_osm_info() {
  local file="$1"
  local extended="${2:-1}"
  local json_file parsed
  INFO_ERROR=""
  json_file="$(mktemp)"
  if [[ "$extended" -eq 1 ]]; then
    if ! osmium fileinfo -e -j "$file" >"$json_file"; then
      rm -f "$json_file"
      INFO_ERROR="osmium fileinfo -e failed for ${file}"
      return 1
    fi
  else
    if ! osmium fileinfo -j "$file" >"$json_file"; then
      rm -f "$json_file"
      INFO_ERROR="osmium fileinfo failed for ${file}"
      return 1
    fi
  fi
  parsed="$(
    python3 -c '
import json, sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
option = data.get("header", {}).get("option", {})
timestamp = option.get("osmosis_replication_timestamp") or option.get("timestamp") or ""
bbox = data.get("data", {}).get("bbox") or []
count = data.get("data", {}).get("count", {})
size = int(data.get("file", {}).get("size") or 0)
data_ts = data.get("data", {}).get("timestamp") or {}

def emit(name, value):
    print(f"{name}={value}")

emit("INFO_TIMESTAMP", timestamp)
emit("INFO_SIZE", size)
if isinstance(bbox, list) and len(bbox) == 4:
    emit("INFO_MIN_LON", bbox[0])
    emit("INFO_MIN_LAT", bbox[1])
    emit("INFO_MAX_LON", bbox[2])
    emit("INFO_MAX_LAT", bbox[3])
else:
    emit("INFO_MIN_LON", "")
    emit("INFO_MIN_LAT", "")
    emit("INFO_MAX_LON", "")
    emit("INFO_MAX_LAT", "")
boxes = data.get("header", {}).get("boxes") or []
if boxes and isinstance(boxes[0], list) and len(boxes[0]) == 4:
    emit("INFO_HDR_MIN_LON", boxes[0][0])
    emit("INFO_HDR_MIN_LAT", boxes[0][1])
    emit("INFO_HDR_MAX_LON", boxes[0][2])
    emit("INFO_HDR_MAX_LAT", boxes[0][3])
else:
    emit("INFO_HDR_MIN_LON", "")
    emit("INFO_HDR_MIN_LAT", "")
    emit("INFO_HDR_MAX_LON", "")
    emit("INFO_HDR_MAX_LAT", "")
emit("INFO_NODES", int(count.get("nodes") or 0))
emit("INFO_WAYS", int(count.get("ways") or 0))
emit("INFO_RELS", int(count.get("relations") or 0))
emit("INFO_DATA_TS_FIRST", data_ts.get("first") or "")
emit("INFO_DATA_TS_LAST", data_ts.get("last") or "")
' "$json_file"
  )" || {
    rm -f "$json_file"
    INFO_ERROR="Could not parse osmium fileinfo JSON for ${file}"
    return 1
  }
  rm -f "$json_file"
  eval "$parsed"
  return 0
}

load_osm_info() {
  if ! try_load_osm_info "$1" "${2:-1}"; then
    fail "${INFO_ERROR}"
  fi
}

validate_bbox_inside_window() {
  python3 -c '
import sys
min_lon, min_lat, max_lon, max_lat = map(float, sys.argv[1:5])
lon_min, lon_max, lat_min, lat_max = map(float, sys.argv[5:9])
ok = (
    lon_min <= min_lon <= lon_max
    and lon_min <= max_lon <= lon_max
    and lat_min <= min_lat <= lat_max
    and lat_min <= max_lat <= lat_max
    and min_lon < max_lon
    and min_lat < max_lat
)
sys.exit(0 if ok else 1)
' "$1" "$2" "$3" "$4" "$BBOX_LON_MIN" "$BBOX_LON_MAX" "$BBOX_LAT_MIN" "$BBOX_LAT_MAX"
}

bboxes_overlap() {
  python3 -c '
import sys
a_minx, a_miny, a_maxx, a_maxy = map(float, sys.argv[1:5])
b_minx, b_miny, b_maxx, b_maxy = map(float, sys.argv[5:9])
overlap = not (
    a_maxx < b_minx or b_maxx < a_minx or a_maxy < b_miny or b_maxy < a_miny
)
sys.exit(0 if overlap else 1)
' "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8"
}

load_boundary_bbox() {
  local parsed
  parsed="$(
    python3 - "$BOUNDARY" <<'PY'
import json, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
features = []
ptype = payload.get("type")
if ptype == "FeatureCollection":
    features = payload.get("features") or []
elif ptype == "Feature":
    features = [payload]
else:
    features = [{"geometry": payload}]

minx = miny = 1e9
maxx = maxy = -1e9
ncoords = 0

def walk(obj):
    global minx, miny, maxx, maxy, ncoords
    if not isinstance(obj, list) or not obj:
        return
    if isinstance(obj[0], (int, float)) and len(obj) >= 2 and not isinstance(obj[0], list):
        lon, lat = float(obj[0]), float(obj[1])
        ncoords += 1
        minx = min(minx, lon)
        maxx = max(maxx, lon)
        miny = min(miny, lat)
        maxy = max(maxy, lat)
        return
    for item in obj:
        walk(item)

for feature in features:
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    if not geometry:
        continue
    if geometry.get("type") not in ("Polygon", "MultiPolygon"):
        continue
    walk(geometry.get("coordinates"))

if ncoords == 0:
    sys.stderr.write(f"No polygon/multipolygon coordinates in {path}\n")
    sys.exit(2)
print(f"BOUNDARY_MIN_LON={minx}")
print(f"BOUNDARY_MIN_LAT={miny}")
print(f"BOUNDARY_MAX_LON={maxx}")
print(f"BOUNDARY_MAX_LAT={maxy}")
PY
  )" || fail "Could not read polygon bbox from $(relpath "$BOUNDARY")"
  eval "$parsed"
  echo "Boundary bbox:       (${BOUNDARY_MIN_LON}, ${BOUNDARY_MIN_LAT}, ${BOUNDARY_MAX_LON}, ${BOUNDARY_MAX_LAT})"
  if ! validate_bbox_inside_window "$BOUNDARY_MIN_LON" "$BOUNDARY_MIN_LAT" "$BOUNDARY_MAX_LON" "$BOUNDARY_MAX_LAT"; then
    fail "Requested boundary is outside the safety window (lon ${BBOX_LON_MIN}..${BBOX_LON_MAX}, lat ${BBOX_LAT_MIN}..${BBOX_LAT_MAX}): (${BOUNDARY_MIN_LON}, ${BOUNDARY_MIN_LAT}, ${BOUNDARY_MAX_LON}, ${BOUNDARY_MAX_LAT})"
  fi
}

validate_data_overlaps_boundary() {
  local label="$1"
  local min_lon="$2"
  local min_lat="$3"
  local max_lon="$4"
  local max_lat="$5"
  if [[ -z "$min_lon" ]]; then
    fail "${label} has no data bounding box"
  fi
  if ! bboxes_overlap "$min_lon" "$min_lat" "$max_lon" "$max_lat" \
      "$BOUNDARY_MIN_LON" "$BOUNDARY_MIN_LAT" "$BOUNDARY_MAX_LON" "$BOUNDARY_MAX_LAT"; then
    fail "${label} data bounding box does not overlap the requested boundary (${BOUNDARY_MIN_LON}, ${BOUNDARY_MIN_LAT}, ${BOUNDARY_MAX_LON}, ${BOUNDARY_MAX_LAT}): (${min_lon}, ${min_lat}, ${max_lon}, ${max_lat})"
  fi
}

read_source_meta() {
  SOURCE_ETAG=""
  SOURCE_LAST_MODIFIED=""
  if [[ -f "$SOURCE_META" ]]; then
    # shellcheck disable=SC1090
    source "$SOURCE_META"
    SOURCE_ETAG="${etag:-}"
    SOURCE_LAST_MODIFIED="${last_modified:-}"
  fi
}

write_source_meta() {
  local etag="$1"
  local last_modified="$2"
  local length="$3"
  mkdir -p "$(dirname "$SOURCE_META")"
  cat >"$SOURCE_META" <<EOF
etag='${etag//\'/}'
last_modified='${last_modified//\'/}'
content_length='${length}'
url='${GEOFABRIK_URL}'
EOF
}

parse_http_headers_file() {
  python3 - "$1" <<'PY'
import re, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read().replace("\r", "")
headers = {}
# curl -I writes blank-line separated HTTP responses. wget -S prefixes
# header lines with two spaces and interleaves progress text.
blocks = []
current = []
for line in raw.splitlines():
    stripped = line.strip()
    if re.match(r"HTTP/\d", stripped):
        if current:
            blocks.append(current)
        current = [stripped]
        continue
    if stripped == "":
        if current:
            blocks.append(current)
            current = []
        continue
    if current is not None and ":" in stripped:
        current.append(stripped)
if current:
    blocks.append(current)
chosen = None
for block in reversed(blocks):
    status = block[0]
    if re.search(r"\s200\b", status):
        chosen = block
        break
if chosen is None and blocks:
    chosen = blocks[-1]
if not chosen:
    sys.exit(2)
parsed = {}
for line in chosen[1:]:
    key, value = line.split(":", 1)
    parsed[key.strip().lower()] = value.strip()
print("REMOTE_ETAG=" + repr(parsed.get("etag", "")))
print("REMOTE_LAST_MODIFIED=" + repr(parsed.get("last-modified", "")))
print("REMOTE_LENGTH=" + repr(parsed.get("content-length", "")))
PY
}

fetch_remote_headers() {
  local headers parsed wget_out
  REMOTE_ETAG=""
  REMOTE_LAST_MODIFIED=""
  REMOTE_LENGTH=""
  headers="$(mktemp)"
  wget_out="$(mktemp)"
  parsed=""
  if curl -sI -L --connect-timeout 15 --max-time 45 --retry 2 --retry-delay 2 \
      -A "osm-strava-update/1.0" -o "$headers" "$GEOFABRIK_URL" \
      && [[ -s "$headers" ]]; then
    parsed="$(parse_http_headers_file "$headers" || true)"
  fi
  if [[ -z "$parsed" ]]; then
    echo "WARNING: curl HEAD failed; retrying Geofabrik with wget --spider" >&2
    if wget -S --spider --timeout=45 --tries=2 -U "osm-strava-update/1.0" \
        "$GEOFABRIK_URL" >"$wget_out" 2>&1; then
      parsed="$(parse_http_headers_file "$wget_out" || true)"
    fi
  fi
  rm -f "$headers" "$wget_out"
  if [[ -z "$parsed" ]]; then
    return 1
  fi
  eval "$parsed"
  echo "Remote Last-Modified: ${REMOTE_LAST_MODIFIED:-unknown}"
  echo "Remote ETag:          ${REMOTE_ETAG:-unknown}"
  if [[ -n "$REMOTE_LENGTH" ]]; then
    echo "Remote Content-Length: $(format_int "$REMOTE_LENGTH") bytes"
  fi
  return 0
}

fetch_planet_minutely_timestamp() {
  local state
  PLANET_TS=""
  state="$(mktemp)"
  if ! curl -fsL --connect-timeout 15 --max-time 30 --retry 2 --retry-delay 2 \
      -A "osm-strava-update/1.0" \
      -o "$state" "https://planet.openstreetmap.org/replication/minute/state.txt"; then
    rm -f "$state"
    echo "WARNING: could not read planet.openstreetmap.org minutely state.txt" >&2
    return 1
  fi
  PLANET_TS="$(
    python3 - "$state" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
for raw in text.splitlines():
    line = raw.strip()
    if line.startswith("timestamp="):
        print(line.split("=", 1)[1].replace(r"\:", ":"))
        break
PY
  )"
  rm -f "$state"
  if [[ -z "$PLANET_TS" ]]; then
    echo "WARNING: planet minutely state.txt had no timestamp" >&2
    return 1
  fi
  echo "Planet minutely:      ${PLANET_TS}"
}

source_is_current() {
  if [[ ! -f "$SOURCE_PBF" || ! -s "$SOURCE_PBF" ]]; then
    return 1
  fi
  read_source_meta
  if [[ -n "$REMOTE_ETAG" && -n "$SOURCE_ETAG" && "$REMOTE_ETAG" == "$SOURCE_ETAG" ]]; then
    return 0
  fi
  if [[ -n "$REMOTE_LAST_MODIFIED" && -n "$SOURCE_LAST_MODIFIED" && "$REMOTE_LAST_MODIFIED" == "$SOURCE_LAST_MODIFIED" ]]; then
    if [[ -z "$REMOTE_LENGTH" || "$(file_size "$SOURCE_PBF")" == "$REMOTE_LENGTH" ]]; then
      return 0
    fi
  fi
  return 1
}

validate_source_pbf() {
  local file="$1"
  local size
  size="$(file_size "$file")"
  if [[ ! -s "$file" ]]; then
    fail "Geofabrik PBF is empty: $(relpath "$file")"
  fi
  if (( size < SOURCE_MIN_BYTES )); then
    fail "Geofabrik PBF is implausibly small ($(format_mib "$size")); expected at least $(format_mib "$SOURCE_MIN_BYTES")"
  fi
  if (( size > SOURCE_MAX_BYTES )); then
    fail "Geofabrik PBF is implausibly large ($(format_mib "$size")); expected at most $(format_mib "$SOURCE_MAX_BYTES")"
  fi
  if ! osmium fileinfo "$file" >/dev/null; then
    fail "osmium fileinfo cannot read Geofabrik PBF $(relpath "$file")"
  fi
}

download_source() {
  echo "Downloading ${GEOFABRIK_URL}"
  mkdir -p "$(dirname "$SOURCE_PBF")"
  rm -f "$SOURCE_TMP" "$SOURCE_TMP_LEGACY"
  if ! curl -fL --retry 3 --retry-delay 2 --max-time 3600 \
      -A "osm-strava-update/1.0" \
      -o "$SOURCE_TMP" "$GEOFABRIK_URL"; then
    fail "Download failed: ${GEOFABRIK_URL}"
  fi
  validate_source_pbf "$SOURCE_TMP"
  mv -f "$SOURCE_TMP" "$SOURCE_PBF"
  CLEANUP_SOURCE_TMP=0
  write_source_meta "$REMOTE_ETAG" "$REMOTE_LAST_MODIFIED" "$(file_size "$SOURCE_PBF")"
}

derived_is_current() {
  if [[ "$FORCE" -eq 1 ]]; then
    return 1
  fi
  if [[ ! -f "$EXTRACT_PBF" || ! -s "$EXTRACT_PBF" ]]; then
    return 1
  fi
  if [[ ! -f "$EXTRACT_XML" || ! -s "$EXTRACT_XML" ]]; then
    return 1
  fi
  if [[ ! -f "$STAMP_FILE" || ! -f "$EXTRACT_INPUT_PBF" ]]; then
    return 1
  fi
  python3 - "$STAMP_FILE" "$EXTRACT_INPUT_PBF" "$BOUNDARY" "$EXTRACT_STRATEGY" "$GEOFABRIK_URL" "$UPDATE_MODE" <<'PY'
import os, sys
stamp_path, source, boundary, strategy, url, mode = sys.argv[1:]
data = {}
with open(stamp_path, encoding="utf-8") as handle:
    for raw in handle:
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
ok = (
    data.get("url") == url
    and data.get("strategy") == strategy
    and data.get("mode", "geofabrik") == mode
    and data.get("extract_from", source) == source
    and data.get("source_size") == str(os.path.getsize(source))
    and data.get("source_mtime") == str(int(os.path.getmtime(source)))
    and data.get("boundary") == boundary
    and data.get("boundary_mtime") == str(int(os.path.getmtime(boundary)))
)
sys.exit(0 if ok else 1)
PY
}

write_stamp() {
  mkdir -p "$(dirname "$STAMP_FILE")"
  cat >"$STAMP_FILE" <<EOF
url=${GEOFABRIK_URL}
strategy=${EXTRACT_STRATEGY}
mode=${UPDATE_MODE}
extract_from=${EXTRACT_INPUT_PBF}
source_size=$(file_size "$EXTRACT_INPUT_PBF")
source_mtime=$(file_mtime "$EXTRACT_INPUT_PBF")
boundary=${BOUNDARY}
boundary_mtime=$(file_mtime "$BOUNDARY")
EOF
}

validate_check_refs() {
  local file="$1"
  local rc=0
  echo "Checking referential integrity (osmium check-refs)..."
  set +e
  osmium check-refs --no-progress "$file" >"$CHECKREFS_LOG" 2>&1
  rc=$?
  set -e
  if [[ -s "$CHECKREFS_LOG" ]]; then
    cat "$CHECKREFS_LOG"
  fi
  set +e
  python3 - "$CHECKREFS_LOG" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# complete_ways must not leave way→node holes. Relation members outside the
# polygon can still be missing; this check does not use --check-relations.
missing = 0
match = re.search(
    r"([0-9,]+)\s+ways?\b.*missing node",
    text,
    re.I | re.S,
)
if match:
    missing = int(match.group(1).replace(",", ""))
if missing:
    sys.stderr.write(
        f"osmium check-refs reported {missing} ways with missing node references\n"
    )
    sys.exit(2)
PY
  parse_rc=$?
  set -e
  if [[ "$parse_rc" -ne 0 ]]; then
    fail "osmium check-refs reported missing node references in $(relpath "$file")"
  fi
  if [[ "$rc" -ne 0 ]]; then
    fail "osmium check-refs failed for $(relpath "$file") (exit ${rc})"
  fi
}

backup_current_extract() {
  if [[ ! -f "$EXTRACT_PBF" ]]; then
    return 0
  fi
  mkdir -p "$BACKUP_DIR"
  local stamp name
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -n "$BEFORE_TS" ]]; then
    name="${REGION_ID}-$(compact_ts "$BEFORE_TS").osm.pbf"
  else
    name="${REGION_ID}-${stamp}.osm.pbf"
  fi
  BACKUP_PATH="${BACKUP_DIR}/${name}"
  if [[ -e "$BACKUP_PATH" ]]; then
    BACKUP_PATH="${BACKUP_DIR}/${REGION_ID}-${stamp}.osm.pbf"
  fi
  echo "Backing up previous extract: $(relpath "$BACKUP_PATH")"
  cp -f "$EXTRACT_PBF" "$BACKUP_PATH"
  mapfile -t existing_backups < <(ls -1 "$BACKUP_DIR"/"${REGION_ID}"-*.osm.pbf 2>/dev/null | sort -r)
  if ((${#existing_backups[@]} > MAX_BACKUPS)); then
    for old_backup in "${existing_backups[@]:${MAX_BACKUPS}}"; do
      echo "Removing old backup: $(relpath "$old_backup")"
      rm -f "$old_backup"
    done
  fi
}

promote_derived() {
  mkdir -p "$(dirname "$EXTRACT_PBF")" "$(dirname "$EXTRACT_XML")"
  backup_current_extract
  if ! mv -f "$EXTRACT_TMP" "$EXTRACT_PBF"; then
    fail "Could not move new extract PBF into $(relpath "$EXTRACT_PBF")"
  fi
  CLEANUP_EXTRACT_TMP=0
  if ! mv -f "$XML_TMP" "$EXTRACT_XML"; then
    echo "Restore extract PBF after failed XML promote" >&2
    if [[ -n "$BACKUP_PATH" && -f "$BACKUP_PATH" ]]; then
      mv -f "$BACKUP_PATH" "$EXTRACT_PBF" || true
    fi
    fail "Could not move new XML into $(relpath "$EXTRACT_XML"); previous extract PBF restored if a backup existed"
  fi
  CLEANUP_XML_TMP=0
  write_stamp
}

pbf_header_timestamp() {
  if ! try_load_osm_info "$1" 0; then
    return 1
  fi
  printf '%s' "${INFO_TIMESTAMP:-}"
}

bootstrap_working_source() {
  echo "Bootstrapping working source from $(relpath "$SOURCE_PBF")"
  rm -f "$WORKING_TMP"
  cp -f "$SOURCE_PBF" "$WORKING_TMP"
  if ! osmium fileinfo "$WORKING_TMP" >/dev/null; then
    fail "Bootstrapped working PBF is not readable by osmium"
  fi
  mv -f "$WORKING_TMP" "$WORKING_PBF"
  CLEANUP_WORKING_TMP=0
}

update_working_source_fresh() {
  require_cmd osmupdate
  require_cmd osmconvert
  require_cmd wget
  mkdir -p "$(dirname "$WORKING_PBF")" "$(dirname "$OSMUPDATE_TEMP")"
  rm -f "$WORKING_TMP"

  local need_bootstrap=0
  if [[ "$BOOTSTRAP" -eq 1 ]]; then
    need_bootstrap=1
  elif [[ ! -f "$WORKING_PBF" || ! -s "$WORKING_PBF" ]]; then
    need_bootstrap=1
  else
    local geo_ts work_ts geo_epoch work_epoch
    geo_ts="$(pbf_header_timestamp "$SOURCE_PBF" || true)"
    work_ts="$(pbf_header_timestamp "$WORKING_PBF" || true)"
    if [[ -z "$work_ts" ]]; then
      need_bootstrap=1
    elif [[ -n "$geo_ts" ]]; then
      geo_epoch="$(ts_to_epoch "$geo_ts")"
      work_epoch="$(ts_to_epoch "$work_ts")"
      if (( geo_epoch > work_epoch )); then
        echo "Geofabrik source is newer than the working copy; re-bootstrapping."
        need_bootstrap=1
      fi
    fi
  fi

  if [[ "$need_bootstrap" -eq 1 ]]; then
    bootstrap_working_source
  fi
  CLEANUP_WORKING_TMP=1

  FRESH_START_TS="$(pbf_header_timestamp "$WORKING_PBF" || true)"
  if [[ -z "$FRESH_START_TS" ]]; then
    fail "Working PBF has no timestamp; cannot apply planet replication"
  fi
  echo "Replication start:    ${FRESH_START_TS}"
  fetch_planet_minutely_timestamp || true
  echo "Applying planet.openstreetmap.org replication without clipping"

  local update_log update_rc
  update_log="${TMP_ROOT}/${REGION_ID}-osmupdate.log"
  # Never clip replication diffs: -B reintroduces missing way→node refs.
  # --keep-tempfiles/--trust-tempfiles persist planet diffs under
  # osm-data/.update-tmp/osmupdate/ so a second --fresh run only fetches
  # newly published files.
  local osmupdate_cmd=(
    osmupdate
    "$WORKING_PBF"
    "$WORKING_TMP"
    --keep-tempfiles
    --trust-tempfiles
    -t="$OSMUPDATE_TEMP"
    -v
  )
  local joined="${osmupdate_cmd[*]}"
  if [[ "$joined" == *"-B="* || "$joined" == *"--bounding-polygon"* ]]; then
    fail "Internal error: osmupdate would run with clipping"
  fi

  set +e
  "${osmupdate_cmd[@]}" 2>&1 | tee "$update_log"
  update_rc=${PIPESTATUS[0]}
  set -e

  if grep -qiE 'already up-to-date|already up to date' "$update_log"; then
    echo "Working source is already up-to-date with available planet diffs."
    FRESH_DIFFS="$(count_replication_diffs "$update_log")"
    CLEANUP_WORKING_TMP=1
    rm -f "$WORKING_TMP"
  elif [[ "$update_rc" -ne 0 ]]; then
    fail "osmupdate failed with exit code ${update_rc}"
  else
    if [[ ! -f "$WORKING_TMP" || ! -s "$WORKING_TMP" ]]; then
      fail "osmupdate did not create $(relpath "$WORKING_TMP")"
    fi
    if ! osmium fileinfo "$WORKING_TMP" >/dev/null; then
      fail "osmium fileinfo cannot read updated working PBF"
    fi
    local new_ts
    new_ts="$(pbf_header_timestamp "$WORKING_TMP" || true)"
    if [[ -z "$new_ts" ]]; then
      fail "Updated working PBF has no timestamp"
    fi
    if (( $(ts_to_epoch "$new_ts") < $(ts_to_epoch "$FRESH_START_TS") )); then
      fail "Updated working timestamp ${new_ts} is older than ${FRESH_START_TS}"
    fi
    FRESH_DIFFS="$(count_replication_diffs "$update_log")"
    mv -f "$WORKING_TMP" "$WORKING_PBF"
    CLEANUP_WORKING_TMP=0
    echo "Replication applied:  ${new_ts}"
  fi

  AFTER_TS="$(pbf_header_timestamp "$WORKING_PBF" || true)"
  FRESH_AFTER_TS="$AFTER_TS"
  if [[ -z "$FRESH_DIFFS" ]]; then
    FRESH_DIFFS=0
  fi
  echo "Replication result:   ${FRESH_AFTER_TS:-unknown}"
  echo "Replication diffs:    ${FRESH_DIFFS}"
  echo "Working PBF:          $(relpath "$WORKING_PBF") ($(format_mib "$(file_size "$WORKING_PBF")"))"
  write_working_state
}

write_working_state() {
  if [[ -z "$WORKING_STATE" ]]; then
    return 0
  fi
  cat >"$WORKING_STATE" <<EOF
region=${REGION_ID}
bootstrap_source=$(relpath "$SOURCE_PBF")
working=$(relpath "$WORKING_PBF")
start_timestamp=${FRESH_START_TS}
result_timestamp=${FRESH_AFTER_TS}
planet_timestamp=${PLANET_TS}
diffs=${FRESH_DIFFS}
updated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
}

count_replication_diffs() {
  python3 - "$1" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# osmupdate -v logs e.g. "minutely changefile 7269932: downloading"
# The sequence that only has a timestamp (the file's current seq) is not counted.
seqs = set(
    re.findall(
        r"(?:minutely|hourly|daily)\s+changefile\s+(\d+):\s+downloading\b",
        text,
        re.I,
    )
)
if not seqs:
    seqs = set(
        re.findall(
            r"replication/(?:minute|hour|day)/[0-9/]+\.osc(?:\.gz)?",
            text,
        )
    )
print(len(seqs))
PY
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        usage
        SUMMARY_PRINTED=1
        exit 0
        ;;
      --list)
        LIST_ONLY=1
        ;;
      --show-config)
        SHOW_CONFIG=1
        if [[ $# -lt 2 ]]; then
          echo "ERROR: --show-config requires a region name" >&2
          usage
          SUMMARY_PRINTED=1
          exit 1
        fi
        REGION_ID="$2"
        shift
        ;;
      --force)
        FORCE=1
        ;;
      --fresh)
        FRESH=1
        UPDATE_MODE="fresh"
        ;;
      --bootstrap)
        BOOTSTRAP=1
        ;;
      --)
        shift
        break
        ;;
      -*)
        echo "ERROR: unknown option: $1" >&2
        usage
        SUMMARY_PRINTED=1
        exit 1
        ;;
      *)
        if [[ -n "$REGION_ID" ]]; then
          echo "ERROR: unexpected argument: $1" >&2
          usage
          SUMMARY_PRINTED=1
          exit 1
        fi
        REGION_ID="$1"
        ;;
    esac
    shift
  done
}

parse_args "$@"

if [[ ! -f "$CONF_FILE" ]]; then
  fail "Missing region configuration: $(relpath "$CONF_FILE")"
fi

require_cmd python3

if [[ "$LIST_ONLY" -eq 1 ]]; then
  echo "Configured regions:"
  list_regions
  SUMMARY_PRINTED=1
  exit 0
fi

if [[ -z "$REGION_ID" ]]; then
  echo "ERROR: region name is required." >&2
  echo >&2
  usage
  echo >&2
  echo "Configured regions:" >&2
  list_regions >&2
  SUMMARY_PRINTED=1
  exit 1
fi

load_region "$REGION_ID"
if [[ "$BOOTSTRAP" -eq 1 && "$FRESH" -eq 0 ]]; then
  fail "--bootstrap requires --fresh"
fi
if [[ "$FRESH" -eq 1 ]]; then
  UPDATE_MODE="fresh"
  REPLICATION_NOTE="planet minutely"
fi
SOURCE_META="${SOURCE_PBF}.http"
STAMP_FILE="$(dirname "$EXTRACT_PBF")/.source-stamp"
BACKUP_DIR="$(dirname "$EXTRACT_PBF")/backups"
# osmium infers format from the filename. Temporary PBFs must end in
# .osm.pbf, not .osm.pbf.tmp.
SOURCE_TMP="${SOURCE_PBF%.osm.pbf}.tmp.osm.pbf"
WORKING_PBF="${SOURCE_PBF%.osm.pbf}-working.osm.pbf"
WORKING_TMP="${SOURCE_PBF%.osm.pbf}-working.tmp.osm.pbf"
WORKING_STATE="${SOURCE_PBF%.osm.pbf}-working.state"
OSMUPDATE_TEMP="${TMP_ROOT}/osmupdate/temp"
EXTRACT_INPUT_PBF="$SOURCE_PBF"
EXTRACT_TMP="$(dirname "$EXTRACT_PBF")/current.new.osm.pbf"
XML_TMP="$(dirname "$EXTRACT_XML")/current.new.osm"
CHECKREFS_LOG="${TMP_ROOT}/${REGION_ID}-check-refs.log"
# Leftover from the broken .osm.pbf.tmp naming; never a known-good file.
SOURCE_TMP_LEGACY="${SOURCE_PBF}.tmp"

if [[ "$SHOW_CONFIG" -eq 1 ]]; then
  print_region_config
  SUMMARY_PRINTED=1
  exit 0
fi

require_cmd osmium
require_cmd curl

if [[ ! -f "$BOUNDARY" || ! -s "$BOUNDARY" ]]; then
  fail "Missing or empty boundary file: $(relpath "$BOUNDARY")"
fi
load_boundary_bbox

mkdir -p "$(dirname "$SOURCE_PBF")" "$(dirname "$EXTRACT_PBF")" "$(dirname "$EXTRACT_XML")" "$TMP_ROOT" "$BACKUP_DIR"
rm -f "$EXTRACT_TMP" "$XML_TMP" "$SOURCE_TMP" "$SOURCE_TMP_LEGACY" "$WORKING_TMP"

echo "OSM update directory: ${OSM_DIR}"
print_region_config
echo

if [[ -f "$EXTRACT_PBF" ]]; then
  echo "Reading current extract metadata..."
  if try_load_osm_info "$EXTRACT_PBF" 1; then
    BEFORE_TS="$INFO_TIMESTAMP"
    BEFORE_NODES="$INFO_NODES"
    BEFORE_WAYS="$INFO_WAYS"
    BEFORE_RELS="$INFO_RELS"
    echo "Extract before:       timestamp=${BEFORE_TS:-unknown} nodes=$(format_int "$BEFORE_NODES") ways=$(format_int "$BEFORE_WAYS") relations=$(format_int "$BEFORE_RELS")"
  else
    echo "WARNING: existing extract could not be read: ${INFO_ERROR}" >&2
  fi
fi

echo "Checking Geofabrik source..."
source_started="$(date -u +%s)"
if fetch_remote_headers; then
  if source_is_current; then
    echo "Geofabrik source is already current; download skipped."
    SOURCE_STATUS="UNCHANGED"
    validate_source_pbf "$SOURCE_PBF"
  else
    download_source
    SOURCE_STATUS="UPDATED"
  fi
elif [[ -f "$SOURCE_PBF" && -s "$SOURCE_PBF" ]]; then
  echo "WARNING: Could not fetch Geofabrik HTTP headers; using local source." >&2
  SOURCE_STATUS="UNCHANGED"
  validate_source_pbf "$SOURCE_PBF"
else
  fail "Could not fetch HTTP headers from ${GEOFABRIK_URL}"
fi
SOURCE_SIZE="$(file_size "$SOURCE_PBF")"
load_osm_info "$SOURCE_PBF" 0
AFTER_TS="${INFO_TIMESTAMP:-$REMOTE_LAST_MODIFIED}"
echo "Source PBF:           $(relpath "$SOURCE_PBF") ($(format_mib "$SOURCE_SIZE"))"
if [[ -n "$AFTER_TS" ]]; then
  echo "Source timestamp:     ${AFTER_TS}"
fi

if [[ "$FRESH" -eq 1 ]]; then
  update_working_source_fresh
  EXTRACT_INPUT_PBF="$WORKING_PBF"
  SOURCE_SIZE="$(file_size "$WORKING_PBF")"
fi
SOURCE_RUNTIME=$(( $(date -u +%s) - source_started ))

INPUT_TS="$(pbf_header_timestamp "$EXTRACT_INPUT_PBF" || true)"
if [[ -n "$INPUT_TS" ]]; then
  AFTER_TS="$INPUT_TS"
fi
reject_downgrade "$INPUT_TS" "proposed source ($(relpath "$EXTRACT_INPUT_PBF"), mode=${UPDATE_MODE})"

# Clipped extracts may lack a header timestamp. A newer --fresh working
# copy must still block Geofabrik from promoting older data.
if [[ "$UPDATE_MODE" != "fresh" && -f "$WORKING_PBF" ]]; then
  work_ts="$(pbf_header_timestamp "$WORKING_PBF" || true)"
  if [[ -n "$work_ts" && -n "$INPUT_TS" ]]; then
    work_epoch="$(ts_to_epoch "$work_ts")"
    input_epoch="$(ts_to_epoch "$INPUT_TS")"
    if (( input_epoch < work_epoch )); then
      fail "OSM update rejected because it would replace newer local data with an older source. Fresh working copy timestamp=${work_ts}; proposed Geofabrik source timestamp=${INPUT_TS}."
    fi
  fi
fi

if derived_is_current; then
  if [[ "$UPDATE_MODE" == "fresh" ]]; then
    echo "Regional extract and XML already match the current fresh working source and boundary."
  else
    echo "Regional extract and XML already match the current Geofabrik source and boundary."
  fi
  EXTRACT_STATUS="UNCHANGED"
  XML_STATUS="UNCHANGED"
  load_osm_info "$EXTRACT_PBF" 1
  AFTER_NODES="$INFO_NODES"
  AFTER_WAYS="$INFO_WAYS"
  AFTER_RELS="$INFO_RELS"
  EXTRACT_SIZE="$(file_size "$EXTRACT_PBF")"
  XML_SIZE="$(file_size "$EXTRACT_XML")"
  if [[ -z "$FRESH_AFTER_TS" ]]; then
    AFTER_TS="${INFO_TIMESTAMP:-$AFTER_TS}"
  fi
  STATUS="SUCCESS"
  print_summary
  SUMMARY_PRINTED=1
  exit 0
fi

extract_started="$(date -u +%s)"
echo "Extracting $(relpath "$BOUNDARY") with osmium extract --strategy=${EXTRACT_STRATEGY}"
if ! osmium extract \
    --strategy="$EXTRACT_STRATEGY" \
    --polygon="$BOUNDARY" \
    --set-bounds \
    --overwrite \
    -o "$EXTRACT_TMP" \
    "$EXTRACT_INPUT_PBF"; then
  fail "osmium extract failed"
fi
if [[ ! -f "$EXTRACT_TMP" || ! -s "$EXTRACT_TMP" ]]; then
  fail "osmium extract did not create a non-empty PBF"
fi
if ! osmium fileinfo "$EXTRACT_TMP" >/dev/null; then
  fail "osmium fileinfo cannot read the extracted PBF"
fi

echo "Validating extracted PBF..."
load_osm_info "$EXTRACT_TMP" 1
if [[ -z "$FRESH_AFTER_TS" ]]; then
  AFTER_TS="${INFO_TIMESTAMP:-$AFTER_TS}"
fi
EXTRACT_SIZE="$INFO_SIZE"
AFTER_NODES="$INFO_NODES"
AFTER_WAYS="$INFO_WAYS"
AFTER_RELS="$INFO_RELS"
AFTER_DATA_TS_FIRST="$INFO_DATA_TS_FIRST"
AFTER_DATA_TS_LAST="$INFO_DATA_TS_LAST"

echo "Extract header bbox:  (${INFO_HDR_MIN_LON:-none}, ${INFO_HDR_MIN_LAT:-none}, ${INFO_HDR_MAX_LON:-none}, ${INFO_HDR_MAX_LAT:-none})"
echo "Extract data bbox:    (${INFO_MIN_LON}, ${INFO_MIN_LAT}, ${INFO_MAX_LON}, ${INFO_MAX_LAT})"
echo "Extract objects:      nodes=$(format_int "$AFTER_NODES") ways=$(format_int "$AFTER_WAYS") relations=$(format_int "$AFTER_RELS")"

if [[ -n "$INFO_HDR_MIN_LON" ]]; then
  if ! validate_bbox_inside_window "$INFO_HDR_MIN_LON" "$INFO_HDR_MIN_LAT" "$INFO_HDR_MAX_LON" "$INFO_HDR_MAX_LAT"; then
    fail "Extract header/extraction bbox is outside the safety window (lon ${BBOX_LON_MIN}..${BBOX_LON_MAX}, lat ${BBOX_LAT_MIN}..${BBOX_LAT_MAX}): (${INFO_HDR_MIN_LON}, ${INFO_HDR_MIN_LAT}, ${INFO_HDR_MAX_LON}, ${INFO_HDR_MAX_LAT})"
  fi
  if ! bboxes_overlap "$INFO_HDR_MIN_LON" "$INFO_HDR_MIN_LAT" "$INFO_HDR_MAX_LON" "$INFO_HDR_MAX_LAT" \
      "$BOUNDARY_MIN_LON" "$BOUNDARY_MIN_LAT" "$BOUNDARY_MAX_LON" "$BOUNDARY_MAX_LAT"; then
    fail "Extract header bbox does not overlap the requested boundary"
  fi
fi
validate_data_overlaps_boundary "Extract" "$INFO_MIN_LON" "$INFO_MIN_LAT" "$INFO_MAX_LON" "$INFO_MAX_LAT"
if (( AFTER_NODES < MIN_NODES || AFTER_WAYS < MIN_WAYS || AFTER_RELS < MIN_RELS )); then
  fail "Extract object counts are implausibly low (nodes=$(format_int "$AFTER_NODES") ways=$(format_int "$AFTER_WAYS") relations=$(format_int "$AFTER_RELS"))"
fi
if [[ -f "$EXTRACT_PBF" ]] && (( BEFORE_NODES > 0 )); then
  size_ratio="$(python3 -c 'import sys; print(float(sys.argv[1]) / max(float(sys.argv[2]), 1.0))' "$EXTRACT_SIZE" "$(file_size "$EXTRACT_PBF")")"
  too_big="$(python3 -c 'import sys; print(int(float(sys.argv[1]) > float(sys.argv[2])))' "$size_ratio" "$SIZE_FAIL_FACTOR")"
  warn_big="$(python3 -c 'import sys; print(int(float(sys.argv[1]) > float(sys.argv[2])))' "$size_ratio" "$SIZE_WARN_FACTOR")"
  if [[ "$too_big" -eq 1 ]]; then
    fail "New extract is more than ${SIZE_FAIL_FACTOR}× the previous size"
  fi
  if [[ "$warn_big" -eq 1 ]]; then
    echo "WARNING: New extract grew more than ${SIZE_WARN_FACTOR}×" >&2
  fi
fi

validate_check_refs "$EXTRACT_TMP"
EXTRACT_STATUS="UPDATED"
EXTRACT_RUNTIME=$(( $(date -u +%s) - extract_started ))

xml_started="$(date -u +%s)"
echo "Converting extracted PBF to OSM XML..."
if ! osmium cat "$EXTRACT_TMP" -o "$XML_TMP" --overwrite; then
  fail "osmium cat failed to write $(relpath "$XML_TMP")"
fi
if [[ ! -f "$XML_TMP" || ! -s "$XML_TMP" ]]; then
  fail "New OSM XML file is missing or empty"
fi
if ! osmium fileinfo "$XML_TMP" >/dev/null; then
  fail "osmium fileinfo cannot read the new OSM XML file"
fi
echo "Validating OSM XML..."
load_osm_info "$XML_TMP" 1
XML_TS="$INFO_TIMESTAMP"
XML_SIZE="$INFO_SIZE"
echo "XML data bbox:        (${INFO_MIN_LON}, ${INFO_MIN_LAT}, ${INFO_MAX_LON}, ${INFO_MAX_LAT})"
echo "XML objects:          nodes=$(format_int "$INFO_NODES") ways=$(format_int "$INFO_WAYS") relations=$(format_int "$INFO_RELS")"
validate_data_overlaps_boundary "XML" "$INFO_MIN_LON" "$INFO_MIN_LAT" "$INFO_MAX_LON" "$INFO_MAX_LAT"
if [[ "$INFO_NODES" -ne "$AFTER_NODES" || "$INFO_WAYS" -ne "$AFTER_WAYS" || "$INFO_RELS" -ne "$AFTER_RELS" ]]; then
  fail "XML object counts do not match extracted PBF"
fi
if [[ -n "${AFTER_DATA_TS_LAST:-}" && -n "${INFO_DATA_TS_LAST:-}" && "$INFO_DATA_TS_LAST" != "$AFTER_DATA_TS_LAST" ]]; then
  fail "XML data timestamp last (${INFO_DATA_TS_LAST}) does not match PBF (${AFTER_DATA_TS_LAST})"
fi
if [[ -n "${AFTER_DATA_TS_FIRST:-}" && -n "${INFO_DATA_TS_FIRST:-}" && "$INFO_DATA_TS_FIRST" != "$AFTER_DATA_TS_FIRST" ]]; then
  fail "XML data timestamp first (${INFO_DATA_TS_FIRST}) does not match PBF (${AFTER_DATA_TS_FIRST})"
fi
XML_STATUS="UPDATED"
XML_RUNTIME=$(( $(date -u +%s) - xml_started ))

reject_downgrade "${INFO_TIMESTAMP:-$AFTER_TS}" "candidate extract ($(relpath "$EXTRACT_TMP"), mode=${UPDATE_MODE})"

echo "Promoting validated extract and XML"
promote_derived
STATUS="SUCCESS"
print_summary
SUMMARY_PRINTED=1
exit 0
