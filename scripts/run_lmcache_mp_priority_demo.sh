#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-5555}"
L1_SIZE_GB="${L1_SIZE_GB:-4}"
CHUNK_SIZE="${CHUNK_SIZE:-256}"
EVICTION_POLICY="${EVICTION_POLICY:-LRU}"
READ_FIRST_DIR="${READ_FIRST_DIR:-${ROOT_DIR}/.kvcache_remote}"
WRITE_DIR="${WRITE_DIR:-${ROOT_DIR}/.kvcache}"
L2_ADAPTER_KIND="${L2_ADAPTER_KIND:-fs}"
NIXL_BACKEND="${NIXL_BACKEND:-GDS}"
NIXL_POOL_SIZE="${NIXL_POOL_SIZE:-8}"
USE_DIRECT_IO="${USE_DIRECT_IO:-true}"
PYTHONHASHSEED="${PYTHONHASHSEED:-0}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Missing virtualenv at ${VENV_DIR}" >&2
  exit 1
fi

mkdir -p "${READ_FIRST_DIR}" "${WRITE_DIR}"

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
export PYTHONHASHSEED

case "${L2_ADAPTER_KIND}" in
  fs)
    READ_ADAPTER_JSON="{\"type\":\"fs\",\"base_path\":\"${READ_FIRST_DIR}\"}"
    WRITE_ADAPTER_JSON="{\"type\":\"fs\",\"base_path\":\"${WRITE_DIR}\"}"
    ;;
  nixl_store)
    READ_ADAPTER_JSON="{\"type\":\"nixl_store\",\"backend\":\"${NIXL_BACKEND}\",\"backend_params\":{\"file_path\":\"${READ_FIRST_DIR}\",\"use_direct_io\":\"${USE_DIRECT_IO}\"},\"pool_size\":${NIXL_POOL_SIZE}}"
    WRITE_ADAPTER_JSON="{\"type\":\"nixl_store\",\"backend\":\"${NIXL_BACKEND}\",\"backend_params\":{\"file_path\":\"${WRITE_DIR}\",\"use_direct_io\":\"${USE_DIRECT_IO}\"},\"pool_size\":${NIXL_POOL_SIZE}}"
    ;;
  *)
    echo "Unsupported L2_ADAPTER_KIND=${L2_ADAPTER_KIND}. Use 'fs' or 'nixl_store'." >&2
    exit 1
    ;;
esac

echo "Starting LMCache MP server"
echo "Read-first dir (B): ${READ_FIRST_DIR}"
echo "Write-only dir (A): ${WRITE_DIR}"
echo "Adapter order: B first, A second"
echo "Store policy: write_last"
echo "Prefetch policy: default"
echo "PYTHONHASHSEED: ${PYTHONHASHSEED}"

exec python -m demo.run_lmcache_mp_server \
  --host "${SERVER_HOST}" \
  --port "${SERVER_PORT}" \
  --chunk-size "${CHUNK_SIZE}" \
  --l1-size-gb "${L1_SIZE_GB}" \
  --eviction-policy "${EVICTION_POLICY}" \
  --disable-observability \
  --l2-store-policy write_last \
  --l2-prefetch-policy default \
  --l2-adapter "${READ_ADAPTER_JSON}" \
  --l2-adapter "${WRITE_ADAPTER_JSON}"
