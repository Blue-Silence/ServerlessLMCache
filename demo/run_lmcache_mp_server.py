import sys
import inspect

import demo.write_last_store_policy  # noqa: F401
from lmcache.v1.distributed.config import parse_args_to_config
from lmcache.logging import init_logger
from lmcache.v1.mp_observability.config import parse_args_to_observability_config
from lmcache.v1.multiprocess.config import parse_args_to_mp_server_config
from lmcache.v1.multiprocess.server import MPCacheEngine, parse_args, run_cache_server

logger = init_logger(__name__)


def _install_hash_debug_patch() -> None:
    original_lookup = MPCacheEngine.lookup
    original_store = MPCacheEngine.store

    def lookup_with_debug(self, key, tp_size):
        chunk_hashes = self.token_hasher.compute_chunk_hashes(list(key.token_ids))
        logger.info(
            "HASH_DEBUG lookup request_id=%s tokens=%d chunks=%d first_hashes=%s",
            key.request_id,
            len(key.token_ids),
            len(chunk_hashes),
            [h.hex() for h in chunk_hashes[:3]],
        )
        return original_lookup(self, key, tp_size)

    def store_with_debug(self, key, instance_id, gpu_block_ids, event_ipc_handle):
        session = self.session_manager.get_or_create(key.request_id)
        session.set_tokens(list(key.token_ids))
        chunk_hashes = [
            session.hasher.hash_to_bytes(h).hex()
            for h in session.get_hashes(key.start, key.end)[:3]
        ]
        logger.info(
            "HASH_DEBUG store request_id=%s tokens=%d range=[%d,%d) first_hashes=%s",
            key.request_id,
            len(key.token_ids),
            key.start,
            key.end,
            chunk_hashes,
        )
        return original_store(self, key, instance_id, gpu_block_ids, event_ipc_handle)

    lookup_with_debug.__signature__ = inspect.signature(original_lookup)
    lookup_with_debug.__annotations__ = getattr(original_lookup, "__annotations__", {})
    store_with_debug.__signature__ = inspect.signature(original_store)
    store_with_debug.__annotations__ = getattr(original_store, "__annotations__", {})

    MPCacheEngine.lookup = lookup_with_debug
    MPCacheEngine.store = store_with_debug


def main() -> None:
    _install_hash_debug_patch()
    args = parse_args()
    mp_config = parse_args_to_mp_server_config(args)
    storage_manager_config = parse_args_to_config(args)
    obs_config = parse_args_to_observability_config(args)
    run_cache_server(
        mp_config=mp_config,
        storage_manager_config=storage_manager_config,
        obs_config=obs_config,
    )


if __name__ == "__main__":
    main()
