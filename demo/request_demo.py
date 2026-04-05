import argparse
import textwrap
import time

from openai import OpenAI
from openai import APIConnectionError


def build_prompt(repetitions: int) -> str:
    shared_prefix = textwrap.dedent(
        """
        Shared prefix for KV-cache reuse demo.
        ServerlessLMCache uses vLLM with LMCache.
        LMCache stores KV cache outside GPU memory.
        The same prefix can be reused after restart.
        """
    ).strip()
    return "\n".join(shared_prefix for _ in range(repetitions))


def resolve_model(client: OpenAI, model: str | None) -> str:
    if model:
        return model
    models = client.models.list().data
    if not models:
        raise RuntimeError("No served models were returned by /v1/models")
    return models[0].id


def run_request(client: OpenAI, model: str, prompt: str, round_id: int) -> None:
    start = time.perf_counter()
    response = client.completions.create(
        model=model,
        prompt=f"{prompt}\n\nRound {round_id}: give 3 short summaries.",
        max_tokens=64,
        temperature=0,
    )
    elapsed = time.perf_counter() - start
    answer = response.choices[0].text or ""
    print(f"[round {round_id}] latency={elapsed:.2f}s")
    print(answer.strip())
    print("-" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send repeated requests to a local vLLM server backed by LMCache."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--model", default=None)
    parser.add_argument("--repetitions", type=int, default=96)
    parser.add_argument("--requests", type=int, default=2)
    args = parser.parse_args()

    client = OpenAI(base_url=args.base_url, api_key=args.api_key)
    try:
        model = resolve_model(client, args.model)
    except APIConnectionError as exc:
        raise SystemExit(
            "Could not connect to the local vLLM server at "
            f"{args.base_url}. Start vLLM first and wait until "
            "/v1/models responds before running this script."
        ) from exc
    prompt = build_prompt(args.repetitions)

    print(f"Using model: {model}")
    print(
        "First request warms LMCache. If you restart the server and rerun this script, "
        "the next run should reuse the stored prefix from the configured GDS path."
    )
    print("-" * 60)

    for round_id in range(1, args.requests + 1):
        run_request(client, model, prompt, round_id=round_id)


if __name__ == "__main__":
    main()
