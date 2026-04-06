# 当前方案说明

这份文档详细记录当前仓库里用于验证 `LMCache + vLLM` 的方案、设计理由、已知约束，以及我们在排查过程中确认下来的关键结论。

本文档描述的是“当前已经验证可运行”的方案，不是所有可能方案的总览。

补充说明：仓库当前后续工作的默认重心已经转到 `embedded priority-fs` 路线。

- `embedded_demo/` 是现在优先继续推进的主线
- `LMCache MP` 路径仍然保留，主要用于历史对照和回归验证
- 如果没有特别说明，默认应优先基于 embedded 脚本、server 和 helper 继续工作
- 当前 embedded 默认也已经切到：
  - `save_decode_cache = on`
  - `save_unfull_chunk = on`

## 1. 我们想做到什么

我们当前希望实现并验证的，不只是“LMCache 能接上 vLLM”，而是一个更具体的缓存语义：

- 永远写到目录 `A`
- 总是先尝试从目录 `B` 读取
- `B` miss 再回退到 `A`

当前约定：

- `A = .kvcache`
- `B = .kvcache_remote`

也就是说，我们想模拟的是一种“写入路径”和“优先读取路径”分离的缓存体系：

- `A` 更像主写入目录
- `B` 更像优先读取目录

在这个基础上，我们还想验证几件事：

- 能否在单机上模拟“另一台机器读取共享目录”
- 能否区分 `LMCache` 自己的 `L1` 命中和磁盘层 `L2` 命中
- 在服务重启后，cache key 是否仍然稳定，从而真正命中已有落盘内容
- 能否不依赖额外控制面、数据库或独立索引服务，而是直接依靠分布式文件系统上的目录项扫描和文件名编码完成查找

这最后一点非常重要。我们当前不是想做一个“必须有单独索引服务/元数据服务”才能工作的缓存系统，而是希望：

- 后端直接把 KV chunk 落到共享或分布式文件系统
- 读取时直接通过文件系统 entry scan / exists / 文件名解码来完成查找
- 不额外维护一套独立控制面来保存 chunk 索引
- 这样另一台机器只要挂载到同一份共享目录，就有机会直接读到已有 cache

## 2. 我们是怎么做到的

为了实现上面的语义，我们最终采用的是：

- `vLLM + LMCacheMPConnector`
- `LMCache MP server`
- 多 adapter + 自定义 store policy

而不是单进程内嵌的 `LMCacheConnectorV1`。

原因很简单：

- 单进程内嵌模式更适合“最简单集成”
- 但不适合清晰表达“只写 A / 读优先 B / 再回退 A”
- 这个语义更适合 `LMCache MP` 的多 adapter 架构
- 同时我们希望底层尽量直接依赖共享文件系统，而不是引入额外控制面或集中索引服务

具体做法分成 3 步：

### 2.1 把 adapter 顺序设成 `B -> A`

启动 `LMCache MP server` 时，adapter 顺序固定为：

1. `B = .kvcache_remote`
2. `A = .kvcache`

这一步的目的是让默认读取逻辑总是先看 `B`。

### 2.2 保持默认 `PrefetchPolicy=default`

`LMCache` 默认的 `PrefetchPolicy=default` 会对每个 key 选择“第一个命中的 adapter”。

因此在 `B -> A` 这个顺序下，自然得到：

- 先查 `B`
- `B` miss 再查 `A`

### 2.3 新增 `write_last` 策略

我们新增了一个自定义 `StorePolicy=write_last`：

- [demo/write_last_store_policy.py](/home/junhaoy/ServerlessLMCache/demo/write_last_store_policy.py)

它的行为是：

- 所有 key 只写到最后一个 adapter

由于当前 adapter 顺序是 `[B, A]`，最后一个 adapter 就是 `A`。

因此最终实现出的整体语义就是：

- 只写 `A`
- 读优先 `B`
- `B` miss 再回退 `A`

### 2.4 底层尽量不依赖额外控制面

当前默认优先使用 `fs` adapter，还有一个非常现实的原因：

- 它更接近我们想验证的“直接依赖共享文件系统”模式
- cache object 直接以文件形式落盘
- 查找逻辑依赖文件系统本身，而不是依赖外部索引服务

也就是说，我们现在刻意追求的是一种更朴素的后端形态：

