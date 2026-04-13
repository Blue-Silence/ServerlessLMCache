# Current Approach

This document records the approach currently used in this repository to validate `LMCache + vLLM`, including the design rationale, known constraints, and the key conclusions we confirmed during debugging.

This document describes the setup that has already been validated to work. It is not a survey of every possible approach.

Additional note: the default focus of follow-up work in this repository has now shifted to the `embedded priority-fs` path.

- `embedded_demo/` is now the primary path we should keep pushing forward.
- The `LMCache MP` path is still kept for historical comparison and regression validation.
- Unless stated otherwise, new work should default to the embedded scripts, server, and helpers.
- Under `embedded`, do not enable both `layerwise` and `partial / unfull chunk replay` at the same time right now.
  Relevant investigation notes:
  [docs/embedded-layerwise-partial-chunk-investigation.en.md](/home/junhaoy/ServerlessLMCache/docs/embedded-layerwise-partial-chunk-investigation.en.md)
- The embedded default has also been switched to:
  - `layerwise = on`
  - `save_decode_cache = on`
  - `save_unfull_chunk = off`

## 1. What We Want To Achieve

What we want to implement and validate is not just "LMCache can be connected to vLLM", but a more specific cache semantic:

- Always write to directory `A`
- Always try reading from directory `B` first
- Fall back to `A` when `B` misses

Current convention:

- `A = .kvcache`
- `B = .kvcache_remote`

In other words, we want to simulate a cache layout where the write path and the preferred read path are separated:

- `A` acts more like the primary write directory
- `B` acts more like the preferred read directory

On top of that, we also want to validate several things:

- Whether we can simulate "another machine reading a shared directory" on a single machine
- Whether we can distinguish LMCache's own `L1` hits from disk-level `L2` hits
- Whether cache keys remain stable after a service restart so that existing on-disk content can really be reused
- Whether lookup can be done directly through distributed filesystem directory scanning and filename encoding, without an extra control plane, database, or standalone index service

That last point is especially important. We are not trying to build a cache system that only works if there is a separate index service or metadata service. Instead, we want:

- The backend to write KV chunks directly into a shared or distributed filesystem
- Reads to be resolved directly through filesystem entry scan / exists / filename decoding
- No separate control plane that stores chunk indices
- A second machine to potentially read existing cache directly as long as it mounts the same shared directory

## 2. How We Achieved It

To implement the semantics above, we ended up using:

- `vLLM + LMCacheMPConnector`
- `LMCache MP server`
- Multiple adapters plus a custom store policy

instead of the single-process embedded `LMCacheConnectorV1`.

The reason is straightforward:

- Single-process embedded mode is better for the simplest possible integration
- But it does not express "write only to A / read B first / then fall back to A" very clearly
- That semantic fits the multi-adapter architecture of `LMCache MP` better
- We also want the backend to rely on the shared filesystem as directly as possible, rather than introducing an extra control plane or centralized index service

The implementation has 3 steps:

### 2.1 Set the adapter order to `B -> A`

When starting the `LMCache MP server`, the adapter order is fixed as:

1. `B = .kvcache_remote`
2. `A = .kvcache`

The point is to make the default read path always check `B` first.

### 2.2 Keep the default `PrefetchPolicy=default`

LMCache's default `PrefetchPolicy=default` selects the first adapter that hits for each key.

So with the order `B -> A`, we naturally get:

- Check `B` first
- If `B` misses, check `A`

### 2.3 Add a `write_last` policy

We added a custom `StorePolicy=write_last`:

- [demo/write_last_store_policy.py](/home/junhaoy/ServerlessLMCache/demo/write_last_store_policy.py)

Its behavior is:

- Every key is written only to the last adapter

Since the current adapter order is `[B, A]`, the last adapter is `A`.

So the final semantic becomes:

- Write only to `A`
- Prefer reading from `B`
- Fall back to `A` when `B` misses

### 2.4 Keep the backend as free as possible from an extra control plane

There is also a very practical reason why the current default prefers the `fs` adapter:

