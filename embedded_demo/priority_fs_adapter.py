from __future__ import annotations

import aiofiles
import aiofiles.os
import struct
import torch

from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from lmcache.logging import init_logger
from lmcache.utils import CacheEngineKey
from lmcache.v1.config import LMCacheEngineConfig
from lmcache.v1.memory_management import MemoryObj
from lmcache.v1.pin_monitor import PinMonitor
from lmcache.v1.protocol import RemoteMetadata
from lmcache.v1.storage_backend.connector import ConnectorAdapter, ConnectorContext
from lmcache.v1.storage_backend.connector.base_connector import RemoteConnector
from lmcache.v1.storage_backend.connector.fs_connector import FSConnector
from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend

logger = init_logger(__name__)
REMOTE_METADATA_SHAPE_DIMS = 4


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


def _pad_shape_for_remote_metadata(shape: torch.Size) -> torch.Size:
    if len(shape) > REMOTE_METADATA_SHAPE_DIMS:
        raise ValueError(
            f"Unsupported shape rank {len(shape)} for remote metadata: {shape}"
        )
    if len(shape) == REMOTE_METADATA_SHAPE_DIMS:
        return shape
    padded = list(shape) + [0] * (REMOTE_METADATA_SHAPE_DIMS - len(shape))
    return torch.Size(padded)


def _restore_shape_from_remote_metadata(shape: torch.Size) -> torch.Size:
    actual_shape = []
    for dim in shape:
        if dim == 0 and actual_shape:
            break
        actual_shape.append(dim)
    return torch.Size(actual_shape)


def _serialize_cached_positions(cached_positions: Optional[torch.Tensor]) -> bytes:
    if cached_positions is None:
        return struct.pack("<i", -1)
    positions = cached_positions.to(dtype=torch.int64, device="cpu").tolist()
    return struct.pack(f"<i{len(positions)}q", len(positions), *positions)


def _deserialize_cached_positions(data: bytes) -> Optional[torch.Tensor]:
    if len(data) < 4:
        raise ValueError("cached_positions sidecar is too short")
    count = struct.unpack_from("<i", data, 0)[0]
    if count < 0:
        return None
    expected_size = struct.calcsize(f"<i{count}q")
    if len(data) != expected_size:
        raise ValueError(
            f"cached_positions sidecar size mismatch: expected {expected_size}, got {len(data)}"
        )
    if count == 0:
        return torch.empty(0, dtype=torch.int64)
    values = struct.unpack_from(f"<i{count}q", data, 0)[1:]
    return torch.tensor(values, dtype=torch.int64)


class LayerwiseAwareFSConnector(FSConnector):
    """FSConnector variant that tolerates layerwise 3D memory objects."""

    @staticmethod
    def _logical_byte_view(memory_obj: MemoryObj) -> memoryview:
        return memory_obj.byte_array[: memory_obj.get_size()]

    def _get_cached_positions_path(self, key: CacheEngineKey):
        file_path = self._get_file_path(key)
        return file_path.with_suffix(".pos")

    async def get(self, key: CacheEngineKey) -> Optional[MemoryObj]:
        if not self.save_chunk_meta:
            return await super().get(key)

        file_path = self._get_file_path(key)
        memory_obj = None
        try:
            async with aiofiles.open(file_path, "rb") as f:
                md_buffer = bytearray(self.remote_metadata_bytes)
                num_read = await f.readinto(md_buffer)
                if num_read != len(md_buffer):
                    raise RuntimeError(
                        f"Partial read meta {len(md_buffer)} got {num_read}"
                    )

                metadata = RemoteMetadata.deserialize(md_buffer)
                restored_shapes = [
                    _restore_shape_from_remote_metadata(shape)
                    for shape in metadata.shapes
                ]
                allocated = self.local_cpu_backend.batched_allocate(
                    restored_shapes,
                    metadata.dtypes,
                    batch_size=1,
                    fmt=metadata.fmt,
                )
                if allocated is None:
                    logger.debug("Memory allocation failed during async disk load.")
                    return None
                memory_obj = allocated[0]

                logical_buffer = self._logical_byte_view(memory_obj)
                num_read = await f.readinto(logical_buffer)
                if num_read != metadata.length:
                    raise RuntimeError(
                        f"Partial read data {metadata.length} got {num_read}"
                    )

                # Match local_disk_backend's layerwise async load behavior:
                # keep the staging object pinned until retrieve_layer() reaches
                # its post-sync unpin step.
                PinMonitor.GetOrCreate(self.local_cpu_backend.config)
                memory_obj.pin()

                cached_positions_path = self._get_cached_positions_path(key)
                if await aiofiles.os.path.exists(cached_positions_path):
                    async with aiofiles.open(cached_positions_path, "rb") as f_pos:
                        cached_positions_bytes = await f_pos.read()
                    memory_obj.metadata.cached_positions = _deserialize_cached_positions(
                        cached_positions_bytes
                    )

            return memory_obj

        except Exception as exc:
            if not isinstance(exc, FileNotFoundError):
                logger.error(f"Failed to read from file {file_path}: {exc}")
            if memory_obj is not None:
                memory_obj.ref_count_down()
            return None

    async def put(self, key: CacheEngineKey, memory_obj: MemoryObj):
        if not self.save_chunk_meta:
            await super().put(key, memory_obj)
            return

        final_path, temp_path = self._get_file_and_tmp_path(key)
        cached_positions_path = self._get_cached_positions_path(key)
        temp_cached_positions_path = cached_positions_path.with_suffix(".pos.tmp")

        try:
            logical_buffer = self._logical_byte_view(memory_obj)
            padded_shapes = [
                _pad_shape_for_remote_metadata(shape)
                for shape in memory_obj.get_shapes()
            ]
            metadata = RemoteMetadata(
                len(logical_buffer),
                padded_shapes,
                memory_obj.get_dtypes(),
                memory_obj.get_memory_format(),
            )

            async with aiofiles.open(temp_path, "wb") as f:
                await f.write(metadata.serialize())
                await f.write(logical_buffer)

            cached_positions_bytes = _serialize_cached_positions(
                memory_obj.metadata.cached_positions
            )
            async with aiofiles.open(temp_cached_positions_path, "wb") as f_pos:
                await f_pos.write(cached_positions_bytes)

            await aiofiles.os.replace(temp_path, final_path)
            await aiofiles.os.replace(temp_cached_positions_path, cached_positions_path)

        except Exception as exc:
            logger.error(f"Failed to write file {final_path}: {exc}")
            if await aiofiles.os.path.exists(temp_path):
                await aiofiles.os.unlink(temp_path)
            if await aiofiles.os.path.exists(temp_cached_positions_path):
                await aiofiles.os.unlink(temp_cached_positions_path)
            raise

    def remove_sync(self, key: CacheEngineKey) -> bool:
        removed = super().remove_sync(key)
        cached_positions_path = self._get_cached_positions_path(key)
        try:
            cached_positions_path.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.error(
                f"Failed to remove cached_positions sidecar {cached_positions_path}: {exc}"
            )
        return removed


class PriorityFSConnector(RemoteConnector):
    """A thin wrapper that preserves the current B -> A fs semantics."""

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
        connector_cls = (
            LayerwiseAwareFSConnector
            if config is not None and config.use_layerwise
            else FSConnector
        )
        self.read_connector = connector_cls(read_path, loop, local_cpu_backend, config)
        self.write_connector = connector_cls(
            write_path, loop, local_cpu_backend, config
        )
        logger.info(
            "Initialized PriorityFSConnector with read_path=%s write_path=%s connector=%s",
            read_path,
            write_path,
            connector_cls.__name__,
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
