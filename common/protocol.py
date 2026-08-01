"""Binary communication protocol between C2 and remote Agent.

Packet structure (16-byte header + variable body):

  Header:
    [0..7]   request_id  uint64 LE
    [8..11]  body_len    uint32 LE  (size of body in bytes; 0 = no body)
    [12..14] reserved    3 bytes (must be 0)
    [15]     cmd/flag    uint8     (action cmd / control cmd / file transfer end_flag)

  Body (request):  TLV chain of (uint32 length + UTF-8 data)
  Body (response): single UTF-8 string (no TLV split, no length prefix per param)
  Body (data pkt): raw file bytes (binary-safe)
"""

import struct
from typing import List, Optional, Tuple


PACKET_HEADER_LEN = 16
REQUEST_ID_SIZE = 8
BODY_LEN_SIZE = 4
RESERVED_SIZE = 3
CMD_SIZE = 1

REQUEST_ID_OFFSET = 0
BODY_LEN_OFFSET = 8
RESERVED_OFFSET = 12
CMD_OFFSET = 15

DATA_CHUNK_SIZE = 512


ACTION_CMDS = {
    0x01: ("list_dir",    ["path"]),
    0x02: ("make_dir",    ["path"]),
    0x03: ("delete_dir",  ["path"]),
    0x04: ("rename_dir",  ["path", "new_name"]),
    0x05: ("read_file",   ["path", "start_line", "end_line"]),
    0x06: ("write_file",  ["path", "content"]),
    0x07: ("delete_file", ["path"]),
    0x08: ("edit_file",   ["path", "operation", "start_line", "end_line", "content"]),
    0x09: ("rename_file", ["path", "new_name"]),
    0x0A: ("copy",        ["src", "dest"]),
    0x0B: ("move",        ["src", "dest"]),
    0x0E: ("create_file", ["path"]),
    0x0F: ("get_cwd",     []),
    0x10: ("exec_cmd",    ["command"]),
}

CMD_UPLOAD = 0x0C
CMD_DOWNLOAD = 0x0D

CMD_REGISTER = 0x80
CMD_REGISTER_RESPONSE = 0x81
CMD_HEARTBEAT = 0x82
CMD_HEARTBEAT_ACK = 0x83
CMD_DISCONNECT = 0x84
CMD_SHUTDOWN = 0x85

CONTROL_CMDS = {
    CMD_REGISTER: "register",
    CMD_REGISTER_RESPONSE: "register_response",
    CMD_HEARTBEAT: "heartbeat",
    CMD_HEARTBEAT_ACK: "heartbeat_ack",
    CMD_DISCONNECT: "disconnect",
    CMD_SHUTDOWN: "shutdown",
}

ACTION_NAME_TO_CMD = {v[0]: k for k, v in ACTION_CMDS.items()}

END_FLAG_CONTINUE = 0
END_FLAG_LAST = 1

REQUEST_CMDS = set(ACTION_CMDS.keys()) | {CMD_UPLOAD, CMD_DOWNLOAD}


class ProtocolError(Exception):
    pass


def encode_header(req_id: int, body_len: int, cmd_or_flag: int) -> bytes:
    buf = bytearray(PACKET_HEADER_LEN)
    struct.pack_into("<Q", buf, REQUEST_ID_OFFSET, req_id)
    struct.pack_into("<I", buf, BODY_LEN_OFFSET, body_len)
    buf[CMD_OFFSET] = cmd_or_flag & 0xFF
    return bytes(buf)


def decode_header(data: bytes) -> Tuple[int, int, int]:
    if len(data) < PACKET_HEADER_LEN:
        raise ProtocolError(f"header too short: {len(data)}")
    req_id = struct.unpack_from("<Q", data, REQUEST_ID_OFFSET)[0]
    body_len = struct.unpack_from("<I", data, BODY_LEN_OFFSET)[0]
    cmd_or_flag = data[CMD_OFFSET]
    return req_id, body_len, cmd_or_flag


def encode_tlv(params: List[str]) -> bytes:
    buf = bytearray()
    for p in params:
        data = p.encode("utf-8") if isinstance(p, str) else bytes(p)
        buf += struct.pack("<I", len(data))
        buf += data
    return bytes(buf)


def decode_tlv(buf: bytes) -> List[str]:
    out: List[str] = []
    i = 0
    n = len(buf)
    while i < n:
        if i + 4 > n:
            raise ProtocolError(f"TLV truncated at offset {i}")
        length = struct.unpack_from("<I", buf, i)[0]
        i += 4
        end = i + length
        if end > n:
            raise ProtocolError(f"TLV data overruns at offset {i}: len={length}")
        out.append(buf[i:end].decode("utf-8", errors="replace"))
        i = end
    return out


def encode_request(req_id: int, cmd: int, params: List[str]) -> bytes:
    if cmd not in REQUEST_CMDS:
        raise ProtocolError(f"unknown request cmd: {cmd:#x}")
    body = encode_tlv(params)
    return encode_header(req_id, len(body), cmd) + body


def encode_response(req_id: int, cmd: int, result: str) -> bytes:
    body = result.encode("utf-8")
    return encode_header(req_id, len(body), cmd) + body


def encode_data_packet(req_id: int, end_flag: int, data: bytes) -> bytes:
    if len(data) > DATA_CHUNK_SIZE:
        raise ProtocolError(f"data chunk too large: {len(data)} > {DATA_CHUNK_SIZE}")
    return encode_header(req_id, len(data), end_flag) + data


def encode_control(req_id: int, cmd: int, params: List[str]) -> bytes:
    if cmd not in CONTROL_CMDS:
        raise ProtocolError(f"unknown control cmd: {cmd:#x}")
    body = encode_tlv(params)
    return encode_header(req_id, len(body), cmd) + body


class PacketReader:
    """Stream-oriented packet parser with internal buffer for half-packets / stickiness."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def next_packet(self) -> Optional[Tuple[int, int, int, bytes]]:
        if len(self._buf) < PACKET_HEADER_LEN:
            return None
        try:
            req_id, body_len, cmd = decode_header(bytes(self._buf))
        except ProtocolError:
            raise
        total = PACKET_HEADER_LEN + body_len
        if len(self._buf) < total:
            return None
        body = bytes(self._buf[PACKET_HEADER_LEN:total])
        del self._buf[:total]
        return req_id, body_len, cmd, body