from __future__ import annotations

import array
import asyncio
import mmap
import os
import socket
import struct
from dataclasses import dataclass
from math import isfinite
from typing import Optional, Tuple


TOUCH_PACKET_BYTES = 16
TOUCH_PACKET_DOWN = 1
TOUCH_PACKET_MOVE = 2
TOUCH_PACKET_UP = 3
TOUCH_PACKET_CANCEL = 4
TOUCH_PACKET_CLEAR = 5

MAX_HEADER_BYTES = 4096


class DirectScreenProtocolError(RuntimeError):
    """DirectScreenAPI 协议错误。"""


class FrameTimeoutError(TimeoutError):
    """等待帧超时。"""


@dataclass(frozen=True)
class RawFrame:
    """一帧原始 RGBA 数据。"""

    frame_seq: int
    width: int
    height: int
    pixel_format: str
    byte_len: int
    checksum: int
    rgba: bytes


@dataclass(frozen=True)
class _WaitFrameMeta:
    """RENDER_FRAME_WAIT_SHM_PRESENT 响应头。"""

    frame_seq: int
    width: int
    height: int
    pixel_format: str
    byte_len: int
    checksum: int
    offset: int


class DirectScreenClient:
    """
    DirectScreenAPI 异步客户端。

    设计说明：
    1. 取帧路径需要 recvmsg/SCM_RIGHTS 接收 fd，因此底层使用同步 socket，
       通过 asyncio.to_thread 放到线程执行，保证事件循环不阻塞。
    2. 触控路径采用 STREAM_TOUCH_V1 持久连接，使用 asyncio Stream 做异步写入。
    3. 文本语义 TOUCH_DOWN / TOUCH_MOVE / TOUCH_UP 在本实现中映射为
       STREAM_TOUCH_V1 的 kind=1/2/3 固定 16B 数据包。
    """

    def __init__(
        self,
        data_socket_path: str,
        socket_timeout_ms: int = 30000,
        frame_wait_timeout_ms: int = 250,
        reconnect_delay_ms: int = 120,
    ) -> None:
        self._data_socket_path = data_socket_path
        self._socket_timeout_ms = max(1, int(socket_timeout_ms))
        self._frame_wait_timeout_ms = max(1, int(frame_wait_timeout_ms))
        self._reconnect_delay_ms = max(1, int(reconnect_delay_ms))

        self._touch_reader: Optional[asyncio.StreamReader] = None
        self._touch_writer: Optional[asyncio.StreamWriter] = None
        self._touch_lock = asyncio.Lock()

    async def close(self) -> None:
        """关闭触控流连接。"""
        await self._reset_touch_stream()

    async def get_raw_frame(
        self,
        last_frame_seq: int = 0,
        wait_timeout_ms: Optional[int] = None,
    ) -> RawFrame:
        """
        拉取一帧 RGBA 原始像素数据。

        协议流程：
        1. 发送 `RENDER_FRAME_BIND_SHM`
        2. 接收 `OK SHM_BOUND <capacity> <offset>` + SCM_RIGHTS fd
        3. 发送 `RENDER_FRAME_WAIT_SHM_PRESENT <last_seq> <timeout_ms>`
        4. 接收 `OK <frame_seq> <w> <h> RGBA8888 <byte_len> <checksum> <offset>`
        5. 从 mmap(fd) 对应偏移读取 RGBA 字节流
        """
        timeout = (
            max(1, int(wait_timeout_ms))
            if wait_timeout_ms is not None
            else self._frame_wait_timeout_ms
        )
        return await asyncio.to_thread(self._pull_frame_sync, int(last_frame_seq), timeout)

    async def touch_down(self, pointer_id: int, x: float, y: float) -> None:
        """触点按下（TOUCH_DOWN 语义）。"""
        await self._send_touch_packet(TOUCH_PACKET_DOWN, pointer_id, x, y)

    async def touch_move(self, pointer_id: int, x: float, y: float) -> None:
        """触点移动（TOUCH_MOVE 语义）。"""
        await self._send_touch_packet(TOUCH_PACKET_MOVE, pointer_id, x, y)

    async def touch_up(self, pointer_id: int, x: float = 0.0, y: float = 0.0) -> None:
        """触点抬起（TOUCH_UP 语义）。"""
        await self._send_touch_packet(TOUCH_PACKET_UP, pointer_id, x, y)

    async def touch_cancel(self, pointer_id: int) -> None:
        """触点取消（TOUCH_CANCEL 语义）。"""
        await self._send_touch_packet(TOUCH_PACKET_CANCEL, pointer_id, 0.0, 0.0)

    async def clear_touches(self) -> None:
        """清空所有活动触点（TOUCH_CLEAR 语义）。"""
        await self._send_touch_packet(TOUCH_PACKET_CLEAR, 0, 0.0, 0.0)

    async def send_touch_text_command(
        self,
        command: str,
        pointer_id: int,
        x: float = 0.0,
        y: float = 0.0,
    ) -> None:
        """
        兼容文本命令风格的触控入口。

        支持：
        - TOUCH_DOWN <pointer_id> <x> <y>
        - TOUCH_MOVE <pointer_id> <x> <y>
        - TOUCH_UP   <pointer_id> <x> <y>
        - TOUCH_CANCEL <pointer_id>
        """
        cmd = command.strip().upper()
        if cmd == "TOUCH_DOWN":
            await self.touch_down(pointer_id, x, y)
            return
        if cmd == "TOUCH_MOVE":
            await self.touch_move(pointer_id, x, y)
            return
        if cmd == "TOUCH_UP":
            await self.touch_up(pointer_id, x, y)
            return
        if cmd == "TOUCH_CANCEL":
            await self.touch_cancel(pointer_id)
            return
        raise ValueError(f"unsupported_touch_command:{command}")

    async def _send_touch_packet(
        self,
        kind: int,
        pointer_id: int,
        x: float,
        y: float,
    ) -> None:
        """
        发送 16B 触控包，并在连接断开时自动重连后重试一次。
        """
        packet = self._encode_touch_packet(kind, pointer_id, x, y)
        async with self._touch_lock:
            last_error: Optional[BaseException] = None
            for attempt in range(2):
                try:
                    await self._ensure_touch_stream()
                    assert self._touch_writer is not None
                    self._touch_writer.write(packet)
                    await self._touch_writer.drain()
                    return
                except (
                    BrokenPipeError,
                    ConnectionError,
                    OSError,
                    RuntimeError,
                    asyncio.IncompleteReadError,
                ) as exc:
                    last_error = exc
                    await self._reset_touch_stream()
                    if attempt == 0:
                        await asyncio.sleep(self._reconnect_delay_ms / 1000.0)
                        continue
            raise DirectScreenProtocolError(f"touch_stream_write_failed:{last_error}")

    async def _ensure_touch_stream(self) -> None:
        """确保触控流连接存在，若不存在则建立握手。"""
        if self._touch_writer is not None and not self._touch_writer.is_closing():
            return
        await self._connect_touch_stream_once()

    async def _connect_touch_stream_once(self) -> None:
        """
        建立 STREAM_TOUCH_V1 长连接。

        握手协议：
        - C -> S: STREAM_TOUCH_V1\\n
        - S -> C: OK STREAM_TOUCH_V1\\n
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._data_socket_path),
            timeout=self._socket_timeout_ms / 1000.0,
        )
        writer.write(b"STREAM_TOUCH_V1\n")
        await writer.drain()

        line = await asyncio.wait_for(
            reader.readline(), timeout=self._socket_timeout_ms / 1000.0
        )
        if not line:
            writer.close()
            await writer.wait_closed()
            raise DirectScreenProtocolError("touch_stream_handshake_eof")

        if line.strip() != b"OK STREAM_TOUCH_V1":
            writer.close()
            await writer.wait_closed()
            raise DirectScreenProtocolError(
                f"touch_stream_handshake_bad:{line.decode('utf-8', 'replace').strip()}"
            )

        self._touch_reader = reader
        self._touch_writer = writer

    async def _reset_touch_stream(self) -> None:
        """释放当前触控连接。"""
        writer = self._touch_writer
        self._touch_reader = None
        self._touch_writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            # 关闭阶段的异常无需向上抛，避免覆盖主异常。
            pass

    @staticmethod
    def _encode_touch_packet(kind: int, pointer_id: int, x: float, y: float) -> bytes:
        """
        将触控命令编码为 STREAM_TOUCH_V1 16B 包。

        布局（小端）：
        - [0]   kind
        - [1:4] 保留
        - [4:8] pointer_id (i32)
        - [8:12] x (f32)
        - [12:16] y (f32)
        """
        if pointer_id < 0:
            raise ValueError("pointer_id_must_be_non_negative")
        if not isfinite(x) or not isfinite(y):
            raise ValueError("touch_coordinate_must_be_finite")
        return struct.pack("<B3xiff", int(kind), int(pointer_id), float(x), float(y))

    def _pull_frame_sync(self, last_frame_seq: int, wait_timeout_ms: int) -> RawFrame:
        """
        同步取帧逻辑（由 asyncio.to_thread 调用）。
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._socket_timeout_ms / 1000.0)

        bound_fd: Optional[int] = None
        mapping: Optional[mmap.mmap] = None
        try:
            sock.connect(self._data_socket_path)

            sock.sendall(b"RENDER_FRAME_BIND_SHM\n")
            bind_line, bound_fd = self._recv_line_with_optional_fd(sock, require_fd=True)
            if bound_fd is None:
                raise DirectScreenProtocolError(
                    f"bind_missing_fd line='{bind_line}'"
                )

            capacity, bind_offset = self._parse_bind_response(bind_line)
            map_len = capacity + bind_offset
            mapping = mmap.mmap(bound_fd, map_len, prot=mmap.PROT_READ, flags=mmap.MAP_SHARED)

            # mmap 之后 fd 可以关闭，映射仍然有效。
            os.close(bound_fd)
            bound_fd = None

            wait_cmd = f"RENDER_FRAME_WAIT_SHM_PRESENT {last_frame_seq} {wait_timeout_ms}\n"
            sock.sendall(wait_cmd.encode("ascii"))
            wait_line, _ = self._recv_line_with_optional_fd(sock, require_fd=False)
            wait_meta = self._parse_wait_response(wait_line)
            if wait_meta is None:
                raise FrameTimeoutError("frame_wait_timeout")

            if wait_meta.pixel_format != "RGBA8888":
                raise DirectScreenProtocolError(
                    f"unsupported_pixel_format:{wait_meta.pixel_format}"
                )

            expected_len = wait_meta.width * wait_meta.height * 4
            if wait_meta.byte_len != expected_len:
                raise DirectScreenProtocolError(
                    f"byte_len_mismatch expected={expected_len} actual={wait_meta.byte_len}"
                )

            if wait_meta.offset < 0 or wait_meta.offset + wait_meta.byte_len > map_len:
                raise DirectScreenProtocolError("invalid_offset_or_length")

            start = wait_meta.offset
            end = start + wait_meta.byte_len
            rgba_bytes = bytes(mapping[start:end])
            if len(rgba_bytes) != wait_meta.byte_len:
                raise DirectScreenProtocolError("frame_copy_size_mismatch")

            return RawFrame(
                frame_seq=wait_meta.frame_seq,
                width=wait_meta.width,
                height=wait_meta.height,
                pixel_format=wait_meta.pixel_format,
                byte_len=wait_meta.byte_len,
                checksum=wait_meta.checksum,
                rgba=rgba_bytes,
            )
        finally:
            if mapping is not None:
                mapping.close()
            if bound_fd is not None:
                try:
                    os.close(bound_fd)
                except OSError:
                    pass
            sock.close()

    @staticmethod
    def _recv_line_with_optional_fd(
        sock: socket.socket,
        require_fd: bool,
    ) -> Tuple[str, Optional[int]]:
        """
        读取一行文本头，并可选地接收一个 SCM_RIGHTS fd。
        """
        line = bytearray()
        line_done = False
        line_text = ""
        received_fd: Optional[int] = None
        fd_size = array.array("i").itemsize
        ancbuf_size = socket.CMSG_SPACE(fd_size * 4)

        while True:
            try:
                data, ancdata, _, _ = sock.recvmsg(1024, ancbuf_size)
            except TimeoutError as exc:
                raise FrameTimeoutError("socket_timeout") from exc

            if not data and not ancdata:
                if line_done:
                    raise DirectScreenProtocolError(f"bind_missing_fd line='{line_text}'")
                raise DirectScreenProtocolError("header_eof")

            for level, ctype, cdata in ancdata:
                if level != socket.SOL_SOCKET or ctype != socket.SCM_RIGHTS:
                    continue
                fds = array.array("i")
                fds.frombytes(cdata[: len(cdata) - (len(cdata) % fd_size)])
                for fd in fds:
                    fd_int = int(fd)
                    if received_fd is None:
                        received_fd = fd_int
                    else:
                        # 除第一个 fd 外的附带 fd 立即释放，避免泄漏。
                        try:
                            os.close(fd_int)
                        except OSError:
                            pass

            for byte in data:
                if line_done:
                    continue
                if byte == 10:  # '\n'
                    line_text = line.decode("utf-8", "replace").strip()
                    line_done = True
                    continue
                if byte != 13:  # '\r'
                    line.append(byte)
                if len(line) > MAX_HEADER_BYTES:
                    raise DirectScreenProtocolError("header_too_long")

            if line_done and (not require_fd or received_fd is not None):
                return line_text, received_fd

    @staticmethod
    def _parse_bind_response(line: str) -> Tuple[int, int]:
        parts = line.split()
        if len(parts) != 4 or parts[0] != "OK" or parts[1] != "SHM_BOUND":
            raise DirectScreenProtocolError(f"bad_bind_header line='{line}'")
        try:
            capacity = int(parts[2])
            offset = int(parts[3])
        except ValueError as exc:
            raise DirectScreenProtocolError("invalid_bind_layout") from exc
        if capacity <= 0 or offset < 0:
            raise DirectScreenProtocolError("invalid_bind_layout")
        return capacity, offset

    @staticmethod
    def _parse_wait_response(line: str) -> Optional[_WaitFrameMeta]:
        parts = line.split()
        if len(parts) == 2 and parts[0] == "OK" and parts[1] == "TIMEOUT":
            return None
        if len(parts) != 8 or parts[0] != "OK":
            raise DirectScreenProtocolError(f"bad_wait_header line='{line}'")

        try:
            frame_seq = int(parts[1])
            width = int(parts[2])
            height = int(parts[3])
            pixel_format = parts[4]
            byte_len = int(parts[5])
            checksum = int(parts[6])
            offset = int(parts[7])
        except ValueError as exc:
            raise DirectScreenProtocolError("bad_wait_payload") from exc

        if width <= 0 or height <= 0 or byte_len <= 0 or offset < 0:
            raise DirectScreenProtocolError("invalid_wait_payload")

        return _WaitFrameMeta(
            frame_seq=frame_seq,
            width=width,
            height=height,
            pixel_format=pixel_format,
            byte_len=byte_len,
            checksum=checksum,
            offset=offset,
        )
