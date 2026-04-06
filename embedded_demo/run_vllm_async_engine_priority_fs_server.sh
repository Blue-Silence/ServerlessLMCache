#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

PYTHONHASHSEED="${PYTHONHASHSEED:-0}"
PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
LAYERWISE="${LAYERWISE:-0}"
SAVE_DECODE_CACHE="${SAVE_DECODE_CACHE:-1}"
SAVE_UNFULL_CHUNK="${SAVE_UNFULL_CHUNK:-1}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Missing virtualenv at ${VENV_DIR}" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
export PYTHONHASHSEED
export PYTHONPATH

EXTRA_ARGS=()
if [[ "${LAYERWISE}" == "1" || "${LAYERWISE}" == "true" || "${LAYERWISE}" == "True" ]]; then
  EXTRA_ARGS+=("--layerwise")
fi
if [[ "${SAVE_DECODE_CACHE}" == "1" || "${SAVE_DECODE_CACHE}" == "true" || "${SAVE_DECODE_CACHE}" == "True" ]]; then
  EXTRA_ARGS+=("--save-decode-cache")
else
  EXTRA_ARGS+=("--no-save-decode-cache")
fi
if [[ "${SAVE_UNFULL_CHUNK}" == "1" || "${SAVE_UNFULL_CHUNK}" == "true" || "${SAVE_UNFULL_CHUNK}" == "True" ]]; then
  EXTRA_ARGS+=("--save-unfull-chunk")
else
  EXTRA_ARGS+=("--no-save-unfull-chunk")
fi

exec python embedded_demo/run_vllm_async_engine_priority_fs_server.py "${EXTRA_ARGS[@]}" "$@"
