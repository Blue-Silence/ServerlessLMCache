from pathlib import Path

from lmcache.v1.multiprocess.token_hasher import TokenHasher


def compute_cache_filenames(
    tokenizer,
    prompt: str,
    model_name: str,
    kv_rank: str,
    chunk_size: int = 256,
    hash_algorithm: str = "blake3",
) -> tuple[list[int], list[bytes], list[str]]:
    token_ids = tokenizer(prompt, add_special_tokens=True).input_ids
    hasher = TokenHasher(chunk_size=chunk_size, hash_algorithm=hash_algorithm)
    chunk_hashes = hasher.compute_chunk_hashes(list(token_ids))

    safe_model = model_name.replace("/", "-SEP-")
    filenames = [
        f"{safe_model}@{kv_rank}@{chunk_hash.hex()}.data"
        for chunk_hash in chunk_hashes
    ]
    return token_ids, chunk_hashes, filenames