- It is closer to the "directly rely on a shared filesystem" model we want to validate
- Cache objects are written to disk directly as files
- Lookup relies on the filesystem itself, not on an external index service

In other words, we are intentionally aiming for a simpler backend shape:

- Data lives in the distributed filesystem
- As long as another machine mounts the same directory, it can look up the same keys
- It does not need to ask some central control plane where the chunk is located first

## 3. Current Architecture

The current architecture has 3 parts:

1. `LMCache MP server`
2. `vLLM API server`
3. A minimal client script used to send requests

Relevant files:

- [demo/write_last_store_policy.py](/home/junhaoy/ServerlessLMCache/demo/write_last_store_policy.py)
- [demo/run_lmcache_mp_server.py](/home/junhaoy/ServerlessLMCache/demo/run_lmcache_mp_server.py)
- [demo/request_demo.py](/home/junhaoy/ServerlessLMCache/demo/request_demo.py)
- [scripts/run_lmcache_mp_priority_demo.sh](/home/junhaoy/ServerlessLMCache/scripts/run_lmcache_mp_priority_demo.sh)
- [scripts/run_vllm_lmcache_mp_demo.sh](/home/junhaoy/ServerlessLMCache/scripts/run_vllm_lmcache_mp_demo.sh)

## 4. A/B Read-Write Strategy

### 4.1 Adapter order

When starting the `LMCache MP server`, the adapter order is fixed as:

1. `B = .kvcache_remote`
2. `A = .kvcache`

That means `B` is always first.

### 4.2 PrefetchPolicy

We currently use LMCache's default `PrefetchPolicy=default`.

Its behavior is:

- For each key, select the first adapter that hits

So with our ordering:

- Check `B` first
- If `B` misses, check `A`

### 4.3 StorePolicy

We added a custom policy called `write_last`:

- [demo/write_last_store_policy.py](/home/junhaoy/ServerlessLMCache/demo/write_last_store_policy.py)

Its behavior is:

- Every key is written only to the last adapter

Since the current adapter order is `[B, A]`, the last one is `A`.

So the final effect is:

- Write only to `A`
- Prefer reading from `B`
- Fall back to `A`

## 5. Why We Use the `fs` Adapter by Default First

Current default script:

- [scripts/run_lmcache_mp_priority_demo.sh](/home/junhaoy/ServerlessLMCache/scripts/run_lmcache_mp_priority_demo.sh)

By default it uses the `fs` adapter instead of defaulting directly to `GDS`.

This is not because the functionality is impossible with `GDS`, but because we want to separate "semantic validation" from "GDS/NIXL environment compatibility":

- The `fs` adapter is the easiest way to validate A/B read-write semantics
- `GDS` / `nixl_store` depend more heavily on the lower-level environment
- The `fs` adapter is also the closest to our goal of "a backend that works directly on a shared filesystem with no extra control plane"

At the single-machine simulation and strategy-debugging stage, `fs` is more stable.

If we want to switch back to the GDS path later, we can use:

- [scripts/run_lmcache_mp_priority_gds_demo.sh](/home/junhaoy/ServerlessLMCache/scripts/run_lmcache_mp_priority_gds_demo.sh)

## 6. Current Key Scripts

### 6.1 Start LMCache MP

```bash
bash scripts/run_lmcache_mp_priority_demo.sh
```

This script:

- Fixes the adapter order to `B -> A`
- Uses `write_last`
- Disables observability by default so extra dependencies do not affect the demo
- Fixes `PYTHONHASHSEED=0`

### 6.2 Start vLLM

```bash
bash scripts/run_vllm_lmcache_mp_demo.sh
```

This script:

- Uses `LMCacheMPConnector`
- Automatically sets `--disable-hybrid-kv-cache-manager`
- Defaults `GPU_MEMORY_UTILIZATION=0.5`
- Defaults `PYTHONHASHSEED=0`
- Prefers resolving a local Hugging Face snapshot first, and sets `HF_HUB_OFFLINE=1` when a local snapshot is found

### 6.3 Send requests

