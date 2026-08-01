"""File transfer primitives used by Handler for upload/download.

These are low-level helpers that read/write individual data packets
on top of an asyncio StreamReader/Writer pair plus a PacketReader
buffer. Higher-level state machines live in Handler.
"""

import asyncio
from typing import Optional, Tuple

from common.protocol import (
    DATA_CHUNK_SIZE,
    END_FLAG_CONTINUE,
    END_FLAG_LAST,
    PacketReader,
    ProtocolError,
    decode_header,
    encode_data_packet,
)


class FileTransferError(Exception):
    pass


async def read_one_packet(
    reader: asyncio.StreamReader,
    pr: PacketReader,
    timeout: float = 30.0,
) -> Optional[Tuple[int, bytes]]:
    """Pull one complete packet from ``pr``, fetching more bytes from
    ``reader`` when needed. Returns ``(end_flag, data)`` or ``None`` on
    connection close.
    """
    while True:
        pkt = pr.next_packet()
        if pkt is not None:
            req_id, body_len, end_flag, body = pkt
            if len(body) != body_len:
                raise FileTransferError(
                    f"packet body length mismatch: header={body_len}, got={len(body)}"
                )
            if end_flag not in (END_FLAG_CONTINUE, END_FLAG_LAST):
                raise FileTransferError(f"invalid end_flag: {end_flag}")
            return end_flag, body
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        except asyncio.TimeoutError:
            raise FileTransferError("packet read timeout")
        if not chunk:
            return None
        pr.feed(chunk)


def write_data_packet(writer: asyncio.StreamWriter, req_id: int, end_flag: int, data: bytes) -> None:
    if len(data) > DATA_CHUNK_SIZE:
        raise FileTransferError(f"chunk too large: {len(data)}")
    writer.write(encode_data_packet(req_id, end_flag, data))