# embedded priority-fs: layerwise 与 partial chunk 调查结论

这份文档专门记录当前 `embedded priority-fs` 主线下，
`LAYERWISE=1` 与 `SAVE_UNFULL_CHUNK=1` 组合相关的调查结论。

目标不是回顾全部历史，而是把当前已经收敛下来的判断写清楚，
避免后续继续在错误方向上浪费时间。

## 1. 结论先行

当前仓库默认应视为：

- `layerwise` 与 `partial / unfull chunk` 不能同时启用
- 更准确地说：
  - `LAYERWISE=1 SAVE_UNFULL_CHUNK=1` 当前不可靠
  - `LAYERWISE=1 SAVE_UNFULL_CHUNK=0` 当前可用
  - `layerwise=off` 当前可用

如果需要 correctness，当前推荐策略是二选一：

- 要么保持 `layerwise=off`
- 要么在 `layerwise=on` 时禁止 partial chunk 参与 replay

当前不建议把下面这个组合作为默认主线配置：

```bash
LAYERWISE=1 SAVE_UNFULL_CHUNK=1
```

## 2. 当前最关键的已确认事实

以下结论已经基本确认：

1. 当前坏的不是 `priority-fs` 文件层 I/O。
2. 对最后一个 partial tail chunk，`put/get` 的：
   - `key`
   - `len`
   - `fmt`
   - `shape`
   - `crc32`
   都能对上。
3. 当前复现里，tail chunk 落盘格式是：
   - `fmt=MemoryFormat.KV_T2D`
   - `shape=[74, 2, 1024]`
   - 这次请求总命中前缀是 `3658 = 14 * 256 + 74`
4. 重启后第二次请求日志明确出现：
   - `LMCache hit tokens: 3658`
   - `need to load: 3657`
5. `LAYERWISE=1 SAVE_UNFULL_CHUNK=0` 正常。
6. `layerwise=off` 正常。
7. 之前做过一个 repo-local 诊断性 workaround：
   - 在 `start_load_kv()` 后强制把 layerwise replay generator 全部跑完
   - 它能让同一份 cache、同一请求恢复正确输出
   - 但这个诊断 patch 现已从仓库移除，不再作为长期方案保留

这组证据共同说明：

- 不是 tail 文件本身坏了
- 不是 key/hash 不一致
- 不是 A/B priority-fs 读写语义错了
- 真正不可靠的是上游 `layerwise replay` 在 partial tail chunk 存在时的行为

## 3. 为什么 `SAVE_UNFULL_CHUNK=0` 正常，而 `=1` 不正常

这是当前最容易反复混淆的点。

以当前复现数字为例：

- `chunk_size = 256`
- `3658 = 14 * 256 + 74`

因此这次 prefix 可以拆成：

- 前 14 个 full chunks，共 `3584` tokens
- 最后 1 个 partial tail chunk，共 `74` tokens

两种配置的真实差别是：

### 3.1 `SAVE_UNFULL_CHUNK=0`

- 只保存前 14 个 full chunks
- 最后 `74` 个 token 不会参与 cache replay
- 重启后，系统最多只会把 `3584` 当作可复用前缀
- 最后 `74` 个 token 会重新计算

因此这条路径根本不会把 partial tail chunk 带进 `layerwise replay`。

### 3.2 `SAVE_UNFULL_CHUNK=1`

- 最后 `74` 个 token 也会落盘
- lookup 也会把这段 tail 计入命中
- 因此日志会出现：
  - `LMCache hit tokens: 3658`
  - `need to load: 3657`

这时系统不再重算最后 `74` 个 token 对应的 KV，
而是要求 `layerwise replay` 把这段 partial tail 也恢复回来。

当前问题正是在这里暴露出来：

- full chunks 的 replay 当前可用
- partial tail chunk 一旦真的进入 `layerwise replay`
- 输出就会变坏

所以表面上看是“只差了 74 个 token”，
本质上是：

- `SAVE_UNFULL_CHUNK=0` 把 bug 绕开了
- `SAVE_UNFULL_CHUNK=1` 把 bug 暴露出来了

## 4. 为什么不开 `layerwise` 就没问题

不开 `layerwise` 时，走的是上游的普通 `retrieve()` 路径，
不是 `retrieve_layer()` 的逐层 replay 路径。

也就是说：

- non-layerwise：一次性 retrieve，再 batched copy 到 GPU
- layerwise：把每个 chunk 再拆成逐层 replay 的流程

当前问题只出现在后者。

因此：

- `partial chunk` 本身并不是完全不可读
- 真正不可靠的是：
  - `partial chunk`
  - 加上
  - `layerwise replay`

这两个条件同时出现时的上游行为

## 5. 当前更合理的根因判断

当前最合理的判断是：

- 上游 `layerwise replay` 这条路径基本是按 chunk 边界的 happy path 设计的
- partial tail chunk 虽然允许被保存、也允许被 lookup 到
- 但它并没有在当前这条 replay 路径里被稳定支持

不要再优先怀疑下面这些方向：

- `priority-fs` 文件格式
- key/hash 漂移
- partial tail 文件内容损坏
- `cached_positions` sidecar 是主因

当前最应聚焦的方向仍然是：

- repo-local workaround
- 不改 site-packages
- 先保证 correctness

## 6. 当前建议

当前仓库默认建议：

- embedded 主线继续保留：
  - `save_decode_cache = on`
  - `save_unfull_chunk = off`
  - `layerwise = on`

如果后续一定要重新打开 `layerwise`，当前更推荐：

1. `layerwise=on` 时禁止 partial chunk 参与 replay
2. 当前仓库已经对 `layerwise=on + save_unfull_chunk=on` 增加 fail-fast 检查

不建议当前默认使用：

```bash
LAYERWISE=1 SAVE_UNFULL_CHUNK=1
```

## 7. 推荐复现实验

```bash
LAYERWISE=1 SAVE_DECODE_CACHE=0 SAVE_UNFULL_CHUNK=1 \
bash embedded_demo/run_vllm_async_engine_priority_fs_server.sh
```

当前这个组合会被启动脚本直接拒绝，用来明确暴露不支持的配置。