- 数据存在分布式文件系统里
- 另一台机器只要挂到同一个目录，就能基于同样的 key 去查
- 不是先访问某个中心控制面，再由控制面告诉它 chunk 在哪

## 3. 当前架构

当前架构由 3 部分组成：

1. `LMCache MP server`
2. `vLLM API server`
3. 一个用于发请求的最小客户端脚本

对应文件：

- [demo/write_last_store_policy.py](/home/junhaoy/ServerlessLMCache/demo/write_last_store_policy.py)
- [demo/run_lmcache_mp_server.py](/home/junhaoy/ServerlessLMCache/demo/run_lmcache_mp_server.py)
- [demo/request_demo.py](/home/junhaoy/ServerlessLMCache/demo/request_demo.py)
- [scripts/run_lmcache_mp_priority_demo.sh](/home/junhaoy/ServerlessLMCache/scripts/run_lmcache_mp_priority_demo.sh)
- [scripts/run_vllm_lmcache_mp_demo.sh](/home/junhaoy/ServerlessLMCache/scripts/run_vllm_lmcache_mp_demo.sh)

## 4. A/B 读写策略

### 4.1 Adapter 顺序

启动 `LMCache MP server` 时，adapter 顺序固定为：

1. `B = .kvcache_remote`
2. `A = .kvcache`

也就是说，`B` 总是排在第一个。

### 4.2 PrefetchPolicy

当前使用 `LMCache` 默认的 `PrefetchPolicy=default`。

它的行为是：

- 对每个 key，选择“第一个命中的 adapter”

因此在我们这个顺序下：

- 先查 `B`
- `B` miss 再查 `A`

### 4.3 StorePolicy

我们新增了一个自定义策略 `write_last`：

- [demo/write_last_store_policy.py](/home/junhaoy/ServerlessLMCache/demo/write_last_store_policy.py)

它的行为是：

- 所有 key 只写到最后一个 adapter

由于当前 adapter 顺序是 `[B, A]`，所以最后一个就是 `A`。

最终效果就是：

- 只写 `A`
- 读优先 `B`
- 再回退 `A`

## 5. 为什么默认先用 fs adapter

当前默认脚本：

- [scripts/run_lmcache_mp_priority_demo.sh](/home/junhaoy/ServerlessLMCache/scripts/run_lmcache_mp_priority_demo.sh)

默认使用 `fs` adapter，而不是直接默认 `GDS`。

原因不是功能做不到，而是为了把“语义验证”和“GDS/NIXL 环境兼容性”拆开：

- `fs` adapter 最容易验证 A/B 读写语义
- `GDS` / `nixl_store` 更依赖底层环境
- `fs` adapter 也最接近“无额外控制面、直接靠共享文件系统工作的后端”这个目标

在单机模拟和策略排查阶段，`fs` 更稳定。

如果后续要切回 GDS 路径，可以使用：

- [scripts/run_lmcache_mp_priority_gds_demo.sh](/home/junhaoy/ServerlessLMCache/scripts/run_lmcache_mp_priority_gds_demo.sh)

## 6. 当前关键脚本

### 6.1 启动 LMCache MP

```bash
bash scripts/run_lmcache_mp_priority_demo.sh
```

这个脚本会：

- 固定 adapter 顺序为 `B -> A`
- 使用 `write_last`
- 默认关闭 observability，避免额外依赖影响 demo
- 固定 `PYTHONHASHSEED=0`

### 6.2 启动 vLLM

```bash
bash scripts/run_vllm_lmcache_mp_demo.sh
```

这个脚本会：

- 使用 `LMCacheMPConnector`
- 自动设置 `--disable-hybrid-kv-cache-manager`
- 默认 `GPU_MEMORY_UTILIZATION=0.5`
- 默认 `PYTHONHASHSEED=0`
- 优先解析本地 Hugging Face snapshot，并在找到本地 snapshot 时设置 `HF_HUB_OFFLINE=1`

### 6.3 发请求

```bash
python demo/request_demo.py
```

为了更干净地验证“重启后的第一次请求”，推荐：

```bash
python demo/request_demo.py --requests 1
```

## 7. 请求脚本为什么改成 completions

当前 [demo/request_demo.py](/home/junhaoy/ServerlessLMCache/demo/request_demo.py) 使用的是普通 `completions`，不是 `chat.completions`。

原因是我们已经确认：

- `chat.completions` 容易受到 chat template 包装影响
- 在做“跨重启精确 prefix-key 命中验证”时，不够稳定

