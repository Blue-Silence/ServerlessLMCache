from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LMCACHE_CONFIG_FILE = (
    ROOT_DIR / "embedded_demo" / "configs" / "default_layerwise_unfull_off.yaml"
)
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def require_pythonhashseed(seed: str = "0") -> None:
    current = os.environ.get("PYTHONHASHSEED")
    if current == seed:
        return
    raise SystemExit(
        "PYTHONHASHSEED must be set to 0 before starting this script. "
        "Use embedded_demo/run_vllm_async_engine_priority_fs_server.sh or run "
        f"`PYTHONHASHSEED={seed} python embedded_demo/run_vllm_async_engine_priority_fs_server.py`."
    )


require_pythonhashseed()


def resolve_model(model: str, model_source: str) -> str:
    if model_source == "remote":
        return model

    model_path = Path(model)
    if model_path.exists():
        return model

    try:
        from huggingface_hub import scan_cache_dir

        info = scan_cache_dir()
        for repo in info.repos:
            if repo.repo_id != model:
                continue
            revisions = list(repo.revisions)
            if revisions:
                return str(revisions[0].snapshot_path)
    except Exception:
        pass

    return model


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    prompt: str
    max_tokens: int = 16
    temperature: float = 0.0
    stream: bool = False


def build_app(args) -> FastAPI:
    resolved_model = resolve_model(args.model, args.model_source)
    if resolved_model != args.model:
        os.environ["HF_HUB_OFFLINE"] = "1"

    from vllm.config import KVTransferConfig
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.entrypoints.openai.api_server import (
        build_async_engine_client_from_engine_args,
    )

    kv_transfer_config = KVTransferConfig(
        kv_connector="LMCacheConnectorV1",
        kv_role="kv_both",
    )
    engine_args = AsyncEngineArgs(
        model=resolved_model,
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_memory_utilization,
        disable_hybrid_kv_cache_manager=True,
        kv_transfer_config=kv_transfer_config,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with build_async_engine_client_from_engine_args(engine_args) as engine:
            app.state.engine = engine
            app.state.model_id = resolved_model
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        model_id = app.state.model_id
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": now,
                    "owned_by": "serverless-lmcache-demo",
                }
            ],
        }

    @app.post("/v1/completions")
    async def completions(request: CompletionRequest) -> dict[str, Any]:
        if request.stream:
            raise HTTPException(
                status_code=400,
                detail="stream=true is not supported by this minimal async-engine demo",
            )

        from vllm import SamplingParams

        engine = app.state.engine
        request_id = f"cmpl-{uuid.uuid4().hex}"
        sampling_params = SamplingParams(
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )

        final_output = None
        async for request_output in engine.generate(
            {"prompt": request.prompt},
            sampling_params,
            request_id,
        ):
            final_output = request_output

        if final_output is None:
            raise HTTPException(status_code=500, detail="No completion output returned")

        choice = final_output.outputs[0] if final_output.outputs else None
        text = "" if choice is None else choice.text
        finish_reason = None if choice is None else choice.finish_reason
        usage = {
            "prompt_tokens": len(final_output.prompt_token_ids or []),
            "completion_tokens": 0 if choice is None else len(choice.token_ids),
            "total_tokens": len(final_output.prompt_token_ids or [])
            + (0 if choice is None else len(choice.token_ids)),
        }
        return {
            "id": request_id,
            "object": "text_completion",
            "created": int(time.time()),
            "model": app.state.model_id,
            "choices": [
                {
                    "text": text,
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }

    return app


def main() -> None:
    os.environ.setdefault("LMCACHE_USE_EXPERIMENTAL", "True")
    os.environ.setdefault("LMCACHE_CONFIG_FILE", str(DEFAULT_LMCACHE_CONFIG_FILE))

    parser = argparse.ArgumentParser(
        description=(
            "Run a minimal OpenAI-compatible Python server backed by "
            "vLLM AsyncLLMEngine with embedded LMCache priority-fs."
        )
    )
    parser.add_argument("--model", default="Qwen/Qwen3-0.6B")
    parser.add_argument("--model-source", default="auto", choices=["auto", "remote"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-model-len", type=int, default=4096)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.5)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    if args.config is not None:
        os.environ["LMCACHE_CONFIG_FILE"] = str(Path(args.config).expanduser().resolve())

    app = build_app(args)

    print(f"Starting AsyncLLMEngine Python server on http://{args.host}:{args.port}")
    print(f"Model: {args.model}")
    print(f"LMCache config file: {os.environ['LMCACHE_CONFIG_FILE']}")
    print(f"PYTHONHASHSEED: {os.environ.get('PYTHONHASHSEED', 'unset')}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
