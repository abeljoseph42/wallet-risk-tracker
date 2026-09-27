"""A small in-process cache with per-entry expiry and a size cap."""

import time
from collections import OrderedDict
from collections.abc import Callable


class TTLCache[K, V]:
    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> tuple[bool, V | None]:
        """(hit, value). A cached value may itself be None (e.g. "this token has no price")."""
        entry = self._data.get(key)
        if entry is None:
            return False, None
        expires, value = entry
        if self._clock() >= expires:
            del self._data[key]
            return False, None
        return True, value

    def set(self, key: K, value: V) -> None:
        self._data[key] = (self._clock() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)
