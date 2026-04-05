#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export L2_ADAPTER_KIND="nixl_store"
export NIXL_BACKEND="${NIXL_BACKEND:-GDS}"
export USE_DIRECT_IO="${USE_DIRECT_IO:-true}"

exec "${ROOT_DIR}/scripts/run_lmcache_mp_priority_demo.sh"
