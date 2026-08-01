"""Remote Agent packet handler.

State-aware dispatcher. Processes packets from C2 in order:

- Control commands (heartbeat) handled inline.
- Action commands dispatched to local_executor (action result returned).
- upload init enters the receive-upload state machine, which consumes
  subsequent data packets until end_flag=1.
- download init enters the send-download state machine, which sends
  data packets immediately.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from common.protocol import (
    ACTION_CMDS,
    CMD_DOWNLOAD,
    CMD_HEARTBEAT,
    CMD_HEARTBEAT_ACK,
    CMD_REGISTER_RESPONSE,
    CMD_SHUTDOWN,
    CMD_UPLOAD,
    DATA_CHUNK_SIZE,
    END_FLAG_CONTINUE,
    END_FLAG_LAST,
    PacketReader,
    ProtocolError,
    decode_tlv,
    encode_data_packet,
    encode_response,
)
from remote.file_transfer import (
    FileTransferError,
    read_one_packet,
)
from remote.local_executor import execute as exec_action


logger = logging.getLogger(__name__)


class HandlerError(Exception):
    pass


class Handler:
    """Stateful dispatcher for incoming packets on the Agent side."""

    def __init__(
        self,
        cmd_timeout: int = 60,
        write_lock: Optional[asyncio.Lock] = None,
        on_shutdown: Optional[callable] = None,
    ) -> None:
        self._cmd_timeout = cmd_timeout
        self._reader = PacketReader()
        self._write_lock = write_lock
        self._on_shutdown = on_shutdown

    def feed(self, data: bytes) -> None:
        self._reader.feed(data)

    def next_packet(self):
        return self._reader.next_packet()

    @property
    def buffered(self) -> int:
        return self._reader.buffered

    def reset(self) -> None:
        """Drop any buffered packets from a previous connection."""
        self._reader = PacketReader()

    async def process(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Drain all complete packets currently buffered."""
        while True:
            pkt = self._reader.next_packet()
            if pkt is None:
                return
            req_id, _body_len, cmd, body = pkt
            await self._dispatch(reader, writer, req_id, cmd, body)

    async def _dispatch(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        req_id: int,
        cmd: int,
        body: bytes,
    ) -> None:
        if cmd == CMD_HEARTBEAT:
            ts = body.decode("utf-8", errors="replace").strip() or str(int(time.time()))
            await self._write_packet(writer, encode_response(req_id, CMD_HEARTBEAT_ACK, ts))
            return

        if cmd == CMD_REGISTER_RESPONSE:
            logger.debug(f"recv register_response req_id={req_id}: {body!r}")
            return

        if cmd == CMD_SHUTDOWN:
            logger.info(f"recv shutdown cmd req_id={req_id}; terminating")
            try:
                await self._write_packet(writer, encode_response(req_id, CMD_SHUTDOWN, "ok"))
            except Exception:
                pass
            if self._on_shutdown is not None:
                try:
                    self._on_shutdown()
                except Exception as e:
                    logger.warning(f"on_shutdown callback error: {e}")
            return

        if cmd in ACTION_CMDS:
            name, _param_names = ACTION_CMDS[cmd]
            try:
                params = decode_tlv(body)
            except ProtocolError as e:
                logger.debug(f"recv cmd req_id={req_id} cmd={cmd:#x} action={name}: bad TLV: {e}")
                await self._write_packet(writer, encode_response(req_id, cmd, f"Error: bad TLV: {e}"))
                return
            logger.debug(f"recv cmd req_id={req_id} cmd={cmd:#x} action={name} params={params}")
            result = exec_action(name, params, cmd_timeout=self._cmd_timeout)
            logger.debug(f"exec result req_id={req_id} action={name} -> {result!r}")
            await self._write_packet(writer, encode_response(req_id, cmd, result))
            return

        if cmd == CMD_UPLOAD:
            try:
                params = decode_tlv(body)
            except ProtocolError as e:
                params = []
            logger.debug(f"recv cmd req_id={req_id} cmd=upload params={params}")
            await self._handle_upload(reader, writer, req_id, body)
            return

        if cmd == CMD_DOWNLOAD:
            try:
                params = decode_tlv(body)
            except ProtocolError as e:
                params = []
            logger.debug(f"recv cmd req_id={req_id} cmd=download params={params}")
            await self._handle_download(writer, req_id, body)
            return

        await self._write_packet(writer, encode_response(req_id, cmd, f"Error: Unknown cmd: {cmd:#x}"))

    async def _write_packet(self, writer: asyncio.StreamWriter, packet: bytes) -> None:
        if self._write_lock is None:
            writer.write(packet)
            await writer.drain()
            return
        async with self._write_lock:
            writer.write(packet)
            await writer.drain()

    async def _handle_upload(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        req_id: int,
        init_body: bytes,
    ) -> None:
        try:
            params = decode_tlv(init_body)
        except ProtocolError as e:
            await self._write_packet(writer, encode_response(req_id, CMD_UPLOAD, f"Error: bad TLV: {e}"))
            return
        if len(params) < 1:
            await self._write_packet(writer, encode_response(req_id, CMD_UPLOAD, "Error: missing dest_path"))
            return
        dest_path = params[0]
        target = Path(dest_path).resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fileobj = open(target, "wb")
        except Exception as e:
            await self._write_packet(writer, encode_response(req_id, CMD_UPLOAD, f"Error: cannot create file: {e}"))
            return
        total = 0
        success = False
        try:
            while True:
                pkt = await read_one_packet(reader, self._reader)
                if pkt is None:
                    raise FileTransferError("connection closed mid-upload")
                end_flag, data = pkt
                if data:
                    fileobj.write(data)
                    total += len(data)
                if end_flag == END_FLAG_LAST:
                    success = True
                    break
        except FileTransferError:
            pass
        except Exception:
            pass
        finally:
            fileobj.close()
        if success:
            result = f"Successfully uploaded: {dest_path} ({total} bytes)"
            logger.debug(f"exec result req_id={req_id} action=upload -> {result!r}")
            await self._write_packet(writer, encode_response(req_id, CMD_UPLOAD, result))
        else:
            try:
                target.unlink()
            except OSError:
                pass
            logger.debug(f"exec result req_id={req_id} action=upload -> Error: upload failed")
            await self._write_packet(writer, encode_response(req_id, CMD_UPLOAD, "Error: upload failed"))

    async def _handle_download(
        self,
        writer: asyncio.StreamWriter,
        req_id: int,
        init_body: bytes,
    ) -> None:
        try:
            params = decode_tlv(init_body)
        except ProtocolError as e:
            await self._write_packet(writer, encode_response(req_id, CMD_DOWNLOAD, f"Error: bad TLV: {e}"))
            return
        if len(params) < 1:
            await self._write_packet(writer, encode_response(req_id, CMD_DOWNLOAD, "Error: missing src_path"))
            return
        src_path = params[0]
        target = Path(src_path).resolve()
        if not target.exists():
            await self._write_packet(writer, encode_response(req_id, CMD_DOWNLOAD, f"Error: source not found: {src_path}"))
            return
        if target.is_dir():
            await self._write_packet(writer, encode_response(req_id, CMD_DOWNLOAD, f"Error: source is a directory: {src_path}"))
            return
        try:
            fileobj = open(target, "rb")
        except Exception as e:
            await self._write_packet(writer, encode_response(req_id, CMD_DOWNLOAD, f"Error: cannot open source: {e}"))
            return
        total = 0
        try:
            while True:
                chunk = fileobj.read(DATA_CHUNK_SIZE)
                if not chunk:
                    end_flag = END_FLAG_LAST
                elif len(chunk) < DATA_CHUNK_SIZE:
                    end_flag = END_FLAG_LAST
                else:
                    end_flag = END_FLAG_CONTINUE
                await self._write_packet(writer, encode_data_packet(req_id, end_flag, chunk))
                total += len(chunk)
                if end_flag == END_FLAG_LAST:
                    break
            logger.debug(f"exec result req_id={req_id} action=download -> {total} bytes sent for {src_path}")
        except Exception as e:
            logger.debug(f"exec result req_id={req_id} action=download -> Error: {e}")
            return
        finally:
            fileobj.close()