"""CLI for listing embedded LMCache filenames for a prompt.

This follows the same split as the original ``demo/`` helpers:
- ``prompt_cache_files.py`` computes filenames
- this script prints them and checks whether they exist on disk

Embedded filename schema:

    <model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>.data

Layerwise embedded filename schema:

    <model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>@<layer_id>.data
"""

import argparse
from pathlib import Path
import sys

from transformers import AutoConfig
from transformers import AutoTokenizer

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from embedded_demo.cache_files.prompt_cache_files import compute_cache_filenames
from embedded_demo.request_demo import build_prompt


DEFAULT_MODEL = (
    "/home/junhaoy/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B/"
    "snapshots/c1899de289a04d12100db370d81485cdf75e47ca"
)


def build_completion_prompt(repetitions: int, round_id: int) -> str:
    return f"{build_prompt(repetitions)}\n\nRound {round_id}: give 3 short summaries."


def format_chunk_hash(chunk_hash: int) -> str:
    return f"{chunk_hash:x}"


def resolve_num_layers(model: str, explicit_num_layers: int | None) -> int | None:
    if explicit_num_layers is not None:
        return explicit_num_layers

    config = AutoConfig.from_pretrained(model)
    for attr_name in ("num_hidden_layers", "n_layer", "num_layers"):
        value = getattr(config, attr_name, None)
        if isinstance(value, int) and value > 0:
            return value
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "List the embedded LMCache files that correspond to a prompt. "
            "Schema: <model>@<world_size>@<worker_id>@<chunk_hash>@<dtype>.data "
            "(or add @<layer_id> when --layerwise is enabled)"
        )
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--repetitions", type=int, default=96)
    parser.add_argument("--round-id", type=int, default=1)
    parser.add_argument("--chunk-size", type=int, default=256)
    parser.add_argument("--world-size", type=int, default=1)
    parser.add_argument("--worker-id", type=int, default=0)
    parser.add_argument("--kv-dtype", default="bfloat16")
    parser.add_argument("--hash-algorithm", default="builtin")
    parser.add_argument(
        "--layerwise",
        dest="layerwise",
        action="store_true",
    )
    parser.add_argument(
        "--no-layerwise",
        dest="layerwise",
        action="store_false",
    )
    parser.add_argument("--num-layers", type=int, default=None)
    parser.add_argument(
        "--save-unfull-chunk",
        dest="save_unfull_chunk",
        action="store_true",
    )
    parser.add_argument(
        "--no-save-unfull-chunk",
        dest="save_unfull_chunk",
        action="store_false",
    )
    parser.set_defaults(layerwise=True, save_unfull_chunk=False)
    parser.add_argument("--cache-dir", default=".kvcache")
    args = parser.parse_args()

    num_layers = resolve_num_layers(args.model, args.num_layers) if args.layerwise else None
    if args.layerwise and num_layers is None:
        raise SystemExit(
            "Could not determine num_layers from model config. "
            "Pass --num-layers explicitly when using --layerwise."
        )

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    prompt = build_completion_prompt(args.repetitions, args.round_id)
    token_ids, chunk_hashes, filenames = compute_cache_filenames(
        tokenizer=tokenizer,
        prompt=prompt,
        model_name=args.model,
        world_size=args.world_size,
        worker_id=args.worker_id,
        kv_dtype=args.kv_dtype,
        chunk_size=args.chunk_size,
        hash_algorithm=args.hash_algorithm,
        use_layerwise=args.layerwise,
        num_layers=num_layers,
        save_unfull_chunk=args.save_unfull_chunk,
    )
    cache_dir = Path(args.cache_dir).resolve()
    complete_chunks = len(set(chunk_hashes))

    print(f"model: {args.model}")
    print(f"tokens: {len(token_ids)}")
    print(f"complete_chunks: {complete_chunks}")
    print(f"total_files: {len(filenames)}")
    print(f"world_size: {args.world_size}")
    print(f"worker_id: {args.worker_id}")
    print(f"kv_dtype: {args.kv_dtype}")
    print(f"hash_algorithm: {args.hash_algorithm}")
    print(f"layerwise: {args.layerwise}")
    print(f"save_unfull_chunk: {args.save_unfull_chunk}")
    if args.layerwise:
        print(f"num_layers: {num_layers}")
    print(f"cache_dir: {cache_dir}")
    print("-" * 80)

    existing_count = 0
    for idx, (chunk_hash, filename) in enumerate(zip(chunk_hashes, filenames)):
        file_path = cache_dir / filename
        exists = file_path.exists()
        if exists:
            existing_count += 1
        status = "EXISTS" if exists else "MISSING"
        print(f"[{idx:02d}] {status:7s} {format_chunk_hash(chunk_hash)}  {file_path}")

    print("-" * 80)
    print(f"existing_files: {existing_count}/{len(chunk_hashes)}")


if __name__ == "__main__":
    main()
