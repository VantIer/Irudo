"""C2 TCP server accepting connections from remote Agents.

For each connection:
1. Read the first packet - must be a ``register`` control packet.
2. Validate ``auth_token`` against the configured whitelist.
3. Add the Agent to AgentRegistry; send ``register_response``.
4. Start a per-connection reader task that dispatches packets to:
   - heartbeat / disconnect handlers
   - pending Future resolution for action / upload / download responses
"""

import asyncio
import logging
import time
from typing import Optional

from src.c2.agent_registry import AgentInfo, AgentRegistry
from common.protocol import (
    CMD_DISCONNECT,
    CMD_HEARTBEAT,
    CMD_HEARTBEAT_ACK,
    CMD_REGISTER,
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
        auth_tokens: list,
        heartbeat_timeout: int = 60,
    ) -> None:
        self._registry = registry
        self._host = host
        self._port = port
        self._auth_tokens = set(auth_tokens or [])
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
        """Periodically drop Agents whose last heartbeat is stale."""
        interval = max(5, self._heartbeat_timeout // 2)
        try:
            while True:
                await asyncio.sleep(interval)
                now = time.time()
                for info in list(self._registry.list_all()):
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
            register_info = await self._read_register(reader, pr)
            if register_info is None:
                writer.close()
                return
            agent_id, hostname, os_name = register_info
            info = AgentInfo(
                id=agent_id,
                hostname=hostname,
                os=os_name,
                connected_at=time.time(),
                last_heartbeat=time.time(),
                writer=writer,
            )
            await self._registry.register(info)
            try:
                pkt = encode_response(
                    0,
                    CMD_REGISTER_RESPONSE,
                    f"{CMD_REGISTER_RESPONSE}:accepted=true",
                )
                writer.write(pkt)
                await writer.drain()
            except Exception:
                pass

            await self._serve_connection(reader, writer, pr, agent_id)
        except Exception as e:
            logger.warning(f"connection handler error: {e}")
        finally:
            writer.close()

    async def _read_register(
        self,
        reader: asyncio.StreamReader,
        pr: PacketReader,
    ):
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
                if len(params) < 4:
                    logger.warning("register missing fields")
                    return None
                agent_id, auth_token, hostname, os_name = params[:4]
                if self._auth_tokens and auth_token not in self._auth_tokens:
                    logger.warning(f"auth_token rejected for {agent_id}")
                    return None
                return agent_id, hostname, os_name
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("register timeout")
                return None
            if not chunk:
                return None
            pr.feed(chunk)

    async def _serve_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        pr: PacketReader,
        agent_id: str,
    ) -> None:
        info = self._registry.get(agent_id)
        try:
            while True:
                pkt = pr.next_packet()
                if pkt is not None:
                    await self._dispatch_packet(writer, pr, info, pkt)
                    continue
                try:
                    chunk = await reader.read(4096)
                except Exception:
                    return
                if not chunk:
                    return
                pr.feed(chunk)
        finally:
            if self._registry.get(agent_id) is not None:
                await self._registry.unregister(agent_id)

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
            ts = body.decode("utf-8", errors="replace").strip() or str(int(time.time()))
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