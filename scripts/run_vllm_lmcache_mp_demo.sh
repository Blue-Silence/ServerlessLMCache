#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

MODEL="${1:-Qwen/Qwen3-0.6B}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-5555}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.5}"
MODEL_SOURCE="${MODEL_SOURCE:-auto}"
PYTHONHASHSEED="${PYTHONHASHSEED:-0}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Missing virtualenv at ${VENV_DIR}" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
export PYTHONHASHSEED

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

KV_TRANSFER_CONFIG="{\"kv_connector\":\"LMCacheMPConnector\",\"kv_role\":\"kv_both\",\"kv_connector_extra_config\":{\"lmcache.mp.host\":\"tcp://${SERVER_HOST}\",\"lmcache.mp.port\":${SERVER_PORT}}}"

echo "Starting vLLM with model: ${MODEL}"
echo "Resolved model source: ${RESOLVED_MODEL}"
echo "LMCache MP server: tcp://${SERVER_HOST}:${SERVER_PORT}"
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