改成 `completions` 后：

- prompt 更直接
- token 序列更容易保持稳定
- 更适合做 LMCache prefix key 验证

## 8. 排查过程中确认的关键事实

### 8.1 不是没落盘

当前 `.kvcache` 确实会写出真实 `.data` 文件。

例如我们已经确认过：

- `.kvcache` 中存在数十个 `.data` 文件
- 目录体积约 `1.6G`

而 `.kvcache_remote` 默认为空，这是符合当前策略的：

- 因为只写 `A`
- 不写 `B`

### 8.1.1 当前后端是“文件系统直接查找”思路

就当前默认 `fs` adapter 而言，我们已经确认它属于“直接依赖文件系统”的设计：

- `ObjectKey` 会被编码进文件名
- KV chunk 直接以 `.data` 文件形式落盘
- 不额外依赖单独的中心索引服务

因此它和我们希望验证的目标是一致的：

- 后端尽量简单
- 共享目录本身就是共享介质
- 新实例/另一台机器理论上只要看到同一份目录，就能尝试复用这些文件

### 8.1.2 `kv_rank` 是什么

当前落盘文件名除了 `model_name` 和 `chunk_hash`，还包含一个 `kv_rank`。

它的作用不是“请求 ID”，而是：

- 标识这份 KV cache 属于哪个并行切片
- 避免不同 worker / 不同并行配置下的 KV 数据互相混用

在 `LMCache` 里，一个对象 key 主要由三部分组成：

- `chunk_hash`
- `model_name`
- `kv_rank`

对应代码在：

- [api.py](/home/junhaoy/ServerlessLMCache/.venv/lib/python3.12/site-packages/lmcache/v1/distributed/api.py)

`kv_rank` 会由下面这些信息打包计算出来：

- `world_size`
- `global_rank`
- `local_world_size`
- `local_rank`

在我们当前这套单卡、单 worker 配置下，它基本固定成：

```text
0x01000100
```

所以在当前 demo 里，可以先把它理解成：

- 当前这个 worker 的 KV cache 命名空间编号

在更复杂的 TP/PP 并行场景下，不同 `kv_rank` 的文件不能直接互相当成同一份 cache 使用。

### 8.2 不是 GPU 架构没解决

一开始我们遇到：

- `CUDA error: no kernel image is available for execution on the device`

根因是：

- 官方 wheel 里的 `lmcache.c_ops` 没有为你的 `RTX 5060 Ti` 编进 `sm_120`

后来我们已经：

- 从源码构建了新的 `lmcache` wheel
- 替换到了当前 `.venv`
- 并确认 `c_ops` 现在包含 `sm_120`

当前环境里的版本是：

- `lmcache 0.4.3.dev82`

### 8.3 不是 key mismatch 本身

我们给 `LMCache MP` 加了运行时 debug：

- `HASH_DEBUG lookup ...`
- `HASH_DEBUG store ...`

这一步确认过：

- 在同一轮请求内，`lookup` 和 `store` 用到的 chunk hash 是一致的

也就是说，当前同一轮请求的写读键空间是一致的。

### 8.4 真正导致“跨重启不命中”的核心原因

根因是 `vllm` 的 `NONE_HASH` 初始化方式。

在当前 `vllm` 里：

- 如果没有设置 `PYTHONHASHSEED`
- `NONE_HASH` 会用随机种子初始化

对应代码：

- [kv_cache_utils.py](/home/junhaoy/ServerlessLMCache/.venv/lib/python3.12/site-packages/vllm/v1/core/kv_cache_utils.py)

这意味着：

- 相同 prompt
- 在不同进程重启后
- 会得到不同的 rolling prefix hash

于是就出现：

- 盘上已经有 `.data`
- 但重启后 lookup 还是 miss

解决方式就是固定：

```bash
PYTHONHASHSEED=0
```

当前两个启动脚本都已经默认固定了这个值。

## 9. 为什么日志里会看到 “14 L1, 0 L2”

这句日志来自 `LMCache MP` 自己的 storage manager：

- [storage_manager.py](/home/junhaoy/ServerlessLMCache/.venv/lib/python3.12/site-packages/lmcache/v1/distributed/storage_manager.py)

它的含义是：

- 当前 LMCache 自己内部命中了多少 `L1`
- 当前 LMCache 自己内部命中了多少 `L2`

这和 `vllm` 里打印的：

