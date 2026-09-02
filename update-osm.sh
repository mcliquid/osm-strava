#!/usr/bin/env bash
# Incremental OSM update for the Balearic extract using planet minutely diffs.
# MUST always clip with -B=islas-baleares.poly. Never write osmupdate output
# directly onto the current PBF; replace current only after validation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

OSM_DIR="${SCRIPT_DIR}/osm-data"
CURRENT="${OSM_DIR}/islas-baleares-current.osm.pbf"
CURRENT_XML="${OSM_DIR}/islas-baleares-current.osm"
POLY="${OSM_DIR}/islas-baleares.poly"
NEW="${OSM_DIR}/islas-baleares-current.new.osm.pbf"
NEW_XML="${OSM_DIR}/islas-baleares-current.new.osm"
BACKUP_DIR="${OSM_DIR}/backups"
TEMP_PREFIX="${OSM_DIR}/.osmupdate-temp/temp"
MAX_BACKUPS=3

# Data bbox must stay inside this envelope (Balearic Islands, with margin).
BBOX_LON_MIN=-1
BBOX_LON_MAX=7
BBOX_LAT_MIN=36
BBOX_LAT_MAX=43
SIZE_WARN_FACTOR="1.5"
SIZE_FAIL_FACTOR="2.0"

STATUS="FAILED"
PBF_STATUS="FAILED"
XML_STATUS="SKIPPED"
SUMMARY_PRINTED=0
CLEANUP_NEW=1
CLEANUP_NEW_XML=1
RUN_STARTED="$(date -u +%s)"
PBF_RUNTIME=0
XML_RUNTIME=0
TOTAL_RUNTIME=0

BEFORE_TS=""
AFTER_TS=""
BEFORE_SIZE=0
AFTER_SIZE=0
BEFORE_NODES=0
AFTER_NODES=0
BEFORE_WAYS=0
AFTER_WAYS=0
BEFORE_RELS=0
AFTER_RELS=0
BEFORE_DATA_TS_FIRST=""
BEFORE_DATA_TS_LAST=""
AFTER_DATA_TS_FIRST=""
AFTER_DATA_TS_LAST=""
BACKUP_PATH=""
FAIL_REASON=""
XML_SIZE=0
XML_TS=""
XML_KEPT=""

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

print_summary() {
  TOTAL_RUNTIME=$(( $(date -u +%s) - RUN_STARTED ))
  if (( PBF_RUNTIME == 0 )); then
    PBF_RUNTIME="$TOTAL_RUNTIME"
  fi
  echo "------------------------------------------------------------"
  echo "OSM update summary"
  echo "------------------------------------------------------------"
  echo "Status:              ${STATUS}"
  echo "PBF status:          ${PBF_STATUS}"
  echo "XML status:          ${XML_STATUS}"
  if [[ -n "$FAIL_REASON" ]]; then
    echo "Reason:              ${FAIL_REASON}"
  fi
  if [[ "$PBF_STATUS" == "FAILED" ]]; then
    echo "Current OSM file was NOT replaced."
  fi
  if [[ "$STATUS" == "XML_FAILED" ]]; then
    echo "PBF was kept/updated, but XML was not replaced."
    if [[ -f "$CURRENT_XML" ]]; then
      echo "Previous XML kept:   osm-data/islas-baleares-current.osm"
    else
      echo "Previous XML kept:   (none)"
    fi
  fi
  echo "Before:              ${BEFORE_TS:-unknown}"
  echo "After:               ${AFTER_TS:-unknown}"
  if [[ -n "$AFTER_TS" && "$PBF_STATUS" != "FAILED" ]]; then
    local now_epoch after_epoch lag
    now_epoch="$(date -u +%s)"
    after_epoch="$(ts_to_epoch "$AFTER_TS")"
    lag=$((now_epoch - after_epoch))
    echo "Update lag:          $(format_hms "$lag")"
  fi
  if (( BEFORE_SIZE > 0 )); then
    echo "File size before:    $(format_mib "$BEFORE_SIZE")"
  fi
  if (( AFTER_SIZE > 0 )); then
    echo "File size after:     $(format_mib "$AFTER_SIZE")"
  fi
  if (( BEFORE_NODES > 0 || AFTER_NODES > 0 )); then
    echo "Nodes:               $(format_int "$BEFORE_NODES") -> $(format_int "$AFTER_NODES")"
    echo "Ways:                $(format_int "$BEFORE_WAYS") -> $(format_int "$AFTER_WAYS")"
    echo "Relations:           $(format_int "$BEFORE_RELS") -> $(format_int "$AFTER_RELS")"
  fi
  if [[ -n "$BACKUP_PATH" ]]; then
    echo "Backup:              ${BACKUP_PATH#"$SCRIPT_DIR"/}"
  fi
  echo "Current PBF:         osm-data/islas-baleares-current.osm.pbf"
  if [[ "$XML_STATUS" == "SUCCESS" ]]; then
    echo "XML:                 osm-data/islas-baleares-current.osm"
    echo "XML size:            $(format_mib "$XML_SIZE")"
  elif [[ -f "$CURRENT_XML" ]]; then
    echo "XML:                 osm-data/islas-baleares-current.osm"
  fi
  echo "PBF update runtime:  $(format_hms "$PBF_RUNTIME")"
  echo "XML conversion:      $(format_hms "$XML_RUNTIME")"
  echo "Total runtime:       $(format_hms "$TOTAL_RUNTIME")"
  echo "------------------------------------------------------------"
}

