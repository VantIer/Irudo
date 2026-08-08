"""C2-side registry of connected remote Agents.

Each AgentInfo holds the persistent state for one remote Agent:
- identity (id, hostname, os)
- TCP plumbing (writer for outbound, packet_reader for inbound)
- pending request Future dict (request_id -> Future)
- per-Agent conversation history with the LLM
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from common.protocol import END_FLAG_LAST


@dataclass
class AgentInfo:
    id: str
    hostname: str
    os: str
    connected_at: float
    last_heartbeat: float
    writer: asyncio.StreamWriter
    conversation_history: List[dict] = field(default_factory=list)
    pending: Dict[int, asyncio.Future] = field(default_factory=dict)
    data_queues: Dict[int, asyncio.Queue] = field(default_factory=dict)
    auth_token_valid: bool = True
    next_request_id_min: int = 1_000_000
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_ops: int = 0

    def allocate_request_id(self) -> int:
        rid = self.next_request_id_min
        self.next_request_id_min += 1
        return rid

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "hostname": self.hostname,
            "os": self.os,
            "connected_at": self.connected_at,
            "last_heartbeat": self.last_heartbeat,
            "online": True,
        }


class AgentRegistry:
    """Thread-safe (asyncio-safe) registry of AgentInfo."""

    def __init__(self) -> None:
        self._agents: Dict[str, AgentInfo] = {}
        self._lock = asyncio.Lock()
        self._active_id: Optional[str] = None
        self._listeners: List[Callable] = []

    def add_listener(self, callback: Callable) -> None:
        self._listeners.append(callback)

    def _notify(self, event: str, info: Optional[AgentInfo] = None) -> None:
        for cb in list(self._listeners):
            try:
                cb(event, info)
            except Exception:
                pass

    async def register(self, info: AgentInfo) -> None:
        async with self._lock:
            existing = self._agents.get(info.id)
            if existing is not None:
                try:
                    existing.writer.close()
                except Exception:
                    pass
            self._agents[info.id] = info
            if self._active_id is None:
                self._active_id = info.id
        self._notify("registered", info)

    async def unregister(self, agent_id: str) -> None:
        async with self._lock:
            info = self._agents.pop(agent_id, None)
            if self._active_id == agent_id:
                self._active_id = next(iter(self._agents), None)
        if info is not None:
            for fut in info.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Agent disconnected"))
            info.pending.clear()
            for q in info.data_queues.values():
                try:
                    q.put_nowait((END_FLAG_LAST, b""))
                except (asyncio.QueueFull, Exception):
                    pass
            info.data_queues.clear()
            self._notify("unregistered", info)

    def get(self, agent_id: str) -> Optional[AgentInfo]:
        return self._agents.get(agent_id)

    def list_all(self) -> List[AgentInfo]:
        return list(self._agents.values())

    @property
    def active_id(self) -> Optional[str]:
        return self._active_id

    def get_active(self) -> Optional[AgentInfo]:
        if self._active_id is None:
            return None
        return self._agents.get(self._active_id)

    def set_active(self, agent_id: str) -> bool:
        if agent_id not in self._agents:
            return False
        self._active_id = agent_id
        self._notify("active_changed", self._agents[agent_id])
        return True

    def touch_heartbeat(self, agent_id: str) -> None:
        info = self._agents.get(agent_id)
        if info is not None:
            info.last_heartbeat = time.time()

    def history_of(self, agent_id: str) -> List[dict]:
        info = self._agents.get(agent_id)
        if info is None:
            return []
        return info.conversation_history