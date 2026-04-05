#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

MODEL="${1:-Qwen/Qwen3-0.6B}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.85}"
LMCACHE_TARGET="${LMCACHE_TARGET:-remote}"

case "${LMCACHE_TARGET}" in
  local)
    LMCACHE_CONFIG_FILE_PATH="${ROOT_DIR}/config/lmcache-gds-local.yaml"
    ;;
  remote)
    LMCACHE_CONFIG_FILE_PATH="${ROOT_DIR}/config/lmcache-gds-remote.yaml"
    ;;
  *)
    echo "Unsupported LMCACHE_TARGET=${LMCACHE_TARGET}. Use 'local' or 'remote'." >&2
    exit 1
    ;;
esac

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Missing virtualenv at ${VENV_DIR}" >&2
  exit 1
fi

mkdir -p "${ROOT_DIR}/.kvcache" "${ROOT_DIR}/.kvcache_remote"

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

export LMCACHE_USE_EXPERIMENTAL=True
export LMCACHE_CONFIG_FILE="${LMCACHE_CONFIG_FILE_PATH}"

KV_TRANSFER_CONFIG='{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'

echo "Starting vLLM with model: ${MODEL}"
echo "LMCache config: ${LMCACHE_CONFIG_FILE}"
echo "LMCache mode: embedded in the same vLLM process"
echo "Serving at: http://${HOST}:${PORT}"

exec vllm serve "${MODEL}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --kv-transfer-config "${KV_TRANSFER_CONFIG}"
