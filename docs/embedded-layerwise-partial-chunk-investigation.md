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

## 6. 当前已确认的 self-heal 限制

当前 layerwise 路径下，还有一个已经确认的行为限制：

- cache 不会自动 self-heal

具体表现是：

- 如果一次 warmup 后，人工删掉某个已落盘 chunk 的一部分 layer 文件
- 下一次同样请求进来时，命中前缀会缩短
- 这是预期内的
- 但这次请求不会自动把缺失的 layer 文件补写回去

当前最合理的原因判断是：

1. layerwise retrieve 侧只拿每个 chunk 的第 0 层作为“这个 chunk 是否存在”的判定依据。
2. layerwise store 侧也只拿每个 chunk 的第 0 层作为“这个 chunk 是否已经存在”的判定依据。
3. 因此如果一个 chunk 的第 0 层仍在，但其它 layer 缺失：
   - retrieve 侧仍可能把这个 chunk 当作“存在”
   - store 侧也会把这个 chunk 当作“已存在”，从而直接跳过重写
4. 这会导致：
   - prefix 命中一旦因为缺层而变短
   - 后续请求不会顺手把缺失 layer 补齐
   - cache 不会自动恢复完整

对应的上游代码证据：

- `store_layer()` 在处理 layerwise chunk 时：
  - 先 `key.split_layers(self.num_layers)`
  - 但只检查 `keys_multi_layer[0]`
  - 如果第 0 层存在，就直接跳过整个 chunk
- `retrieve_layer()` 也同样：
  - 只检查 `keys_multi_layer[0]`
  - 一旦某个 chunk 的第 0 层 miss，就直接停止继续向后匹配

因此，这不是简单的“部分 overlap 冲突”，而是当前 layerwise 路径的存在性判定粒度过粗：

- 它不是 per-layer 判定
- 而是“拿 layer 0 代表整个 chunk”

当前先把这个问题作为已确认限制记录下来。
后续如果要修，需要再决定是否接受 repo-local hack。

## 7. 当前已确认的运行时禁写能力

当前还确认了一个和实验控制相关的事实：

- user 无法通过一个现成的公开“全局 runtime 开关”
  临时禁止所有 backend 写入
- 但可以按 request 粒度临时禁止写入

更具体地说：

1. per-request 临时禁写是支持的。
2. 上游 vLLM / LMCache 适配层会读取 request_configs 里的：
   - `lmcache.skip_save`
3. 只要某次请求带了：
   - `lmcache.skip_save=True`
   这一请求就不会向 backend 写入 cache。
4. 当前没有一个同样干净的“全局 runtime 开关”
   可以在服务运行中随时打开/关闭所有写入。
5. 类似 `LMCACHE_FORCE_SKIP_SAVE` 这类机制更接近启动时配置，
   不是面向运行中随时切换的正式接口。

因此，如果后续实验需要“临时不写 cache”，当前更推荐：

- 使用 per-request 的 `lmcache.skip_save`

而不是依赖全局动态开关。

## 8. 当前已确认的 pause/resume 能力

当前还确认了一个和 chunk 边界控制相关的事实：

- vLLM Async engine 官方支持 pause / resume
- 但它不是原生的 chunk-aware 接口

更具体地说：

1. Async engine 提供：
   - `pause_generation(mode="keep")`
   - `resume_generation()`
2. 这套接口的语义更接近：
   - 暂停 / 恢复 generation scheduler
   - 并保留 in-flight request 继续运行所需的状态
3. 官方没有直接暴露一个：
   - “跑到下一个 LMCache chunk 边界自动暂停”
   - 这样的专用接口
4. 但如果 user 自己在外层已经能数到 chunk boundary，
   那么就可以：
   - 先运行生成
   - 在达到目标 chunk 边界时调用 pause
   - 之后再调用 resume
5. 因此，从能力上说：
   - “chunk by chunk generate” 不是官方直接给出的现成模式
   - 但可以通过
     - 外层边界计数
     - 加上
     - 官方 pause / resume
     - 来近似实现

当前先把这个能力边界记录下来。

## 9. pause/resume 与 save 策略切换的限制

当前还确认了一个更细的限制：

- `pause_generation(mode="keep")` / `resume_generation()` 可以保留并继续同一个
  in-flight request
- 但当前没有看到公开接口允许在 pause 期间再修改这个 request 的
  LMCache save 策略

更具体地说：

1. request 级 save 控制（例如 `lmcache.skip_save`）是从
   request 创建时的 `sampling_params.extra_args["kv_transfer_params"]`
   提取出来的。
2. 这些 request_configs 会进入 request 的跟踪状态。
3. pause / resume 只是让同一个 request 继续跑。
4. 当前没有公开 API 支持在 pause 后、resume 前再去修改
   同一个 request 的 request_configs / save 策略。

因此：

- 如果某次 request 从一开始就要禁写，应在创建 request 时就设置
  `lmcache.skip_save=True`
- 如果想中途切换 save 策略，更现实的方式是：
  - 结束当前 request
  - 再新建一个 request
  - 给新 request 带不同的 save 策略

## 10. AsyncLLM 输入形式

当前还确认：

- vLLM 的 AsyncLLM / Async engine 输入不只支持字符串
- 也支持直接传 token ids

也就是说：

1. 可以喂普通 string prompt
2. 也可以喂 tokenized input
3. 因此如果后续需要精确控制 chunk boundary，
   直接基于 token ids 驱动会比先转回字符串更自然

当前仓库里的 minimal embedded server 仍然只暴露了最简单的 string prompt 用法，
但这不是 Async engine 本身的能力上限。

## 11. 当前建议

当前仓库默认建议：

- embedded 主线继续保留：
  - `save_decode_cache = on`
  - `save_unfull_chunk = off`
  - `layerwise = on`
  - 默认 profile:
    [default_layerwise_unfull_off.yaml](/home/junhaoy/ServerlessLMCache/embedded_demo/configs/default_layerwise_unfull_off.yaml)

如果后续一定要重新打开 `layerwise`，当前更推荐：

1. `layerwise=on` 时禁止 partial chunk 参与 replay
2. 当前仓库已经对 `layerwise=on + save_unfull_chunk=on` 增加 fail-fast 检查

不建议当前默认使用：

```bash
LAYERWISE=1 SAVE_UNFULL_CHUNK=1
```

## 12. 推荐复现实验

```bash
LMCACHE_CONFIG_FILE_PATH=embedded_demo/configs/non_layerwise_unfull_on.yaml \
bash embedded_demo/run_vllm_async_engine_priority_fs_server.sh
```

这组配置可用于对照验证 `save_unfull_chunk=on` 的 non-layerwise 行为。
