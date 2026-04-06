from __future__ import annotations

from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from lmcache.logging import init_logger
from lmcache.utils import CacheEngineKey
from lmcache.v1.config import LMCacheEngineConfig
from lmcache.v1.memory_management import MemoryObj
from lmcache.v1.storage_backend.connector import ConnectorAdapter, ConnectorContext
from lmcache.v1.storage_backend.connector.base_connector import RemoteConnector
from lmcache.v1.storage_backend.connector.fs_connector import FSConnector
from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend

logger = init_logger(__name__)


def _get_required_query_param(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name)
    if not values or not values[0].strip():
        raise ValueError(
            f"priority-fs URL must include a non-empty '{name}' query parameter"
        )
    return unquote(values[0].strip())


def parse_priority_fs_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "priority-fs":
        raise ValueError(f"Unsupported scheme for priority-fs connector: {url}")

    query = parse_qs(parsed.query, keep_blank_values=True)
    read_path = _get_required_query_param(query, "read_path")
    write_path = _get_required_query_param(query, "write_path")
    return read_path, write_path


class PriorityFSConnector(RemoteConnector):
    """A thin wrapper that preserves the current B -> A fs semantics.

    Reads always check `read_path` first and then fall back to `write_path`.
    Writes always go to `write_path` only.
    """

    def __init__(
        self,
        read_path: str,
        write_path: str,
        loop,
        local_cpu_backend: LocalCPUBackend,
        config: Optional[LMCacheEngineConfig],
    ) -> None:
        super().__init__(local_cpu_backend.config, local_cpu_backend.metadata)
        self.read_path = read_path
        self.write_path = write_path
        self.read_connector = FSConnector(read_path, loop, local_cpu_backend, config)
        self.write_connector = FSConnector(write_path, loop, local_cpu_backend, config)
        logger.info(
            "Initialized PriorityFSConnector with read_path=%s write_path=%s",
            read_path,
            write_path,
        )

    async def exists(self, key: CacheEngineKey) -> bool:
        return await self.read_connector.exists(key) or await self.write_connector.exists(
            key
        )

    def exists_sync(self, key: CacheEngineKey) -> bool:
        return self.read_connector.exists_sync(key) or self.write_connector.exists_sync(
            key
        )

    async def get(self, key: CacheEngineKey) -> Optional[MemoryObj]:
        memory_obj = await self.read_connector.get(key)
        if memory_obj is not None:
            return memory_obj
        return await self.write_connector.get(key)

    async def put(self, key: CacheEngineKey, memory_obj: MemoryObj):
        await self.write_connector.put(key, memory_obj)

    async def list(self) -> List[str]:
        keys = await self.read_connector.list()
        keys.extend(await self.write_connector.list())
        return list(dict.fromkeys(keys))

    async def close(self):
        await self.read_connector.close()
        if self.write_connector is not self.read_connector:
            await self.write_connector.close()

    def remove_sync(self, key: CacheEngineKey) -> bool:
        removed_from_read = self.read_connector.remove_sync(key)
        removed_from_write = self.write_connector.remove_sync(key)
        return removed_from_read or removed_from_write

    def support_batched_contains(self) -> bool:
        return True

    def batched_contains(self, keys: List[CacheEngineKey]) -> int:
        hit_chunks = 0
        for key in keys:
            if self.exists_sync(key):
                hit_chunks += 1
                continue
            break
        return hit_chunks

    def __repr__(self) -> str:
        return (
            "PriorityFSConnector("
            f"read_path={self.read_path!r}, write_path={self.write_path!r})"
        )


class PriorityFSConnectorAdapter(ConnectorAdapter):
    def __init__(self) -> None:
        super().__init__("priority-fs://")

    def create_connector(self, context: ConnectorContext) -> RemoteConnector:
        read_path, write_path = parse_priority_fs_url(context.url)
        logger.info(
            "Creating PriorityFSConnector for read_path=%s write_path=%s",
            read_path,
            write_path,
        )
        return PriorityFSConnector(
            read_path=read_path,
            write_path=write_path,
            loop=context.loop,
            local_cpu_backend=context.local_cpu_backend,
            config=context.config,
        )
