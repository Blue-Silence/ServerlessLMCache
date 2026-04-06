"""Helpers for predicting embedded LMCache fs filenames.

This mirrors the embedded priority-fs path under ``embedded_demo/`` and is
intentionally separate from ``demo/prompt_cache_files.py``.

Schema used by embedded LMCache files:

    <model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>.data

This is the filename schema produced by LMCache V1's ``FSConnector`` via
``CacheEngineKey.to_string()``. It is different from the LMCache MP fs schema:

    <model_name>@<kv_rank_hex>@<chunk_hash_hex>.data
"""

import torch

from lmcache.v1.config import LMCacheEngineConfig
from lmcache.v1.metadata import LMCacheMetadata
from lmcache.v1.token_database import ChunkedTokenDatabase


_KV_DTYPE_MAP = {
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
    "float32": torch.float32,
}


def _resolve_kv_dtype(kv_dtype: str) -> torch.dtype:
    try:
        return _KV_DTYPE_MAP[kv_dtype]
    except KeyError as exc:
        supported = ", ".join(sorted(_KV_DTYPE_MAP))
        raise ValueError(
            f"Unsupported kv_dtype={kv_dtype!r}. Supported values: {supported}."
        ) from exc


def compute_cache_filenames(
    tokenizer,
    prompt: str,
    model_name: str,
    world_size: int = 1,
    worker_id: int = 0,
    kv_dtype: str = "bfloat16",
    chunk_size: int = 256,
    hash_algorithm: str = "builtin",
    use_layerwise: bool = False,
    num_layers: int | None = None,
    save_unfull_chunk: bool = True,
) -> tuple[list[int], list[int], list[str]]:
    """Compute embedded LMCache chunk hashes and filenames for a prompt.

    Returns token ids plus the filenames that embedded priority-fs writes into
    ``.kvcache`` / ``.kvcache_remote``.

    Filename schema:

        <model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>.data

    Layerwise schema:

        <model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>@<layer_id>.data

    Notes:
    - ``world_size`` / ``worker_id`` / ``dtype`` are part of the embedded key.
    - ``chunk_hash`` is the LMCache V1 integer prefix hash rendered with
      ``f"{chunk_hash:x}"``, so negative-looking values are possible.
    - This helper is intentionally not shared with the MP demo because the MP
      path uses a different object-key schema with ``kv_rank`` instead.
    - When ``use_layerwise=True``, ``num_layers`` must be provided and the
      returned ``chunk_hashes`` list is expanded to align 1:1 with filenames.
    - ``save_unfull_chunk`` defaults to True here to match the current embedded
      demo default, so a final partial chunk is also included.
    """
    if use_layerwise and (num_layers is None or num_layers <= 0):
        raise ValueError("num_layers must be a positive integer when use_layerwise=True")

    token_ids = tokenizer(prompt, add_special_tokens=True).input_ids
    config = LMCacheEngineConfig(
        chunk_size=chunk_size,
        pre_caching_hash_algorithm=hash_algorithm,
        use_layerwise=use_layerwise,
        save_unfull_chunk=save_unfull_chunk,
    )
    metadata = LMCacheMetadata(
        model_name=model_name,
        world_size=world_size,
        local_world_size=world_size,
        worker_id=worker_id,
        local_worker_id=worker_id,
        kv_dtype=_resolve_kv_dtype(kv_dtype),
        kv_shape=(1, 2, chunk_size, 1, 1),
        chunk_size=chunk_size,
    )
    token_db = ChunkedTokenDatabase(config=config, metadata=metadata)

    chunk_hashes = []
    filenames = []
    for _, _, key in token_db.process_tokens(tokens=token_ids, make_key=True):
        if use_layerwise:
            for layer_key in key.split_layers(num_layers):
                chunk_hashes.append(layer_key.chunk_hash)
                filenames.append(layer_key.to_string().replace("/", "-SEP-") + ".data")
        else:
            chunk_hashes.append(key.chunk_hash)
            filenames.append(key.to_string().replace("/", "-SEP-") + ".data")

    return token_ids, chunk_hashes, filenames
