from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.storage_controllers.store_policy import (
    AdapterDescriptor,
    StorePolicy,
    register_store_policy,
)


class WriteLastStorePolicy(StorePolicy):
    """Store every key only to the last configured L2 adapter."""

    def select_store_targets(
        self,
        keys: list[ObjectKey],
        adapters: list[AdapterDescriptor],
    ) -> dict[int, list[ObjectKey]]:
        if not adapters:
            return {}
        return {adapters[-1].index: list(keys)}

    def select_l1_deletions(
        self,
        keys: list[ObjectKey],
    ) -> list[ObjectKey]:
        return []


register_store_policy("write_last", WriteLastStorePolicy)