- `Prefix cache hit rate`
- `External prefix cache hit rate`

不是同一个口径。

也就是说：

- `LMCache` 的 `L1/L2` 是 LMCache 自己内部层级
- `vllm` 的 `External prefix cache hit rate` 是 scheduler 看到的 connector external token 统计

两者不能直接一一对应。

## 10. 正确的验证方式

### 10.1 验证写盘

```bash
python demo/request_demo.py --requests 1
find .kvcache -maxdepth 1 -type f -name '*.data'
```

如果写盘正常，应当在 `.kvcache` 看到 `.data` 文件。

### 10.2 验证“重启后第一次请求”

不要一次发两次请求去看重启效果，否则第二次请求很容易直接命中 LMCache L1。

正确流程：

1. 启动 `LMCache MP`
2. 启动 `vllm`
3. 发一次请求
4. 停掉 `LMCache MP`
5. 停掉 `vllm`
6. 重新启动两者
7. 再发一次请求

推荐命令：

```bash
python demo/request_demo.py --requests 1
```

### 10.3 如何判断是不是走到了磁盘层

如果想看是否真的用了磁盘层 `L2`，重点看 `LMCache` 自己的日志：

- 理想情况应更接近 `0 L1, N L2`

如果你看到：

- `14 L1, 0 L2`

那说明当前命中的是 LMCache 进程内的 L1，而不是磁盘层 L2。

## 11. 当前已知局限

### 11.1 当前 demo 主要是单机模拟

虽然语义上是在模拟：

- 机器 1 写 `A`
- 机器 2 读 `B`，再回退 `A`

但当前仓库里的主要验证仍然是在单机上完成的。

### 11.2 fs adapter 适合先验证语义

`fs` adapter 适合做：

- A/B 策略验证
- 单机模拟
- 共享目录验证

但如果后续要追求更接近生产的高性能路径，还是应该继续看：

- `GDS`
- `nixl_store`
- 更真实的共享存储环境

### 11.3 版本不是主要问题，但 GPU 架构支持是关键

我们已经确认过：

- 当前问题核心不在 `vllm 0.19.0`
- 核心在 `LMCache` wheel 是否包含 `sm_120`

所以后续如果换环境，最先要确认的是：

- `lmcache.c_ops` 是否真的编到了目标 GPU 架构

## 12. 当前推荐工作流

如果只是继续沿用当前方案做验证，推荐始终用下面这组命令：

### 第一次写入

```bash
bash scripts/run_lmcache_mp_priority_demo.sh
bash scripts/run_vllm_lmcache_mp_demo.sh
python demo/request_demo.py --requests 1
```

### 重启后验证

```bash
bash scripts/run_lmcache_mp_priority_demo.sh
bash scripts/run_vllm_lmcache_mp_demo.sh
python demo/request_demo.py --requests 1
```

在每轮之间，需要确保旧进程已经完全退出。

## 13. 结论

当前这套方案已经确认了几件重要事情：

- A/B 读写策略本身已经接好
- `LMCache MP` 已经能正常写盘
- `RTX 5060 Ti` 的 `sm_120` 兼容性问题已经通过本地编译解决
- `PYTHONHASHSEED` 不固定会导致跨重启 key 漂移，这已经修复

如果后续还要继续排查“为什么某一轮没有走到 L2”，优先关注：

- 这是不是重启后的第一次请求
- `LMCache` 日志里是 `L1` 命中还是 `L2` 命中
- 本轮 `HASH_DEBUG lookup` 和上一轮落盘 hash 是否一致

## 14. 可选 embedded priority-fs 路径

仓库里现在额外提供了一条“不要独立 `LMCache MP` 进程”的可选路径：

- 启动脚本：
  - [run_vllm_lmcache_embedded_priority_fs_demo.sh](/home/junhaoy/ServerlessLMCache/embedded_demo/run_vllm_lmcache_embedded_priority_fs_demo.sh)
- 自定义 connector 插件：
  - [priority_fs_adapter.py](/home/junhaoy/ServerlessLMCache/embedded_demo/priority_fs_adapter.py)

这条路径的设计目标是：

- 仍然使用 `fs` backend 语义
- 不修改已安装的 `lmcache` 包源码
- 通过 LMCache 已暴露的 `remote connector plugin` 扩展点，在当前仓库内实现
- 继续保持：
  - 读优先 `B = .kvcache_remote`
  - `B` miss 再回退到 `A = .kvcache`
  - 只写 `A`
