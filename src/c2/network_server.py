"""C2 TCP server accepting connections from remote Agents.

For each connection:
1. Read the first packet - must be a ``register`` control packet that
   carries ONLY a random nonce (no identity before authentication).
2. Reply with ``register_response`` = ``sha256(nonce + auth_token)``.
3. Wait for ``register_confirm`` which now carries the Agent identity
   (agent_id, hostname, os); only then add the Agent to AgentRegistry.
4. Start a per-connection reader task that dispatches packets to:
   - heartbeat / disconnect handlers
   - pending Future resolution for action / upload / download responses
"""

import asyncio
import hashlib
import logging
import time
from typing import Optional, Tuple

from src.c2.agent_registry import AgentInfo, AgentRegistry
from common.crypto import NONCE_AGENT_TO_C2, NONCE_C2_TO_AGENT, ChaCha20, EncryptedStream, derive_key
from common.protocol import (
    CMD_DISCONNECT,
    CMD_HEARTBEAT,
    CMD_HEARTBEAT_ACK,
    CMD_REGISTER,
    CMD_REGISTER_CONFIRM,
    CMD_REGISTER_RESPONSE,
    PacketReader,
    ProtocolError,
    decode_tlv,
    encode_control,
    encode_response,
)


logger = logging.getLogger(__name__)