```bash
python demo/request_demo.py
```

To validate "the first request after restart" more cleanly, this is recommended:

```bash
python demo/request_demo.py --requests 1
```

## 7. Why The Request Script Uses `completions`

Current [demo/request_demo.py](/home/junhaoy/ServerlessLMCache/demo/request_demo.py) uses plain `completions`, not `chat.completions`.

The reason is that we already confirmed:

- `chat.completions` is easily affected by chat template wrapping
- That makes it less stable for validating exact prefix-key hits across restarts

After switching to `completions`:

- The prompt is more direct
- The token sequence is easier to keep stable
- It is better suited for LMCache prefix-key validation

## 8. Key Facts Confirmed During Debugging

### 8.1 It is not "nothing was written to disk"

Current `.kvcache` really does contain actual `.data` files.

For example, we already confirmed:

- `.kvcache` contains dozens of `.data` files
- The directory size is around `1.6G`

And `.kvcache_remote` is empty by default, which is expected under the current strategy:

- Because we write only to `A`
- Not to `B`

### 8.1.1 The current backend follows a "filesystem direct lookup" idea

For the current default `fs` adapter, we have already confirmed that it follows a design based directly on the filesystem:

- `ObjectKey` is encoded into the filename
- KV chunks are written directly as `.data` files
- It does not depend on a separate centralized index service

So it matches the goal we want to validate:

- Keep the backend as simple as possible
- Let the shared directory itself be the shared medium
- In theory, a new instance or a second machine can try to reuse those files as long as it sees the same directory

### 8.1.2 What `kv_rank` means

In current on-disk filenames, in addition to `model_name` and `chunk_hash`, there is also a `kv_rank`.

Its role is not "request ID". Instead, it:

- Identifies which parallel slice this KV cache belongs to
- Prevents KV data from different workers or different parallel configurations from being mixed together

In LMCache, an object key is mainly composed of 3 parts:

- `chunk_hash`
- `model_name`
- `kv_rank`

The related code is in:

- [api.py](/home/junhaoy/ServerlessLMCache/.venv/lib/python3.12/site-packages/lmcache/v1/distributed/api.py)

`kv_rank` is computed from the following information:

- `world_size`
- `global_rank`
- `local_world_size`
- `local_rank`

In our current single-GPU, single-worker setup, it is basically fixed at:

```text
0x01000100
```

So in the current demo, it is fine to think of it as:

- The KV cache namespace ID for the current worker

In more complex TP/PP parallel scenarios, files with different `kv_rank` values cannot be treated as the same cache directly.

### 8.2 It is not "the GPU architecture problem is still unresolved"

At the beginning we hit:

- `CUDA error: no kernel image is available for execution on the device`

The root cause was:

- The official wheel's `lmcache.c_ops` did not include `sm_120` for your `RTX 5060 Ti`

Later we already:

- Built a new `lmcache` wheel from source
- Replaced the wheel inside the current `.venv`
- Confirmed that `c_ops` now includes `sm_120`

The version currently installed in this environment is:

- `lmcache 0.4.3.dev82`

### 8.3 It is not a key mismatch within the same request

We added runtime debug logging to `LMCache MP`:

- `HASH_DEBUG lookup ...`
- `HASH_DEBUG store ...`

That confirmed:

- Within the same request, the chunk hashes used by `lookup` and `store` are identical

In other words, the read/write key space within a single request is consistent.

### 8.4 The real reason cross-restart cache misses happened

The root cause is how `vllm` initializes `NONE_HASH`.

In the current `vllm`:

- If `PYTHONHASHSEED` is not set
- `NONE_HASH` is initialized with a random seed

Related code:

- [kv_cache_utils.py](/home/junhaoy/ServerlessLMCache/.venv/lib/python3.12/site-packages/vllm/v1/core/kv_cache_utils.py)

That means:

- Same prompt
- After different process restarts
- Produces different rolling prefix hashes

So we end up with this situation:

- `.data` files already exist on disk
- But lookup still misses after restart

The fix is to pin:

```bash
PYTHONHASHSEED=0
```

Both startup scripts already pin this value by default.

## 9. Why The Logs Show "14 L1, 0 L2"

This log line comes from LMCache MP's own storage manager:

- [storage_manager.py](/home/junhaoy/ServerlessLMCache/.venv/lib/python3.12/site-packages/lmcache/v1/distributed/storage_manager.py)

It means:

- How many hits occurred in LMCache's own internal `L1`
- How many hits occurred in LMCache's own internal `L2`

This is not the same metric as what `vllm` prints:

- `Prefix cache hit rate`
- `External prefix cache hit rate`

In other words:

- LMCache's `L1/L2` are LMCache-internal layers
- `vllm`'s `External prefix cache hit rate` is the connector external-token statistic seen by the scheduler

These two sets of numbers cannot be mapped one-to-one directly.

## 10. The Correct Way To Validate

### 10.1 Validate disk writes

```bash
python demo/request_demo.py --requests 1
find .kvcache -maxdepth 1 -type f -name '*.data'
```

If disk writes are working, you should see `.data` files under `.kvcache`.

### 10.2 Validate "the first request after restart"

Do not send two requests in one run to validate restart behavior, because the second request can easily hit LMCache `L1` directly.

The correct flow is:

1. Start `LMCache MP`
2. Start `vllm`
3. Send one request
4. Stop `LMCache MP`
5. Stop `vllm`
6. Restart both
7. Send one request again

Recommended command:

```bash
python demo/request_demo.py --requests 1
```

### 10.3 How to tell whether the disk-level path was used

If you want to see whether disk-level `L2` was really used, focus on LMCache's own logs:

- Ideally you want something closer to `0 L1, N L2`

If you see:

- `14 L1, 0 L2`

that means the hit came from LMCache's in-process `L1`, not the disk-level `L2`.

## 11. Current Known Limitations

### 11.1 The current demo is mainly a single-machine simulation

Although semantically we are simulating:

- Machine 1 writes `A`
- Machine 2 reads `B`, then falls back to `A`

the main validation in this repository is still being done on a single machine.

### 11.2 The `fs` adapter is good for validating semantics first

The `fs` adapter is suitable for:

- A/B strategy validation
- Single-machine simulation
- Shared-directory validation

But if we later want a higher-performance path closer to production, we should keep looking at:

- `GDS`
- `nixl_store`
- More realistic shared-storage environments

### 11.3 Versions are not the main issue, but GPU architecture support is critical

We already confirmed:

- The core problem was not `vllm 0.19.0`
- The real key was whether the `LMCache` wheel included `sm_120`

So if we move to another environment later, the first thing to confirm is:

- Whether `lmcache.c_ops` was really built for the target GPU architecture

## 12. Current Recommended Workflow

If you simply want to keep validating with the current approach, the recommended command set is always:

### First write

```bash
bash scripts/run_lmcache_mp_priority_demo.sh
bash scripts/run_vllm_lmcache_mp_demo.sh
python demo/request_demo.py --requests 1
```

### Validate after restart

```bash
bash scripts/run_lmcache_mp_priority_demo.sh
bash scripts/run_vllm_lmcache_mp_demo.sh
python demo/request_demo.py --requests 1
```

Between runs, make sure the old processes have fully exited.

## 13. Conclusion

The current setup has already confirmed several important things:

- The A/B read-write strategy itself is wired up correctly
- `LMCache MP` can write to disk normally
- The `sm_120` compatibility problem on `RTX 5060 Ti` has been solved by local compilation
- Unpinned `PYTHONHASHSEED` causes cross-restart key drift, and that has already been fixed

If we still need to debug why a certain run did not hit `L2`, the first things to check are:

- Whether this was the first request after restart
- Whether the LMCache logs show `L1` hits or `L2` hits
- Whether the current run's `HASH_DEBUG lookup` matches the previous run's stored hashes

## 14. Optional Embedded `priority-fs` Path

The repository now also includes an optional path that does not require a separate `LMCache MP` process:

- Startup script:
  - [run_vllm_lmcache_embedded_priority_fs_demo.sh](/home/junhaoy/ServerlessLMCache/embedded_demo/run_vllm_lmcache_embedded_priority_fs_demo.sh)
- Custom connector plugin:
  - [priority_fs_adapter.py](/home/junhaoy/ServerlessLMCache/embedded_demo/priority_fs_adapter.py)

The design goals of this path are:

- Keep using `fs` backend semantics
- Avoid modifying the installed `lmcache` package source
- Implement the behavior inside this repository through the remote connector plugin extension point already exposed by LMCache
- Continue to preserve:
  - Prefer reading `B = .kvcache_remote`
  - Fall back to `A = .kvcache` when `B` misses
  - Write only to `A`
- Also currently allow:
  - Decode-cache writes
  - Partial / unfull chunk writes

In addition, the repository keeps a dedicated write-skip switch for the embedded `priority-fs` path:

- Set environment variable `PRIORITY_FS_SKIP_WRITE=1`
- Then the repo-local backend in `embedded_demo/priority_fs_adapter.py` will skip remote/fs writes directly
- This only affects writes in embedded `priority-fs`, not the read semantics
- So:
  - It still reads `B = .kvcache_remote` first
  - It still falls back to `A = .kvcache`
- This switch works for both `layerwise` and `non-layerwise`
- It does not affect the `LMCache MP` path

The implementation does not hack the filesystem API. Instead, the embedded path reuses two official `FSConnector` instances:

- One handles the preferred read path `B`
- One handles fallback reads and the only write path `A`

There is another easy-to-miss but practically important debugging detail here:

- The embedded path uses the `FSConnector + CacheEngineKey` filename schema:
  - `<model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>.data`
- If `layerwise` is enabled, it switches to the `LayerCacheEngineKey` schema:
  - `<model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>@<layer_id>.data`
- This is different from the default `fs` adapter schema used by the `LMCache MP` path:
  - `<model_name>@<kv_rank_hex>@<chunk_hash_hex>.data`
- So do not infer embedded `.kvcache` / `.kvcache_remote` filenames directly from the MP-path filename format

If `save_unfull_chunk=on` is explicitly enabled, it also means:

- The final tail chunk that does not fill a full chunk may also be persisted separately
- After later decode growth, disk may contain both a "partial version" and a longer overlapping version
- These files are not append relationships; they coexist as different keys and different files
- This is a semantic we currently accept and want to keep, because it makes it easier to infer full prefill + decode context directly from filenames

The repository now also provides a corresponding set of helpers specifically for the embedded path:

- [prompt_cache_files.py](/home/junhaoy/ServerlessLMCache/embedded_demo/cache_files/prompt_cache_files.py)
- [list_prompt_cache_files.py](/home/junhaoy/ServerlessLMCache/embedded_demo/cache_files/list_prompt_cache_files.py)
  - Defaults to calculating with `layerwise=on`
  - Supports `--no-layerwise`
  - Supports `--num-layers` when needed
  - Defaults to calculating with `save_unfull_chunk=off`
  - Add `--save-unfull-chunk` if partial tails need to be included

The default validation path has now switched to `embedded priority-fs`; the `LMCache MP` solution is kept as a historical comparison path.

### 14.1 Current layerwise blocker

Under the current `embedded priority-fs` path, `layerwise + partial / unfull chunk` is still not truly working end to end. The current default combination is:

- `layerwise = on`
- `save_unfull_chunk = off`
- `save_decode_cache = on`

Current confirmed conclusions are:

- `layerwise` itself can now work on the embedded `priority-fs` path
- The earlier repo-local adaptation for `Shape dimension should be 4` has already been covered by the layerwise-aware FS wrapper in `priority_fs_adapter.py`
- What still does not work is:
  - `layerwise = on`
  - together with
  - `save_unfull_chunk = on`

So the problem has now narrowed from "layerwise itself does not work" to:

- `embedded priority-fs`
- under
- `layerwise + partial / unfull chunk`
- still being unreliable as a combination