fail() {
  FAIL_REASON="$*"
  STATUS="FAILED"
  PBF_STATUS="FAILED"
  XML_STATUS="SKIPPED"
  print_summary
  SUMMARY_PRINTED=1
  echo "ERROR: $FAIL_REASON" >&2
  exit 1
}

xml_fail() {
  FAIL_REASON="$*"
  STATUS="XML_FAILED"
  XML_STATUS="FAILED"
  if [[ -f "$CURRENT_XML" ]]; then
    XML_KEPT="osm-data/islas-baleares-current.osm"
  fi
  print_summary
  SUMMARY_PRINTED=1
  echo "ERROR: PBF is current, but XML conversion failed: $FAIL_REASON" >&2
  exit 1
}

on_exit() {
  local rc=$?
  if [[ "$CLEANUP_NEW" -eq 1 && -f "$NEW" ]]; then
    rm -f "$NEW"
  fi
  if [[ "$CLEANUP_NEW_XML" -eq 1 && -f "$NEW_XML" ]]; then
    rm -f "$NEW_XML"
  fi
  if [[ "$SUMMARY_PRINTED" -eq 0 ]]; then
    if [[ "$PBF_STATUS" == "UPDATED" || "$PBF_STATUS" == "UNCHANGED" ]]; then
      STATUS="XML_FAILED"
      XML_STATUS="FAILED"
      if [[ -z "$FAIL_REASON" ]]; then
        FAIL_REASON="XML conversion aborted (exit ${rc})"
      fi
    else
      STATUS="FAILED"
      PBF_STATUS="FAILED"
      if [[ -z "$FAIL_REASON" ]]; then
        FAIL_REASON="Update aborted (exit ${rc})"
      fi
    fi
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

INFO_ERROR=""

try_load_osm_info() {
  local file="$1"
  local json_file parsed
  INFO_ERROR=""
  json_file="$(mktemp)"
  if ! osmium fileinfo -e -j "$file" >"$json_file"; then
    rm -f "$json_file"
    INFO_ERROR="osmium fileinfo -e failed for ${file}"
    return 1
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
if not isinstance(bbox, list) or len(bbox) != 4:
    sys.stderr.write("data bounding box missing or invalid\n")
    sys.exit(2)

def emit(name, value):
    print(f"{name}={value}")

emit("INFO_TIMESTAMP", timestamp)
emit("INFO_SIZE", size)
emit("INFO_MIN_LON", bbox[0])
emit("INFO_MIN_LAT", bbox[1])
emit("INFO_MAX_LON", bbox[2])
emit("INFO_MAX_LAT", bbox[3])
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
  if ! try_load_osm_info "$1"; then
    fail "${INFO_ERROR}"
  fi
}

validate_bbox() {
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

mark_pbf_runtime() {
  PBF_RUNTIME=$(( $(date -u +%s) - RUN_STARTED ))
}

convert_current_pbf_to_xml() {
  local xml_started pbf_epoch xml_epoch
  xml_started="$(date -u +%s)"
  CLEANUP_NEW_XML=1

  if [[ -f "$NEW_XML" ]]; then
    echo "Removing leftover temporary file: osm-data/islas-baleares-current.new.osm"
    rm -f "$NEW_XML"
  fi

  echo "Converting current PBF to OSM XML..."
  if ! osmium cat "$CURRENT" -o "$NEW_XML" --overwrite; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "osmium cat failed to write osm-data/islas-baleares-current.new.osm"
  fi

  if [[ ! -f "$NEW_XML" ]]; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "osmium cat did not create osm-data/islas-baleares-current.new.osm"
  fi
  if [[ ! -s "$NEW_XML" ]]; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "New OSM XML file is empty"
  fi

  if ! osmium fileinfo "$NEW_XML" >/dev/null; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "osmium fileinfo cannot read the new OSM XML file"
  fi

  echo "Validating new OSM XML..."
  if ! try_load_osm_info "$NEW_XML"; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "${INFO_ERROR}"
  fi

  XML_TS="$INFO_TIMESTAMP"
  XML_SIZE="$INFO_SIZE"
  echo "XML provenance:       PBF replication timestamp ${AFTER_TS}"
  echo "XML header timestamp: ${XML_TS:-not present in OSM XML}"
  echo "XML data timestamps:  first=${INFO_DATA_TS_FIRST:-unknown} last=${INFO_DATA_TS_LAST:-unknown}"
  echo "XML data bbox:        (${INFO_MIN_LON}, ${INFO_MIN_LAT}, ${INFO_MAX_LON}, ${INFO_MAX_LAT})"
  echo "XML objects:          nodes=$(format_int "$INFO_NODES") ways=$(format_int "$INFO_WAYS") relations=$(format_int "$INFO_RELS")"

  if [[ -n "$XML_TS" ]]; then
    pbf_epoch="$(ts_to_epoch "$AFTER_TS")"
    xml_epoch="$(ts_to_epoch "$XML_TS")"
    if (( xml_epoch < pbf_epoch )); then
      XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
      xml_fail "XML timestamp ${XML_TS} is older than PBF timestamp ${AFTER_TS}"
    fi
  fi
  if [[ -n "${AFTER_DATA_TS_LAST:-}" && -n "${INFO_DATA_TS_LAST:-}" && "$INFO_DATA_TS_LAST" != "$AFTER_DATA_TS_LAST" ]]; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "XML data timestamp last (${INFO_DATA_TS_LAST}) does not match PBF (${AFTER_DATA_TS_LAST})"
  fi
  if [[ -n "${AFTER_DATA_TS_FIRST:-}" && -n "${INFO_DATA_TS_FIRST:-}" && "$INFO_DATA_TS_FIRST" != "$AFTER_DATA_TS_FIRST" ]]; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "XML data timestamp first (${INFO_DATA_TS_FIRST}) does not match PBF (${AFTER_DATA_TS_FIRST})"
  fi

  if ! validate_bbox "$INFO_MIN_LON" "$INFO_MIN_LAT" "$INFO_MAX_LON" "$INFO_MAX_LAT"; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "XML data bounding box looks global or outside the Balearic safety window (lon ${BBOX_LON_MIN}..${BBOX_LON_MAX}, lat ${BBOX_LAT_MIN}..${BBOX_LAT_MAX}): (${INFO_MIN_LON}, ${INFO_MIN_LAT}, ${INFO_MAX_LON}, ${INFO_MAX_LAT})"
  fi

  if [[ "$INFO_NODES" -ne "$AFTER_NODES" || "$INFO_WAYS" -ne "$AFTER_WAYS" || "$INFO_RELS" -ne "$AFTER_RELS" ]]; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "XML object counts do not match PBF (PBF nodes/ways/relations $(format_int "$AFTER_NODES")/$(format_int "$AFTER_WAYS")/$(format_int "$AFTER_RELS") vs XML $(format_int "$INFO_NODES")/$(format_int "$INFO_WAYS")/$(format_int "$INFO_RELS"))"
  fi

  echo "Replacing current XML"
  if ! mv -f "$NEW_XML" "$CURRENT_XML"; then
    XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
    xml_fail "Could not move new XML into osm-data/islas-baleares-current.osm"
  fi
  CLEANUP_NEW_XML=0
  XML_STATUS="SUCCESS"
  XML_RUNTIME=$(( $(date -u +%s) - xml_started ))
}

finish_after_pbf_success() {
  mark_pbf_runtime
  convert_current_pbf_to_xml
  STATUS="SUCCESS"
  print_summary
  SUMMARY_PRINTED=1
  exit 0
}

echo "OSM update directory: ${OSM_DIR}"

require_cmd osmupdate
require_cmd osmium
require_cmd python3
require_cmd wget

if [[ ! -f "$CURRENT" ]]; then
  fail "Missing current OSM file: osm-data/islas-baleares-current.osm.pbf"
fi
if [[ ! -s "$CURRENT" ]]; then
  fail "Current OSM file is empty: osm-data/islas-baleares-current.osm.pbf"
fi
if [[ ! -f "$POLY" ]]; then
  fail "Missing clip polygon: osm-data/islas-baleares.poly"
fi
if [[ ! -s "$POLY" ]]; then
  fail "Clip polygon is empty: osm-data/islas-baleares.poly"
fi

if [[ -f "$NEW" ]]; then
  echo "Removing leftover temporary file: osm-data/islas-baleares-current.new.osm.pbf"
  rm -f "$NEW"
fi
if [[ -f "$NEW_XML" ]]; then
  echo "Removing leftover temporary file: osm-data/islas-baleares-current.new.osm"
  rm -f "$NEW_XML"
fi

mkdir -p "$BACKUP_DIR"
mkdir -p "$(dirname "$TEMP_PREFIX")"

echo "Reading current OSM metadata..."
load_osm_info "$CURRENT"
BEFORE_TS="$INFO_TIMESTAMP"
BEFORE_SIZE="$INFO_SIZE"
BEFORE_NODES="$INFO_NODES"
BEFORE_WAYS="$INFO_WAYS"
BEFORE_RELS="$INFO_RELS"
BEFORE_MIN_LON="$INFO_MIN_LON"
BEFORE_MIN_LAT="$INFO_MIN_LAT"
BEFORE_MAX_LON="$INFO_MAX_LON"
BEFORE_MAX_LAT="$INFO_MAX_LAT"
BEFORE_DATA_TS_FIRST="$INFO_DATA_TS_FIRST"
BEFORE_DATA_TS_LAST="$INFO_DATA_TS_LAST"

if [[ -z "$BEFORE_TS" ]]; then
  fail "Could not read osmosis_replication_timestamp from current PBF"
fi

echo "OSM timestamp before: ${BEFORE_TS}"
echo "Data bbox before:     (${BEFORE_MIN_LON}, ${BEFORE_MIN_LAT}, ${BEFORE_MAX_LON}, ${BEFORE_MAX_LAT})"
echo "Objects before:       nodes=$(format_int "$BEFORE_NODES") ways=$(format_int "$BEFORE_WAYS") relations=$(format_int "$BEFORE_RELS")"

B_OPTION="-B=${POLY}"
if [[ "$B_OPTION" != -B=* || "$B_OPTION" == "-B=" ]]; then
  fail "Internal error: clip polygon argument -B is missing"
fi

OSMUPDATE_CMD=(
  osmupdate
  "$CURRENT"
  "$NEW"
  --minute
  "$B_OPTION"
  -t="$TEMP_PREFIX"
  -v
)

joined="${OSMUPDATE_CMD[*]}"
if [[ "$joined" != *"-B="* ]]; then
  fail "Internal error: osmupdate would run without -B"
fi
if [[ "$joined" != *"$POLY"* ]]; then
  fail "Internal error: osmupdate clip polygon path is missing"
fi

echo "Running osmupdate with regional clip ${B_OPTION}"
mkdir -p "$(dirname "$TEMP_PREFIX")"
update_log="${OSM_DIR}/.osmupdate-temp/osmupdate.log"
set +e
"${OSMUPDATE_CMD[@]}" 2>&1 | tee "$update_log"
update_rc=${PIPESTATUS[0]}
set -e

if [[ "$update_rc" -ne 0 ]]; then
  if [[ ! -f "$NEW" ]] && grep -qiE 'already up-to-date|already up to date' "$update_log"; then
    echo "osmupdate: OSM file is already up-to-date."
    PBF_STATUS="UNCHANGED"
    AFTER_TS="$BEFORE_TS"
    AFTER_SIZE="$BEFORE_SIZE"
    AFTER_NODES="$BEFORE_NODES"
    AFTER_WAYS="$BEFORE_WAYS"
    AFTER_RELS="$BEFORE_RELS"
    AFTER_MIN_LON="$BEFORE_MIN_LON"
    AFTER_MIN_LAT="$BEFORE_MIN_LAT"
    AFTER_MAX_LON="$BEFORE_MAX_LON"
    AFTER_MAX_LAT="$BEFORE_MAX_LAT"
    AFTER_DATA_TS_FIRST="$BEFORE_DATA_TS_FIRST"
    AFTER_DATA_TS_LAST="$BEFORE_DATA_TS_LAST"
    FAIL_REASON=""
    echo "OSM timestamp after:  ${AFTER_TS}"
    echo "No newer minutely diffs applied; current PBF left unchanged."
    finish_after_pbf_success
  fi
  fail "osmupdate failed with exit code ${update_rc}"
fi

# A. File exists and is not empty
if [[ ! -f "$NEW" ]]; then
  fail "osmupdate did not create osm-data/islas-baleares-current.new.osm.pbf"
fi
if [[ ! -s "$NEW" ]]; then
  fail "New OSM file is empty"
fi

# B. osmium fileinfo -e works
echo "Validating new OSM file..."
load_osm_info "$NEW"
AFTER_TS="$INFO_TIMESTAMP"
AFTER_SIZE="$INFO_SIZE"
AFTER_NODES="$INFO_NODES"
AFTER_WAYS="$INFO_WAYS"
AFTER_RELS="$INFO_RELS"
AFTER_MIN_LON="$INFO_MIN_LON"
AFTER_MIN_LAT="$INFO_MIN_LAT"
AFTER_MAX_LON="$INFO_MAX_LON"
AFTER_MAX_LAT="$INFO_MAX_LAT"
AFTER_DATA_TS_FIRST="$INFO_DATA_TS_FIRST"
AFTER_DATA_TS_LAST="$INFO_DATA_TS_LAST"

echo "OSM timestamp before: ${BEFORE_TS}"
echo "OSM timestamp after:  ${AFTER_TS}"

# C. Replication timestamp must not be older
if [[ -z "$AFTER_TS" ]]; then
  fail "Could not read osmosis_replication_timestamp from new PBF"
fi
before_epoch="$(ts_to_epoch "$BEFORE_TS")"
after_epoch="$(ts_to_epoch "$AFTER_TS")"
if (( after_epoch < before_epoch )); then
  fail "New replication timestamp ${AFTER_TS} is older than ${BEFORE_TS}"
fi

# D. Plausible file size
if (( AFTER_SIZE < 1024 )); then
  fail "New OSM file is implausibly small (${AFTER_SIZE} bytes)"
fi
size_ratio="$(python3 -c 'import sys; print(float(sys.argv[1]) / float(sys.argv[2]))' "$AFTER_SIZE" "$BEFORE_SIZE")"
too_big="$(python3 -c 'import sys; print(int(float(sys.argv[1]) > float(sys.argv[2])))' "$size_ratio" "$SIZE_FAIL_FACTOR")"
warn_big="$(python3 -c 'import sys; print(int(float(sys.argv[1]) > float(sys.argv[2])))' "$size_ratio" "$SIZE_WARN_FACTOR")"
if [[ "$too_big" -eq 1 ]]; then
  fail "New file is more than ${SIZE_FAIL_FACTOR}× the previous size ($(format_mib "$BEFORE_SIZE") -> $(format_mib "$AFTER_SIZE"))"
fi
if [[ "$warn_big" -eq 1 ]]; then
  echo "WARNING: New file grew more than ${SIZE_WARN_FACTOR}× ($(format_mib "$BEFORE_SIZE") -> $(format_mib "$AFTER_SIZE"))" >&2
fi

# E. Data bounding box must remain regional
echo "Data bbox after:      (${AFTER_MIN_LON}, ${AFTER_MIN_LAT}, ${AFTER_MAX_LON}, ${AFTER_MAX_LAT})"
if ! validate_bbox "$AFTER_MIN_LON" "$AFTER_MIN_LAT" "$AFTER_MAX_LON" "$AFTER_MAX_LAT"; then
  fail "Data bounding box looks global or outside the Balearic safety window (lon ${BBOX_LON_MIN}..${BBOX_LON_MAX}, lat ${BBOX_LAT_MIN}..${BBOX_LAT_MAX}): (${AFTER_MIN_LON}, ${AFTER_MIN_LAT}, ${AFTER_MAX_LON}, ${AFTER_MAX_LAT})"
fi

# F. Object counts (diagnostic only)
echo "Objects after:        nodes=$(format_int "$AFTER_NODES") ways=$(format_int "$AFTER_WAYS") relations=$(format_int "$AFTER_RELS")"

# Atomic replace: current -> backup, then .new -> current
backup_name="islas-baleares-$(compact_ts "$BEFORE_TS").osm.pbf"
BACKUP_PATH="${BACKUP_DIR}/${backup_name}"
if [[ -e "$BACKUP_PATH" ]]; then
  BACKUP_PATH="${BACKUP_DIR}/islas-baleares-$(compact_ts "$BEFORE_TS")-$(date -u +%H%M%S).osm.pbf"
fi

echo "Replacing current PBF (backup: ${BACKUP_PATH#"$SCRIPT_DIR"/})"
if ! mv "$CURRENT" "$BACKUP_PATH"; then
  fail "Could not move current PBF to backup"
fi
if ! mv "$NEW" "$CURRENT"; then
  echo "Restore current PBF from backup after failed replace" >&2
  mv "$BACKUP_PATH" "$CURRENT" || true
  fail "Could not move new PBF into place; previous current file restored"
fi
CLEANUP_NEW=0
PBF_STATUS="UPDATED"

# Keep only the newest backups
mapfile -t existing_backups < <(ls -1 "$BACKUP_DIR"/islas-baleares-*.osm.pbf 2>/dev/null | sort -r)
if ((${#existing_backups[@]} > MAX_BACKUPS)); then
  for old_backup in "${existing_backups[@]:${MAX_BACKUPS}}"; do
    echo "Removing old backup: ${old_backup#"$SCRIPT_DIR"/}"
    rm -f "$old_backup"
  done
fi

finish_after_pbf_success
