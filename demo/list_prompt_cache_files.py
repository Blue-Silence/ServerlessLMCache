import argparse
from pathlib import Path
import sys

from transformers import AutoTokenizer

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from demo.prompt_cache_files import compute_cache_filenames
from demo.request_demo import build_prompt


DEFAULT_MODEL = (
    "/home/junhaoy/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B/"
    "snapshots/c1899de289a04d12100db370d81485cdf75e47ca"
)


def build_completion_prompt(repetitions: int, round_id: int) -> str:
    return f"{build_prompt(repetitions)}\n\nRound {round_id}: give 3 short summaries."


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List the LMCache files that correspond to a prompt."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--repetitions", type=int, default=96)
    parser.add_argument("--round-id", type=int, default=1)
    parser.add_argument("--chunk-size", type=int, default=256)
    parser.add_argument("--kv-rank", default="0x01000100")
    parser.add_argument("--cache-dir", default=".kvcache")
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    prompt = build_completion_prompt(args.repetitions, args.round_id)
    token_ids, chunk_hashes, filenames = compute_cache_filenames(
        tokenizer=tokenizer,
        prompt=prompt,
        model_name=args.model,
        kv_rank=args.kv_rank,
        chunk_size=args.chunk_size,
    )
    cache_dir = Path(args.cache_dir).resolve()

    print(f"model: {args.model}")
    print(f"tokens: {len(token_ids)}")
    print(f"complete_chunks: {len(chunk_hashes)}")
    print(f"kv_rank: {args.kv_rank}")
    print(f"cache_dir: {cache_dir}")
    print("-" * 80)

    existing_count = 0
    for idx, (chunk_hash, filename) in enumerate(zip(chunk_hashes, filenames)):
        file_path = cache_dir / filename
        exists = file_path.exists()
        if exists:
            existing_count += 1
        status = "EXISTS" if exists else "MISSING"
        print(f"[{idx:02d}] {status:7s} {chunk_hash.hex()}  {file_path}")

    print("-" * 80)
    print(f"existing_files: {existing_count}/{len(chunk_hashes)}")


if __name__ == "__main__":
    main()
