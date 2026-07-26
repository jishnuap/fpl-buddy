#!/usr/bin/env bash
#
# One prediction run: update the database, fit the model, write the artefact.
#
# Runs in the sidecar image only. fpl-buddy never executes this and never
# imports airsenal -- the JSON file this produces is the whole interface.
# See docs/airsenal.md.

set -euo pipefail

: "${AIRSENAL_HOME:=/data/airsenal/db}"
: "${AIRSENAL_SNAPSHOT_PATH:=/data/airsenal/predictions.json}"
: "${AIRSENAL_WEEKS_AHEAD:=5}"
: "${AIRSENAL_N_PREVIOUS_SEASONS:=3}"
: "${AIRSENAL_EMIT_TRANSFER_PLAN:=false}"
: "${FPL_TEAM_ID:=}"

export AIRSENAL_HOME

# ---------------------------------------------------------------- the invariant
#
# AIrsenal will POST to the FPL API if you give it credentials --
# airsenal_make_transfers and airsenal_set_lineup both exist. Exactly one code
# path in this project is allowed to write to FPL, and it is
# decisions/executor.py, after re-validating against a freshly built context.
#
# Refusing to start is the enforceable version of that rule. Everything the
# predictions need is public.
if [[ -n "${FPL_LOGIN:-}" || -n "${FPL_PASSWORD:-}" ]]; then
  echo "FATAL: FPL_LOGIN/FPL_PASSWORD are set in the AIrsenal sidecar." >&2
  echo "This container must never be able to write to FPL. Unset them." >&2
  exit 78  # EX_CONFIG
fi

if [[ -z "${FPL_TEAM_ID}" ]]; then
  echo "WARNING: FPL_TEAM_ID is unset. Predictions will still be produced;" >&2
  echo "the transfer plan (if enabled) will be skipped." >&2
fi

mkdir -p "${AIRSENAL_HOME}" "$(dirname "${AIRSENAL_SNAPSHOT_PATH}")"

# ------------------------------------------------------------------- the run
#
# The initial build pulls several seasons of history and is slow. It happens
# once, onto the volume; every later run is an incremental update. If the volume
# is ephemeral this becomes a multi-hour job every night, which is the loudest
# possible way to find out the mount is wrong.
if [[ ! -f "${AIRSENAL_HOME}/AIRSENAL_DB_FILE" && -z "${AIRSENAL_DB_URI:-}" ]]; then
  echo "== No AIrsenal database yet; building it (this takes a while) =="
  airsenal_setup_initial_db --n_previous "${AIRSENAL_N_PREVIOUS_SEASONS}" \
    ${FPL_TEAM_ID:+--fpl_team_id "${FPL_TEAM_ID}"}
fi

echo "== Updating the database =="
airsenal_update_db ${FPL_TEAM_ID:+--fpl_team_id "${FPL_TEAM_ID}"}

echo "== Running predictions (${AIRSENAL_WEEKS_AHEAD} weeks ahead) =="
airsenal_run_prediction --weeks_ahead "${AIRSENAL_WEEKS_AHEAD}"

if [[ "${AIRSENAL_EMIT_TRANSFER_PLAN,,}" == "true" && -n "${FPL_TEAM_ID}" ]]; then
  echo "== Running the squad optimiser =="
  # Against a squad reconstructed from the public API, i.e. your last published
  # picks. The artefact stamps that provenance; see docs/airsenal.md.
  airsenal_run_optimization --weeks_ahead "${AIRSENAL_WEEKS_AHEAD}" \
    --fpl_team_id "${FPL_TEAM_ID}"
fi

echo "== Writing the artefact =="
python3 /sidecar/dump_predictions.py \
  --out "${AIRSENAL_SNAPSHOT_PATH}" \
  --weeks-ahead "${AIRSENAL_WEEKS_AHEAD}"

echo "== Done =="
