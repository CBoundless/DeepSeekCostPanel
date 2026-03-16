from __future__ import annotations

import asyncio
import itertools
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import WebSocket


@dataclass
class RealtimeConnection:
    connection_id: int
    user_id: int
    websocket: WebSocket
    queue: asyncio.Queue[Dict[str, Any]]
    loop: asyncio.AbstractEventLoop


class RealtimeHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._connections: Dict[int, RealtimeConnection] = {}

    async def register(self, user_id: int, websocket: WebSocket) -> int:
        await websocket.accept()
        connection_id = next(self._seq)
        connection = RealtimeConnection(
            connection_id=connection_id,
            user_id=user_id,
            websocket=websocket,
            queue=asyncio.Queue(maxsize=200),
            loop=asyncio.get_running_loop(),
        )
        with self._lock:
            self._connections[connection_id] = connection
        await connection.queue.put({"type": "connected", "payload": {"user_id": user_id}})
        return connection_id

    def unregister(self, connection_id: int) -> None:
        with self._lock:
            self._connections.pop(connection_id, None)

    async def pump(self, connection_id: int) -> None:
        connection = self._connections.get(connection_id)
        if not connection:
            return
        while True:
            try:
                message = await asyncio.wait_for(connection.queue.get(), timeout=15)
            except asyncio.TimeoutError:
                message = {"type": "heartbeat", "payload": {"connection_id": connection_id}}
            await connection.websocket.send_text(json.dumps(message, ensure_ascii=False))

    def publish_user(self, user_id: int, message_type: str, payload: Dict[str, Any] | None = None) -> None:
        targets: list[RealtimeConnection] = []
        with self._lock:
            for connection in self._connections.values():
                if connection.user_id == user_id:
                    targets.append(connection)
        message = {"type": message_type, "payload": payload or {}}
        for connection in targets:
            connection.loop.call_soon_threadsafe(self._enqueue_safe, connection.queue, message)

    @staticmethod
    def _enqueue_safe(queue: asyncio.Queue[Dict[str, Any]], message: Dict[str, Any]) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass


realtime_hub = RealtimeHub()
