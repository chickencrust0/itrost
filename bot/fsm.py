"""
bot/fsm.py — минимальный аналог aiogram FSMContext / MemoryStorage.

Как и в исходном боте (aiogram MemoryStorage), состояние живёт только в
памяти процесса и теряется при перезапуске — это не регрессия, а то же
самое поведение, что было раньше.
"""

import asyncio
from typing import Any, Dict, Optional


class _Storage:
    def __init__(self):
        self._data: Dict[int, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: int) -> Dict[str, Any]:
        async with self._lock:
            return dict(self._data.get(user_id, {}))

    async def set_state(self, user_id: int, state: Optional[str]) -> None:
        async with self._lock:
            entry = self._data.setdefault(user_id, {"state": None, "data": {}})
            entry["state"] = state

    async def get_state(self, user_id: int) -> Optional[str]:
        async with self._lock:
            return self._data.get(user_id, {}).get("state")

    async def update_data(self, user_id: int, **kwargs) -> None:
        async with self._lock:
            entry = self._data.setdefault(user_id, {"state": None, "data": {}})
            entry["data"].update(kwargs)

    async def get_data(self, user_id: int) -> Dict[str, Any]:
        async with self._lock:
            return dict(self._data.get(user_id, {}).get("data", {}))

    async def clear(self, user_id: int) -> None:
        async with self._lock:
            self._data.pop(user_id, None)


_storage = _Storage()


class FSMContext:
    """Тот же API, что у aiogram.fsm.context.FSMContext, для user_id."""

    def __init__(self, user_id: int):
        self.user_id = user_id

    async def set_state(self, state: Optional[str]) -> None:
        await _storage.set_state(self.user_id, state)

    async def get_state(self) -> Optional[str]:
        return await _storage.get_state(self.user_id)

    async def update_data(self, **kwargs) -> None:
        await _storage.update_data(self.user_id, **kwargs)

    async def get_data(self) -> Dict[str, Any]:
        return await _storage.get_data(self.user_id)

    async def clear(self) -> None:
        await _storage.clear(self.user_id)