- 当前默认也允许：
  - decode cache 写入
  - partial / unfull chunk 写入

实现方式不是 hack 文件系统 API，而是在 embedded 路径里复用两个官方 `FSConnector`：

- 一个只负责读优先路径 `B`
- 一个负责回退读取和唯一写入路径 `A`

这里还有一个很容易混淆、但实际排障很重要的点：

- embedded 路径落盘文件名遵循 `FSConnector + CacheEngineKey` 的 schema：
  - `<model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>.data`
- 如果启用 `layerwise`，则会改成 `LayerCacheEngineKey` schema：
  - `<model_name>@<world_size>@<worker_id>@<chunk_hash>@<dtype>@<layer_id>.data`
- 这和 `LMCache MP` 默认 `fs` adapter 的 schema 不同：
  - `<model_name>@<kv_rank_hex>@<chunk_hash_hex>.data`
- 因此不要拿 MP 路径下的文件名推导脚本，直接去预测 embedded 路径的 `.kvcache` / `.kvcache_remote` 文件名

当前默认 `save_unfull_chunk=on` 还意味着：

- 最后一个不满 chunk 的尾块也可能单独落盘
- 后续 decode 继续增长后，磁盘上可能同时保留“partial 版本”和“更长版本”的重叠文件
- 这些文件不是 append 关系，而是不同 key / 不同文件并存
- 这正是我们当前愿意接受、并希望保留的语义，因为它让包含 prefill + decode 的完整上下文更容易直接按文件名推导

仓库里现在为 embedded 路径单独提供了一组对应 helper：

- [prompt_cache_files.py](/home/junhaoy/ServerlessLMCache/embedded_demo/cache_files/prompt_cache_files.py)
- [list_prompt_cache_files.py](/home/junhaoy/ServerlessLMCache/embedded_demo/cache_files/list_prompt_cache_files.py)
  - 支持 `--layerwise`
  - 必要时支持 `--num-layers`
  - 默认按 `save_unfull_chunk=on` 计算
  - 如需回到旧行为可加 `--no-save-unfull-chunk`

当前默认验证路径已经切换到 `embedded priority-fs`；`LMCache MP` 方案保留为历史对照路径。

如果希望做成“常驻 Python server”，而不是 one-shot 脚本，还可以使用：

- [run_vllm_async_engine_priority_fs_server.py](/home/junhaoy/ServerlessLMCache/embedded_demo/run_vllm_async_engine_priority_fs_server.py)
- 推荐启动脚本：
  - [run_vllm_async_engine_priority_fs_server.sh](/home/junhaoy/ServerlessLMCache/embedded_demo/run_vllm_async_engine_priority_fs_server.sh)

这个脚本会：

- 在 Python 里直接创建 `AsyncLLMEngine`
- 暴露最小 OpenAI-compatible 接口：
  - `/health`
  - `/v1/models`
  - `/v1/completions`
- 允许继续复用现有 [demo/request_demo.py](/home/junhaoy/ServerlessLMCache/demo/request_demo.py)
- 也提供一份 embedded 专用请求脚本：
  - [request_demo.py](/home/junhaoy/ServerlessLMCache/embedded_demo/request_demo.py)

这里有一个很重要的实现细节：

- `PYTHONHASHSEED=0` 仍然必须在 Python 解释器启动前设置
- 因此现在推荐统一通过 [run_vllm_async_engine_priority_fs_server.sh](/home/junhaoy/ServerlessLMCache/embedded_demo/run_vllm_async_engine_priority_fs_server.sh) 启动
- Python 脚本本身只做 fail-fast 检查，不再自行 `exec`
- 如果直接裸跑 Python 脚本而没先设置 `PYTHONHASHSEED=0`，它会立即报错提醒

这个 server 入口现在也支持两个与当前实验密切相关的开关：

- `--layerwise`
- `--save-decode-cache`
- `--save-unfull-chunk`

如果通过 shell 包装脚本启动，也可以改用环境变量：

- `LAYERWISE=1`
- `SAVE_DECODE_CACHE=1`
- `SAVE_UNFULL_CHUNK=1`

例如：

```bash
LAYERWISE=1 SAVE_DECODE_CACHE=1 SAVE_UNFULL_CHUNK=1 \
  bash embedded_demo/run_vllm_async_engine_priority_fs_server.sh
```
