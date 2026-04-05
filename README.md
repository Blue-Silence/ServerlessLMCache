# LMCache + vLLM Demo

这份仓库现在同时保留两种 demo：

- 单进程内嵌版：`vLLM + LMCacheConnectorV1`
- 多进程优先级版：`vLLM + LMCacheMPConnector`

更详细的当前实现说明见：

- [docs/current-approach.md](/home/junhaoy/ServerlessLMCache/docs/current-approach.md)

如果你要的是下面这个语义：

- 永远写到目录 `A`
- 总是先尝试从目录 `B` 读取
- `B` miss 再回退到 `A`

那请使用下面的多进程优先级版。

## 1. A/B 语义说明

这里约定：

- `A = .kvcache`
- `B = .kvcache_remote`

为了实现 `写 A / 读优先 B`，demo 做了两件事：

1. 启动 LMCache MP server 时，把 adapter 顺序固定成 `B` 在前、`A` 在后
2. 注册一个自定义 `StorePolicy=write_last`，只把数据写到最后一个 adapter，也就是 `A`

读取侧继续用 LMCache 默认的 `PrefetchPolicy=default`，它会优先选第一个命中的 adapter，所以行为就是：

- 先读 `B`
- `B` 没有再读 `A`
- 只写 `A`

## 2. 启动 LMCache MP Server

最简单的本地验证方式是先用 `fs` adapter：

```bash
cd /home/junhaoy/ServerlessLMCache
bash scripts/run_lmcache_mp_priority_demo.sh
```

如果你想尝试 GDS 风格的 L2 adapter：

```bash
cd /home/junhaoy/ServerlessLMCache
bash scripts/run_lmcache_mp_priority_gds_demo.sh
```

这条 GDS 脚本底层会切到 `nixl_store` adapter，并把两个目录都作为独立 adapter 挂进去。

常用环境变量：

```bash
READ_FIRST_DIR=/home/junhaoy/ServerlessLMCache/.kvcache_remote
WRITE_DIR=/home/junhaoy/ServerlessLMCache/.kvcache
SERVER_HOST=127.0.0.1
SERVER_PORT=5555
L1_SIZE_GB=4
CHUNK_SIZE=256
```

GDS/NIXL 额外环境变量：

```bash
NIXL_BACKEND=GDS
NIXL_POOL_SIZE=8
USE_DIRECT_IO=true
```

## 3. 启动 vLLM

另开一个终端：

```bash
cd /home/junhaoy/ServerlessLMCache
bash scripts/run_vllm_lmcache_mp_demo.sh
```

默认模型是 `Qwen/Qwen3-0.6B`。也可以换成别的模型：

```bash
bash scripts/run_vllm_lmcache_mp_demo.sh meta-llama/Llama-3.2-1B-Instruct
```

这个脚本会使用：

```json
{
  "kv_connector": "LMCacheMPConnector",
  "kv_role": "kv_both",
  "kv_connector_extra_config": {
    "lmcache.mp.host": "tcp://127.0.0.1",
    "lmcache.mp.port": 5555
  }
}
```

并自动加上：

```bash
--disable-hybrid-kv-cache-manager
```

因为这是你当前本地 `vllm 0.19.0` 的 `LMCacheMPConnector` 要求。

默认还会使用：

```bash
GPU_MEMORY_UTILIZATION=0.5
PYTHONHASHSEED=0
```

这样对 0.6B 这种小模型更稳，不容易因为显存余量不足直接启动失败；同时固定 `PYTHONHASHSEED` 可以让重启后的 LMCache chunk hash 保持稳定，避免 external cache key 漂移。

## 4. 发送请求

再开一个终端：

```bash
cd /home/junhaoy/ServerlessLMCache
source .venv/bin/activate
python demo/request_demo.py
```

这个脚本会发两次共享长前缀请求，方便你观察前缀复用。

## 5. 相关文件

- `demo/write_last_store_policy.py`
- `demo/run_lmcache_mp_server.py`
- `scripts/run_lmcache_mp_priority_demo.sh`
- `scripts/run_lmcache_mp_priority_gds_demo.sh`
- `scripts/run_vllm_lmcache_mp_demo.sh`

## 6. 说明

- `scripts/run_lmcache_mp_priority_demo.sh` 默认用 `fs` adapter，最容易本地验证 `写 A / 读优先 B` 这个语义
- `scripts/run_lmcache_mp_priority_gds_demo.sh` 会切到 `nixl_store + GDS`，是否能实际运行取决于你的 NIXL / GDS 环境是否已准备好
- 旧的单进程内嵌版脚本仍然保留在仓库里，适合只验证最小 LMCache 集成
