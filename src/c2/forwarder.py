"""C2-side Forwarder.

Sends an action command to the currently active Agent and awaits its
response, raising NetworkError on timeout / disconnect / protocol errors.
"""

import asyncio
from typing import List

from src.c2.agent_registry import AgentRegistry
from common.protocol import encode_control, encode_request


class NetworkError(Exception):
    """Raised when the C2 cannot communicate with the active Agent."""


class Forwarder:
    def __init__(self, registry: AgentRegistry, cmd_timeout: int = 60) -> None:
        self._registry = registry
        self._cmd_timeout = cmd_timeout

    async def forward(self, cmd: int, params: List[str]) -> str:
        agent = self._registry.get_active()
        if agent is None:
            raise NetworkError("No active agent")
        req_id = agent.allocate_request_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        agent.pending[req_id] = fut
        # Mark the agent busy for the whole command wait: a long-running
        # command (e.g. a slow exec_cmd, or a C remote whose event loop blocks
        # during execution) can suppress heartbeats; the watchdog must not
        # disconnect such an agent. Bounded by cmd_timeout.
        agent.active_ops += 1
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
            agent.active_ops -= 1
            # The agent just answered, so it is alive: refresh the timeout
            # record to avoid a stale watchdog check right after the command.
            self._registry.touch_heartbeat(agent.id)

    async def send_control(self, cmd: int, params: List[str]) -> None:
        """Send a control packet to the active Agent (no response awaited)."""
        agent = self._registry.get_active()
        if agent is None:
            raise NetworkError("No active agent")
        req_id = agent.allocate_request_id()
        packet = encode_control(req_id, cmd, params)
        try:
            async with agent.write_lock:
                agent.writer.write(packet)
                await agent.writer.drain()
        except Exception as e:
            raise NetworkError(f"failed to send control: {e}")