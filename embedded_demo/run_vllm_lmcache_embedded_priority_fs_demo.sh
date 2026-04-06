#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

MODEL="${1:-Qwen/Qwen3-0.6B}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.5}"
CHUNK_SIZE="${CHUNK_SIZE:-256}"
L1_SIZE_GB="${L1_SIZE_GB:-4}"
READ_FIRST_DIR="${READ_FIRST_DIR:-${ROOT_DIR}/.kvcache_remote}"
WRITE_DIR="${WRITE_DIR:-${ROOT_DIR}/.kvcache}"
MODEL_SOURCE="${MODEL_SOURCE:-auto}"
PYTHONHASHSEED="${PYTHONHASHSEED:-0}"
SAVE_DECODE_CACHE="${SAVE_DECODE_CACHE:-1}"
SAVE_UNFULL_CHUNK="${SAVE_UNFULL_CHUNK:-1}"
LAYERWISE="${LAYERWISE:-0}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Missing virtualenv at ${VENV_DIR}" >&2
  exit 1
fi

mkdir -p "${READ_FIRST_DIR}" "${WRITE_DIR}"

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
export PYTHONHASHSEED
export PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

RESOLVED_MODEL="${MODEL}"
if [[ "${MODEL_SOURCE}" != "remote" ]]; then
  RESOLVED_MODEL="$(
    python - "${MODEL}" <<'PY'
import sys
from pathlib import Path

model = sys.argv[1]
if Path(model).exists():
    print(model)
    raise SystemExit

try:
    from huggingface_hub import scan_cache_dir

    info = scan_cache_dir()
    for repo in info.repos:
        if repo.repo_id != model:
            continue
        revisions = list(repo.revisions)
        if revisions:
            print(str(revisions[0].snapshot_path))
            raise SystemExit
except Exception:
    pass

print(model)
PY
  )"
fi

if [[ "${RESOLVED_MODEL}" != "${MODEL}" ]]; then
  export HF_HUB_OFFLINE=1
fi

export LMCACHE_USE_EXPERIMENTAL=True
export LMCACHE_CHUNK_SIZE="${CHUNK_SIZE}"
export LMCACHE_LOCAL_CPU=True
export LMCACHE_MAX_LOCAL_CPU_SIZE="${L1_SIZE_GB}"
export LMCACHE_SAVE_DECODE_CACHE="${SAVE_DECODE_CACHE}"
export LMCACHE_SAVE_UNFULL_CHUNK="${SAVE_UNFULL_CHUNK}"
export LMCACHE_USE_LAYERWISE="${LAYERWISE}"
export LMCACHE_REMOTE_SERDE=naive
export LMCACHE_REMOTE_URL="priority-fs://?read_path=${READ_FIRST_DIR}&write_path=${WRITE_DIR}"
export LMCACHE_REMOTE_STORAGE_PLUGINS=priority_fs
export LMCACHE_EXTRA_CONFIG='{"remote_storage_plugin.priority_fs.module_path":"embedded_demo.priority_fs_adapter","remote_storage_plugin.priority_fs.class_name":"PriorityFSConnectorAdapter"}'

KV_TRANSFER_CONFIG='{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'

echo "Starting vLLM with model: ${MODEL}"
echo "Resolved model source: ${RESOLVED_MODEL}"
echo "LMCache mode: embedded in the same vLLM process"
echo "Priority fs read-first dir (B): ${READ_FIRST_DIR}"
echo "Priority fs write-only dir (A): ${WRITE_DIR}"
echo "Layerwise: ${LAYERWISE}"
echo "Save decode cache: ${SAVE_DECODE_CACHE}"
echo "Save unfull chunk: ${SAVE_UNFULL_CHUNK}"
echo "Serving at: http://${HOST}:${PORT}"
echo "PYTHONHASHSEED: ${PYTHONHASHSEED}"
if [[ "${RESOLVED_MODEL}" != "${MODEL}" ]]; then
  echo "HF_HUB_OFFLINE=1 enabled because a local snapshot was found"
fi

exec vllm serve "${RESOLVED_MODEL}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --disable-hybrid-kv-cache-manager \
  --kv-transfer-config "${KV_TRANSFER_CONFIG}"
