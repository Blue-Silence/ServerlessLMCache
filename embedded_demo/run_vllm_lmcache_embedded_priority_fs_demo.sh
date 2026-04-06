#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
DEFAULT_LMCACHE_CONFIG_FILE="${ROOT_DIR}/embedded_demo/configs/default_layerwise_unfull_off.yaml"

MODEL="${1:-Qwen/Qwen3-0.6B}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.5}"
MODEL_SOURCE="${MODEL_SOURCE:-auto}"
PYTHONHASHSEED="${PYTHONHASHSEED:-0}"
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
export LMCACHE_CONFIG_FILE="${LMCACHE_CONFIG_FILE_PATH}"

KV_TRANSFER_CONFIG='{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'

echo "Starting vLLM with model: ${MODEL}"
echo "Resolved model source: ${RESOLVED_MODEL}"
echo "LMCache mode: embedded in the same vLLM process"
echo "LMCache config file: ${LMCACHE_CONFIG_FILE}"
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
