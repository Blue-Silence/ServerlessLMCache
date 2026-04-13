# Embedded `priority-fs`: Conclusions On `layerwise` And Partial Chunks

This document is dedicated to the current investigation results for the `LAYERWISE=1` and `SAVE_UNFULL_CHUNK=1` combination on the `embedded priority-fs` mainline.

The goal is not to review the entire history, but to clearly record the conclusions we have already converged on so we do not keep spending time in the wrong direction.

## 1. Conclusion First

The repository default should currently be treated as:

- `layerwise` and `partial / unfull chunk` cannot be enabled at the same time
- More precisely:
  - `LAYERWISE=1 SAVE_UNFULL_CHUNK=1` is currently unreliable
  - `LAYERWISE=1 SAVE_UNFULL_CHUNK=0` is currently usable
  - `layerwise=off` is currently usable

If correctness matters, the current recommendation is to choose one of these two:

- Either keep `layerwise=off`
- Or forbid partial chunk replay when `layerwise=on`

The following combination is not recommended as the default mainline config right now:

```bash
LAYERWISE=1 SAVE_UNFULL_CHUNK=1
```

## 2. Most Important Confirmed Facts

The following conclusions are now basically confirmed:

1. What is broken is not the `priority-fs` file-layer I/O.
2. For the final partial tail chunk, the following fields all match between `put/get`:
   - `key`
   - `len`
   - `fmt`
   - `shape`
   - `crc32`
3. In the current reproduction, the tail chunk on disk has:
   - `fmt=MemoryFormat.KV_T2D`
   - `shape=[74, 2, 1024]`
   - The total prefix hit in this request is `3658 = 14 * 256 + 74`
4. After restart, the logs of the second request clearly show:
   - `LMCache hit tokens: 3658`
   - `need to load: 3657`
5. `LAYERWISE=1 SAVE_UNFULL_CHUNK=0` works.
6. `layerwise=off` works.
7. We previously had a repo-local diagnostic workaround:
   - Force the layerwise replay generator to run to completion right after `start_load_kv()`
   - That workaround restored correct output for the same cache and the same request
   - But that diagnostic patch has now been removed from the repository and is no longer kept as a long-term solution

Taken together, this evidence shows:

- The tail file itself is not corrupted
- The key/hash is not inconsistent
- The A/B `priority-fs` read-write semantics are not wrong
- The truly unreliable part is upstream `layerwise replay` behavior when a partial tail chunk exists

## 3. Why `SAVE_UNFULL_CHUNK=0` Works But `=1` Does Not

This is the point that is easiest to keep mixing up.

Using the current reproduction numbers as an example:

- `chunk_size = 256`
- `3658 = 14 * 256 + 74`

So the prefix can be split into:

- The first 14 full chunks, totaling `3584` tokens
- The last partial tail chunk, totaling `74` tokens

The real difference between the two settings is:

### 3.1 `SAVE_UNFULL_CHUNK=0`

- Only the first 14 full chunks are saved
- The final `74` tokens do not participate in cache replay
- After restart, the system can reuse at most `3584` tokens as prefix
- The final `74` tokens are recomputed

So this path never sends the partial tail chunk into `layerwise replay`.

### 3.2 `SAVE_UNFULL_CHUNK=1`

- The final `74` tokens are also persisted
- Lookup also counts that tail as a hit
- So the logs show:
  - `LMCache hit tokens: 3658`
  - `need to load: 3657`

At that point, the system no longer recomputes the KV for the final `74` tokens. Instead, it requires `layerwise replay` to restore that partial tail as well.

This is exactly where the current problem shows up:

- Replay of full chunks currently works
- Once the partial tail chunk really enters `layerwise replay`
- The output becomes wrong

So although it looks like "only 74 tokens are different" on the surface, the real meaning is:

- `SAVE_UNFULL_CHUNK=0` avoids the bug
- `SAVE_UNFULL_CHUNK=1` exposes the bug

## 4. Why Things Work When `layerwise` Is Disabled

When `layerwise` is disabled, the code path uses upstream's normal `retrieve()` path, not the per-layer `retrieve_layer()` replay path.

That means:

- Non-layerwise: retrieve once, then copy to GPU in batch
- Layerwise: split each chunk again into per-layer replay flow

The current problem only appears in the latter.

So:

- `partial chunk` itself is not completely unreadable
- The actually unreliable condition is:
  - `partial chunk`
  - plus
  - `layerwise replay`

when those two conditions occur together in upstream behavior

## 5. The More Reasonable Current Root-Cause Judgment

The most reasonable current judgment is:

- Upstream `layerwise replay` is essentially designed around the happy path at chunk boundaries
- Partial tail chunks are allowed to be saved and allowed to be found by lookup
- But they are not stably supported in the current replay path

Do not keep prioritizing suspicion on these directions:

- `priority-fs` file format
- key/hash drift
- corruption in the partial tail file contents
- `cached_positions` sidecar as the primary cause

The directions that still deserve focus are:

- repo-local workaround
- do not modify site-packages
- guarantee correctness first

## 6. Confirmed Self-Heal Limitation

There is also a confirmed behavior limitation on the current layerwise path:

- The cache does not self-heal automatically

The behavior is:

- If after one warmup run, part of the layer files for one persisted chunk is manually deleted
- Then the same request is sent again
- The hit prefix becomes shorter
- That part is expected
- But this request does not automatically rewrite the missing layer files

The most reasonable explanation right now is:

1. On the layerwise retrieve side, only layer 0 of each chunk is used to decide whether "this chunk exists".
2. On the layerwise store side, only layer 0 of each chunk is also used to decide whether "this chunk already exists".
3. Therefore, if layer 0 of a chunk still exists but other layers are missing:
   - The retrieve side may still treat the chunk as "existing"
   - The store side will also treat the chunk as "already existing" and skip rewriting it
4. This leads to:
   - Once the hit prefix becomes shorter due to missing layers
   - Later requests do not opportunistically fill the missing layers back in
   - The cache does not automatically recover completeness

Relevant upstream code evidence:

- In `store_layer()`, when handling a layerwise chunk:
  - It first calls `key.split_layers(self.num_layers)`
  - But only checks `keys_multi_layer[0]`
  - If layer 0 exists, it skips the whole chunk directly
- In `retrieve_layer()`:
  - It also only checks `keys_multi_layer[0]`
  - Once layer 0 of a chunk misses, it stops matching further chunks immediately

So this is not just a simple "partial overlap conflict". The real problem is that the existence check granularity in the current layerwise path is too coarse:

- It is not per-layer
- It uses "layer 0 stands for the whole chunk"

For now, this is recorded as a confirmed limitation. If we want to fix it later, we still need to decide whether a repo-local hack is acceptable.

## 7. Confirmed Runtime Write-Disable Capability

We also confirmed one fact related to experiment control:

- There is no ready-made public "global runtime switch" that lets a user temporarily disable all backend writes
- But per-request temporary write disable is supported

More specifically:

1. Per-request temporary write disable is supported.
2. The upstream vLLM / LMCache adapter layer reads this from `request_configs`:
   - `lmcache.skip_save`
3. As long as a request carries:
   - `lmcache.skip_save=True`
   that request will not write cache to the backend.
4. There is currently no equally clean "global runtime switch"
   that can turn all writes on or off dynamically while the service is running.
5. Mechanisms like `LMCACHE_FORCE_SKIP_SAVE` are closer to startup-time configuration,
   not an official runtime interface for arbitrary switching.

So if a later experiment needs "temporarily do not write cache", the current recommendation is:

- Use per-request `lmcache.skip_save`

instead of relying on a global dynamic switch.

## 8. Confirmed Pause / Resume Capability

We also confirmed one fact related to chunk-boundary control:

- vLLM Async engine officially supports pause / resume
- But it is not a native chunk-aware interface

More specifically:

1. Async engine provides:
   - `pause_generation(mode="keep")`
   - `resume_generation()`
2. The semantics of this interface are closer to:
   - pause / resume the generation scheduler
   - while retaining the state needed for the in-flight request to continue
3. There is no officially exposed dedicated interface that directly says:
   - "run until the next LMCache chunk boundary and pause automatically"
4. But if the outer layer can already count chunk boundaries,
   then it can:
   - run generation first
   - call `pause` when the target chunk boundary is reached
   - and later call `resume`
5. So in terms of capability:
   - "chunk by chunk generate" is not an officially packaged mode
   - but it can be approximated through
     - outer-layer boundary counting
     - plus
     - official pause / resume

For now, we record that capability boundary as-is.

## 9. Limitation Of Pause / Resume For Save-Strategy Switching

We also confirmed a more specific limitation:

- `pause_generation(mode="keep")` / `resume_generation()` can preserve and continue the same in-flight request
- But we have not found a public interface that allows modifying that request's LMCache save strategy during the pause

More specifically:

1. Request-level save control such as `lmcache.skip_save` is extracted from
   `sampling_params.extra_args["kv_transfer_params"]`
   when the request is created.
2. Those `request_configs` enter the tracked state of the request.
3. `pause / resume` only continues running the same request.
4. There is currently no public API that supports changing the same request's
   `request_configs` / save strategy after pause and before resume.

So:

- If one request should be write-disabled from the beginning, set
  `lmcache.skip_save=True`
  when the request is created
- If you want to switch save strategy halfway through, the more realistic approach is:
  - end the current request
  - create a new request
  - give the new request a different save strategy

## 10. AsyncLLM Input Forms

We also confirmed:

- vLLM's AsyncLLM / Async engine input is not limited to strings
- It also supports directly passing token IDs

That means:

1. You can feed a normal string prompt
2. You can also feed tokenized input directly
3. So if we later need precise chunk-boundary control, driving it directly with token IDs will be more natural than converting them back into strings first

The minimal embedded server in the current repository still exposes only the simplest string-prompt usage,
but that is not the upper bound of the Async engine itself.

## 11. Current Recommendation

The current repository default recommendation is:

- Keep the embedded mainline at:
  - `save_decode_cache = on`
  - `save_unfull_chunk = off`
  - `layerwise = on`
  - default profile:
    [default_layerwise_unfull_off.yaml](/home/junhaoy/ServerlessLMCache/embedded_demo/configs/default_layerwise_unfull_off.yaml)

If we must turn `layerwise` back on in later work, the current better recommendation is:

1. Forbid partial chunk replay when `layerwise=on`
2. The repository already includes a fail-fast check for `layerwise=on + save_unfull_chunk=on`

The following is not recommended as the default right now:

```bash
LAYERWISE=1 SAVE_UNFULL_CHUNK=1
```

## 12. Recommended Reproduction Experiment

```bash
LMCACHE_CONFIG_FILE_PATH=embedded_demo/configs/non_layerwise_unfull_on.yaml \
bash embedded_demo/run_vllm_async_engine_priority_fs_server.sh
```

This configuration can be used as a comparison case to validate non-layerwise behavior with `save_unfull_chunk=on`.