class NetworkServer:
    def __init__(
        self,
        registry: AgentRegistry,
        host: str,
        port: int,
        auth_token: str = "",
        heartbeat_timeout: int = 60,
    ) -> None:
        self._registry = registry
        self._host = host
        self._port = port
        self._auth_token = auth_token or ""
        self._heartbeat_timeout = heartbeat_timeout
        self._server: Optional[asyncio.AbstractServer] = None
        self._watchdog_task: Optional[asyncio.Task] = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, self._host, self._port)
        socks = self._server.sockets or []
        if socks:
            self._port = socks[0].getsockname()[1]
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info(f"C2 network server listening on {self._host}:{self._port}")

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            self._server = None

    async def _watchdog_loop(self) -> None:
        """Periodically drop Agents whose last heartbeat is stale.

        Agents with an active operation (``info.active_ops > 0``, e.g. a file
        transfer or an in-flight command) are skipped: heartbeats may be
        suppressed while streaming large files or running long commands, so a
        busy agent must not be disconnected for a heartbeat gap.
        """
        interval = max(5, self._heartbeat_timeout // 2)
        try:
            while True:
                await asyncio.sleep(interval)
                now = time.time()
                for info in list(self._registry.list_all()):
                    if info.active_ops > 0:
                        continue
                    if now - info.last_heartbeat > self._heartbeat_timeout:
                        logger.warning(f"agent {info.id} heartbeat timeout, closing")
                        try:
                            info.writer.close()
                        except Exception:
                            pass
                        await self._registry.unregister(info.id)
        except asyncio.CancelledError:
            return

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info(f"incoming connection from {peer}")
        pr = PacketReader()
        try:
            info = await self._handshake(reader, writer, pr)
            if info is None:
                logger.warning("registration handshake failed")
                writer.close()
                return
            await self._registry.register(info)
            await self._serve_connection(info.writer, pr, info)
        except Exception as e:
            logger.warning(f"connection handler error: {e}")
        finally:
            writer.close()

    async def _handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        pr: PacketReader,
    ) -> Optional[AgentInfo]:
        """Challenge-response registration handshake.

        1. Agent sends ``register`` carrying ONLY a random nonce
           (no agent id / hostname / os yet, to avoid leaking identity
           before authentication).
        2. C2 replies with ``register_response`` whose body is
           ``sha256(nonce + c2_auth_tokens)``.
        3. Agent verifies the hash locally; on success it replies with
           ``register_confirm`` now carrying the identity fields
           (agent_id, hostname, os).
        4. C2 registers the Agent only after receiving ``register_confirm``.
        """
        nonce = await self._read_register(reader, pr)
        if nonce is None:
            return None

        if not self._auth_token:
            logger.warning("c2_auth_tokens is not configured; refusing registration")
            return None

        digest = self._challenge_hash(nonce)
        try:
            writer.write(encode_response(0, CMD_REGISTER_RESPONSE, digest))
            await writer.drain()
        except Exception:
            return None

        confirm_info = await self._wait_confirm(reader, pr)
        if confirm_info is None:
            return None
        agent_id, hostname, os_name = confirm_info

        # Handshake + auth succeeded: switch the whole byte stream to
        # ChaCha20 encryption keyed by sha256(c2_auth_tokens).
        key = derive_key(self._auth_token)
        tx = ChaCha20(key, NONCE_C2_TO_AGENT)
        rx = ChaCha20(key, NONCE_AGENT_TO_C2)
        stream = EncryptedStream(reader, writer, tx, rx)

        return AgentInfo(
            id=agent_id,
            hostname=hostname,
            os=os_name,
            connected_at=time.time(),
            last_heartbeat=time.time(),
            writer=stream,
        )

    def _challenge_hash(self, nonce: str) -> str:
        return hashlib.sha256((nonce + self._auth_token).encode("utf-8")).hexdigest()

    async def _read_register(
        self,
        reader: asyncio.StreamReader,
        pr: PacketReader,
    ) -> Optional[str]:
        """Read the first packet - must be ``register`` carrying only the nonce."""
        while True:
            pkt = pr.next_packet()
            if pkt is not None:
                req_id, _body_len, cmd, body = pkt
                if cmd != CMD_REGISTER:
                    logger.warning(f"first packet not register: cmd={cmd:#x}")
                    return None
                try:
                    params = decode_tlv(body)
                except ProtocolError:
                    return None
                if not params:
                    logger.warning("register missing nonce")
                    return None
                nonce = params[0]
                if not nonce:
                    logger.warning("register missing nonce")
                    return None
                return nonce
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("register timeout")
                return None
            if not chunk:
                return None
            pr.feed(chunk)

    async def _wait_confirm(
        self,
        reader: asyncio.StreamReader,
        pr: PacketReader,
        timeout: float = 30.0,
    ) -> Optional[Tuple[str, str, str]]:
        """Wait for ``register_confirm``; returns ``(agent_id, hostname, os)``.

        The confirm packet (sent only after the Agent verified the challenge)
        carries the identity fields that were deferred from ``register``.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            pkt = pr.next_packet()
            if pkt is not None:
                _req_id, _body_len, cmd, body = pkt
                if cmd == CMD_REGISTER_CONFIRM:
                    try:
                        params = decode_tlv(body)
                    except ProtocolError:
                        params = []
                    if len(params) < 3 or not params[0]:
                        logger.warning("register_confirm missing identity fields")
                        return None
                    return params[0], params[1], params[2]
                logger.warning(f"unexpected packet during handshake: cmd={cmd:#x}")
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
            pr.feed(chunk)
        return None

    async def _serve_connection(
        self,
        stream,
        pr: PacketReader,
        info: AgentInfo,
    ) -> None:
        stream.absorb_leftover(pr)
        try:
            while True:
                pkt = pr.next_packet()
                if pkt is not None:
                    await self._dispatch_packet(stream, pr, info, pkt)
                    continue
                try:
                    chunk = await stream.read(4096)
                except Exception:
                    return
                if not chunk:
                    return
                pr.feed(chunk)
        finally:
            if self._registry.get(info.id) is not None:
                await self._registry.unregister(info.id)

    async def _dispatch_packet(
        self,
        writer: asyncio.StreamWriter,
        pr: PacketReader,
        info: Optional[AgentInfo],
        pkt,
    ) -> None:
        req_id, _body_len, cmd, body = pkt
        if info is None:
            return

        if cmd == CMD_HEARTBEAT:
            self._registry.touch_heartbeat(info.id)
            try:
                params = decode_tlv(body)
            except ProtocolError:
                params = []
            ts = params[0].strip() if params else ""
            if not ts:
                ts = str(int(time.time()))
            try:
                async with info.write_lock:
                    writer.write(encode_response(req_id, CMD_HEARTBEAT_ACK, ts))
                    await writer.drain()
            except Exception:
                pass
            return

        if cmd == CMD_DISCONNECT:
            logger.info(f"agent {info.id} disconnect: {body!r}")
            await self._registry.unregister(info.id)
            return

        if cmd == CMD_REGISTER:
            return

        if req_id in info.data_queues:
            q = info.data_queues[req_id]
            end_flag = cmd
            try:
                # Async put with backpressure: never silently drop data packets.
                await asyncio.wait_for(q.put((end_flag, body)), timeout=10)
            except (asyncio.TimeoutError, asyncio.QueueFull):
                logger.warning(f"drop download data pkt req_id={req_id} (consumer gone?)")
            return

        fut = info.pending.pop(req_id, None)
        if fut is not None and not fut.done():
            try:
                fut.set_result(body.decode("utf-8", errors="replace"))
            except Exception as e:
                fut.set_exception(e)

    async def disconnect_all(self) -> None:
        for info in self._registry.list_all():
            try:
                async with info.write_lock:
                    info.writer.write(encode_control(0, CMD_DISCONNECT, ["server shutdown"]))
                    await info.writer.drain()
            except Exception:
                pass