If we continue working on this, we should prioritize repo-local approaches and avoid modifying the installed `lmcache` package source directly.

If we want a long-running Python server instead of a one-shot script, we can also use:

- [run_vllm_async_engine_priority_fs_server.py](/home/junhaoy/ServerlessLMCache/embedded_demo/run_vllm_async_engine_priority_fs_server.py)
- Recommended startup script:
  - [run_vllm_async_engine_priority_fs_server.sh](/home/junhaoy/ServerlessLMCache/embedded_demo/run_vllm_async_engine_priority_fs_server.sh)

This script:

- Creates `AsyncLLMEngine` directly in Python
- Exposes a minimal OpenAI-compatible interface:
  - `/health`
  - `/v1/models`
  - `/v1/completions`
- Allows continued reuse of the existing [demo/request_demo.py](/home/junhaoy/ServerlessLMCache/demo/request_demo.py)
- Also provides an embedded-specific request script:
  - [request_demo.py](/home/junhaoy/ServerlessLMCache/embedded_demo/request_demo.py)

One very important implementation detail here is:

- `PYTHONHASHSEED=0` must still be set before the Python interpreter starts
- So the recommended entry point is now always [run_vllm_async_engine_priority_fs_server.sh](/home/junhaoy/ServerlessLMCache/embedded_demo/run_vllm_async_engine_priority_fs_server.sh)
- The Python script itself only performs a fail-fast check and no longer re-`exec`s itself
- If the Python script is run directly without setting `PYTHONHASHSEED=0` first, it will fail immediately with a reminder

This embedded mainline has now been switched to:

- LMCache-related parameters are controlled by LMCache's native YAML config files
- The startup script points `LMCACHE_CONFIG_FILE` to the current profile by default
- The Python server also sets a default `LMCACHE_CONFIG_FILE` at the very start of `main()`

The current default profile is:

- [default_layerwise_unfull_off.yaml](/home/junhaoy/ServerlessLMCache/embedded_demo/configs/default_layerwise_unfull_off.yaml)

Common profiles currently include:

- [default_layerwise_unfull_off.yaml](/home/junhaoy/ServerlessLMCache/embedded_demo/configs/default_layerwise_unfull_off.yaml)
- [non_layerwise_unfull_off.yaml](/home/junhaoy/ServerlessLMCache/embedded_demo/configs/non_layerwise_unfull_off.yaml)
- [non_layerwise_unfull_on.yaml](/home/junhaoy/ServerlessLMCache/embedded_demo/configs/non_layerwise_unfull_on.yaml)

To switch profiles, it is recommended to swap the config file directly, for example:

```bash
LMCACHE_CONFIG_FILE_PATH=embedded_demo/configs/non_layerwise_unfull_on.yaml \
  bash embedded_demo/run_vllm_async_engine_priority_fs_server.sh
```

or:

```bash
python embedded_demo/run_vllm_async_engine_priority_fs_server.py \
  --config embedded_demo/configs/non_layerwise_unfull_off.yaml
```

If you only want to validate read behavior / hit behavior temporarily but do not want the embedded backend to actually write to disk, you can also do:

```bash
PRIORITY_FS_SKIP_WRITE=1 \
  bash embedded_demo/run_vllm_async_engine_priority_fs_server.sh
```

In addition, we have also confirmed:

- vLLM Async engine officially supports `pause_generation(mode="keep")` / `resume_generation()`
- It is not a native chunk-aware interface
- But if the outer layer can already count LMCache chunk boundaries, it can actively pause at the boundary and resume later
- So "chunk by chunk generate" is better understood as a combination of:
  - outer boundary counting
  - plus
  - engine pause / resume
- But we have not seen a public interface that allows changing the LMCache save strategy of the same in-flight request while paused; if we need to switch something like `lmcache.skip_save`, the more realistic way is to end the current request and create a new one with a different strategy
- Async engine input is not limited to string prompts; it also supports directly passing token IDs. If we later need precise chunk-boundary control, driving it by token IDs will be more natural
