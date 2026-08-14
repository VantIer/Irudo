"""C2-side Forwarder.

Sends an action command to an Agent (default: the currently active one)
and awaits its response, raising NetworkError on timeout / disconnect /
protocol errors.

Every command holds the target Agent's ``instruction_lock`` for the whole
request/response cycle, so commands and file transfers on the same Agent
are serialized (new commands arriving during a transfer simply wait for
the lock instead of corrupting the protocol). Agents have independent
locks, so different Agents' sessions run concurrently.
"""

import asyncio
from typing import List, Optional

from src.c2.agent_registry import AgentRegistry
from common.protocol import encode_control, encode_request


class NetworkError(Exception):
    """Raised when the C2 cannot communicate with an Agent."""


class Forwarder:
    def __init__(self, registry: AgentRegistry, cmd_timeout: int = 60) -> None:
        self._registry = registry
        self._cmd_timeout = cmd_timeout

    async def forward(self, cmd: int, params: List[str], agent_id: Optional[str] = None) -> str:
        agent = self._registry.get(agent_id) if agent_id else self._registry.get_active()
        if agent is None:
            raise NetworkError("No active agent")
        # Mark the agent busy for the whole command wait (including any wait
        # for the instruction lock): a long-running command can suppress
        # heartbeats, so the watchdog must not disconnect it. Bounded by
        # cmd_timeout.
        agent.active_ops += 1
        try:
            async with agent.instruction_lock:
                req_id = agent.allocate_request_id()
                fut: asyncio.Future = asyncio.get_running_loop().create_future()
                agent.pending[req_id] = fut
                try:
                    try:
                        packet = encode_request(req_id, cmd, params)
                        async with agent.write_lock:
                            agent.writer.write(packet)
                            await agent.writer.drain()
                    except Exception as e:
                        raise NetworkError(f"failed to send: {e}")
                    try:
                        result = await asyncio.wait_for(fut, timeout=self._cmd_timeout)
                        return result
                    except asyncio.TimeoutError:
                        raise NetworkError(f"timeout after {self._cmd_timeout}s")
                    except ConnectionError as e:
                        raise NetworkError(f"connection lost: {e}")
                finally:
                    agent.pending.pop(req_id, None)
        finally:
            agent.active_ops -= 1
            # The agent just answered, so it is alive: refresh the timeout
            # record to avoid a stale watchdog check right after the command.
            self._registry.touch_heartbeat(agent.id)

    async def send_control(self, cmd: int, params: List[str], agent_id: Optional[str] = None) -> None:
        """Send a control packet to an Agent (no response awaited)."""
        agent = self._registry.get(agent_id) if agent_id else self._registry.get_active()
        if agent is None:
            raise NetworkError("No active agent")
        req_id = agent.allocate_request_id()
        packet = encode_control(req_id, cmd, params)
        try:
            async with agent.instruction_lock:
                async with agent.write_lock:
                    agent.writer.write(packet)
                    await agent.writer.drain()
        except Exception as e:
            raise NetworkError(f"failed to send control: {e}")
