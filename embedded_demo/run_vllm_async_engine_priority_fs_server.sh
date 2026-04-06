#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
DEFAULT_LMCACHE_CONFIG_FILE="${ROOT_DIR}/embedded_demo/configs/default_layerwise_unfull_off.yaml"

PYTHONHASHSEED="${PYTHONHASHSEED:-0}"
PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
LMCACHE_CONFIG_FILE_PATH="${LMCACHE_CONFIG_FILE_PATH:-${DEFAULT_LMCACHE_CONFIG_FILE}}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Missing virtualenv at ${VENV_DIR}" >&2
  exit 1
fi

if [[ ! -f "${LMCACHE_CONFIG_FILE_PATH}" ]]; then
  echo "Missing LMCache config file at ${LMCACHE_CONFIG_FILE_PATH}" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
export PYTHONHASHSEED
export PYTHONPATH
export LMCACHE_USE_EXPERIMENTAL=True
export LMCACHE_CONFIG_FILE="${LMCACHE_CONFIG_FILE_PATH}"

exec python embedded_demo/run_vllm_async_engine_priority_fs_server.py "$@"
