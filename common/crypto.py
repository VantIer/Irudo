"""ChaCha20 stream cipher - pure Python, no third-party dependencies.

Adapted from the reference implementation in ``样例代码/chacha20.c``
(CycloneCRYPTO Open, RFC 7539 semantics: 256-bit key, 96-bit nonce,
32-bit block counter).

After the registration handshake succeeds, ALL packet traffic is encrypted
with ChaCha20. Both endpoints derive a 32-byte key from the shared auth
token via SHA-256, and each direction uses a distinct nonce so the two
keystreams never collide:

  C2 -> Agent : nonce = 00 00 00 00 00 00 00 00 00 00 00 00
  Agent -> C2 : nonce = 01 00 00 00 00 00 00 00 00 00 00 00

``EncryptedStream`` transparently encrypts writes / decrypts reads on top
of an asyncio reader/writer pair, so the rest of the code keeps using the
familiar ``write()`` / ``drain()`` / ``read()`` interface.
"""

import hashlib
import struct

NONCE_C2_TO_AGENT = b"\x00" * 12
NONCE_AGENT_TO_C2 = b"\x01" + b"\x00" * 11


def derive_key(auth_token: str) -> bytes:
    """Derive a 32-byte ChaCha20 key from the shared auth token."""
    return hashlib.sha256(auth_token.encode("utf-8")).digest()


class ChaCha20:
    """RFC 7539 ChaCha20 stream cipher (256-bit key, 96-bit nonce)."""

    def __init__(self, key: bytes, nonce: bytes, initial_counter: int = 0):
        if len(key) != 32:
            raise ValueError("ChaCha20 requires a 32-byte key")
        if len(nonce) != 12:
            raise ValueError("ChaCha20 requires a 12-byte nonce")
        constants = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
        key_words = struct.unpack("<8I", key)
        nonce_words = struct.unpack("<3I", nonce)
        self._state = (
            list(constants)
            + list(key_words)
            + [initial_counter & 0xFFFFFFFF]
            + list(nonce_words)
        )
        self._keystream = bytearray(64)
        self._pos = 0

    @staticmethod
    def _rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    def _block(self):
        state = self._state
        w = list(state)

        def qr(a, b, c, d):
            w[a] = (w[a] + w[b]) & 0xFFFFFFFF
            w[d] = self._rol(w[d] ^ w[a], 16)
            w[c] = (w[c] + w[d]) & 0xFFFFFFFF
            w[b] = self._rol(w[b] ^ w[c], 12)
            w[a] = (w[a] + w[b]) & 0xFFFFFFFF
            w[d] = self._rol(w[d] ^ w[a], 8)
            w[c] = (w[c] + w[d]) & 0xFFFFFFFF
            w[b] = self._rol(w[b] ^ w[c], 7)

        for _ in range(10):
            qr(0, 4, 8, 12)
            qr(1, 5, 9, 13)
            qr(2, 6, 10, 14)
            qr(3, 7, 11, 15)
            qr(0, 5, 10, 15)
            qr(1, 6, 11, 12)
            qr(2, 7, 8, 13)
            qr(3, 4, 9, 14)

        self._keystream[:] = struct.pack(
            "<16I", *((w[i] + state[i]) & 0xFFFFFFFF for i in range(16))
        )
        state[12] = (state[12] + 1) & 0xFFFFFFFF
        if state[12] == 0:
            state[13] = (state[13] + 1) & 0xFFFFFFFF
        self._pos = 0

    def crypt(self, data: bytes) -> bytes:
        n = len(data)
        out = bytearray(n)
        offset = 0
        while offset < n:
            if self._pos == 0 or self._pos >= 64:
                self._block()
            take = min(n - offset, 64 - self._pos)
            ks = self._keystream
            pos = self._pos
            for i in range(take):
                out[offset + i] = data[offset + i] ^ ks[pos + i]
            self._pos += take
            offset += take
        return bytes(out)


def build_crypto_pair(key: bytes, is_c2: bool):
    """Return (tx, rx) ChaCha20 contexts for one endpoint.

    ``is_c2=True``: tx encrypts C2 -> Agent, rx decrypts Agent -> C2.
    ``is_c2=False`` (agent): tx encrypts Agent -> C2, rx decrypts C2 -> Agent.
    """
    if is_c2:
        return ChaCha20(key, NONCE_C2_TO_AGENT), ChaCha20(key, NONCE_AGENT_TO_C2)
    return ChaCha20(key, NONCE_AGENT_TO_C2), ChaCha20(key, NONCE_C2_TO_AGENT)


class EncryptedStream:
    """Transparently encrypts writes / decrypts reads on a connection.

    Intended for post-handshake traffic only. ``write`` is synchronous
    (like ``asyncio.StreamWriter.write``), ``drain`` is async, ``read`` is
    async and returns decrypted bytes.
    """

    def __init__(self, reader, writer, tx: ChaCha20, rx: ChaCha20):
        self._reader = reader
        self._writer = writer
        self._tx = tx
        self._rx = rx

    def write(self, data: bytes) -> None:
        self._writer.write(self._tx.crypt(data))

    async def drain(self) -> None:
        await self._writer.drain()

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            raw = await self._reader.read()
        else:
            raw = await self._reader.read(n)
        if not raw:
            return raw
        return self._rx.crypt(raw)

    async def readexactly(self, n: int) -> bytes:
        raw = await self._reader.readexactly(n)
        return self._rx.crypt(raw)

    def absorb_leftover(self, pr) -> None:
        """Decrypt and re-feed bytes buffered in a PacketReader before
        encryption was enabled.

        After the handshake the peer may already have sent the first
        encrypted bytes (e.g. TCP coalescing with the register_confirm).
        Those raw bytes must be decrypted with the rx cipher before the
        packet reader parses them.
        """
        if pr.buffered:
            raw = pr.drain_all()
            pr.feed(self._rx.crypt(raw))

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass
