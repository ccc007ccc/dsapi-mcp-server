from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import mcp.types as mcp_types
import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image as MCPImage

from dsapi_client import DirectScreenClient, FrameTimeoutError
from image_pipeline import process_frame_to_image


def _load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("config_yaml_must_be_mapping")
    return cfg


def _cfg_int(
    cfg: Dict[str, Any],
    key: str,
    default: int,
    min_value: int = 0,
    max_value: int = 2**31 - 1,
) -> int:
    try:
        value = int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


def _cfg_bool(cfg: Dict[str, Any], key: str, default: bool) -> bool:
    value = cfg.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _cfg_str(cfg: Dict[str, Any], key: str, default: str) -> str:
    value = cfg.get(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _resolve_config_relative_path(base_dir: Path, value: str) -> str:
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return str(raw)
    return str((base_dir / raw).resolve())


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _calc_steps(duration_ms: int) -> int:
    # 以约 60FPS 的节奏插值，保证滑动/缩放轨迹平滑。
    return max(2, min(240, max(1, duration_ms // 16)))


class RuntimeContext:
    """
    运行时状态容器。

    关键职责：
    1. 保存最近一次屏幕回传的 scale_factor。
    2. 将模型坐标（压缩图坐标）还原为物理坐标（原始屏幕坐标）。
    """

    def __init__(self, config: Dict[str, Any], config_dir: Path) -> None:
        self.config = config
        self.config_dir = config_dir
        uds_cfg = config.get("uds", {})
        timeout_cfg = config.get("timeouts", {})
        image_cfg = config.get("image", {})
        gesture_cfg = config.get("gesture", {})
        system_cfg = config.get("system_control", {})

        self.image_cfg: Dict[str, Any] = image_cfg if isinstance(image_cfg, dict) else {}
        self.gesture_cfg: Dict[str, Any] = gesture_cfg if isinstance(gesture_cfg, dict) else {}
        self.system_cfg: Dict[str, Any] = system_cfg if isinstance(system_cfg, dict) else {}
        self.frame_wait_timeout_ms = _cfg_int(
            timeout_cfg if isinstance(timeout_cfg, dict) else {},
            "frame_wait_timeout_ms",
            250,
            min_value=1,
        )
        self.system_cmd_timeout_ms = _cfg_int(
            self.system_cfg,
            "command_timeout_ms",
            3000,
            min_value=100,
            max_value=120000,
        )
        self.system_enabled = _cfg_bool(self.system_cfg, "enable", True)
        self.system_use_su = _cfg_bool(self.system_cfg, "use_su", True)
        self.system_su_bin = _cfg_str(self.system_cfg, "su_bin", "su")
        self.system_input_bin = _cfg_str(self.system_cfg, "input_bin", "input")
        keycodes_cfg = self.system_cfg.get("keycodes", {})
        keycodes_cfg = keycodes_cfg if isinstance(keycodes_cfg, dict) else {}
        self.keycodes = {
            "back": _cfg_str(keycodes_cfg, "back", "KEYCODE_BACK"),
            "home": _cfg_str(keycodes_cfg, "home", "KEYCODE_HOME"),
            "recents": _cfg_str(keycodes_cfg, "recents", "KEYCODE_APP_SWITCH"),
            "volume_up": _cfg_str(keycodes_cfg, "volume_up", "KEYCODE_VOLUME_UP"),
            "volume_down": _cfg_str(keycodes_cfg, "volume_down", "KEYCODE_VOLUME_DOWN"),
        }

        data_socket = "artifacts/run/dsapi.data.sock"
        if isinstance(uds_cfg, dict):
            data_socket = str(uds_cfg.get("data_socket", data_socket))
        data_socket = _resolve_config_relative_path(self.config_dir, data_socket)

        self.client = DirectScreenClient(
            data_socket_path=data_socket,
            socket_timeout_ms=_cfg_int(
                timeout_cfg if isinstance(timeout_cfg, dict) else {},
                "socket_timeout_ms",
                30000,
                min_value=1,
            ),
            frame_wait_timeout_ms=self.frame_wait_timeout_ms,
            reconnect_delay_ms=_cfg_int(
                timeout_cfg if isinstance(timeout_cfg, dict) else {},
                "reconnect_delay_ms",
                120,
                min_value=1,
            ),
        )

        self._state_lock = asyncio.Lock()
        self._last_scale_factor: float = 1.0
        self._last_width: int = 0
        self._last_height: int = 0
        self._last_frame_seq: int = 0

    async def update_screen_state(
        self,
        width: int,
        height: int,
        frame_seq: int,
        scale_factor: float,
    ) -> None:
        async with self._state_lock:
            self._last_width = int(width)
            self._last_height = int(height)
            self._last_frame_seq = int(frame_seq)
            self._last_scale_factor = scale_factor if scale_factor > 0 else 1.0

    async def snapshot_transform(self) -> Tuple[float, int, int, int]:
        async with self._state_lock:
            scale = self._last_scale_factor if self._last_scale_factor > 0 else 1.0
            return scale, self._last_width, self._last_height, self._last_frame_seq

    @staticmethod
    def model_to_physical_point(
        x: float,
        y: float,
        scale_factor: float,
        width: int,
        height: int,
    ) -> Tuple[float, float]:
        """
        坐标还原规则：
        physical = model / scale_factor
        """
        px = float(x) / scale_factor
        py = float(y) / scale_factor

        # 若已知屏幕边界，则做夹紧，避免坐标越界。
        if width > 0:
            px = max(0.0, min(px, float(width - 1)))
        if height > 0:
            py = max(0.0, min(py, float(height - 1)))
        return px, py

    @staticmethod
    def model_to_physical_distance(distance: float, scale_factor: float) -> float:
        return max(1.0, float(distance) / scale_factor)

    def build_keyevent_command(self, keycode: str) -> List[str]:
        if self.system_use_su:
            return [self.system_su_bin, "-c", f"{self.system_input_bin} keyevent {keycode}"]
        return [self.system_input_bin, "keyevent", keycode]

    async def send_keyevent(self, keycode: str) -> Dict[str, Any]:
        if not self.system_enabled:
            raise RuntimeError("system_control_disabled")

        cmd = self.build_keyevent_command(keycode)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.system_cmd_timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"keyevent_timeout:{keycode}")

        out = (stdout.decode("utf-8", "replace") if stdout else "").strip()
        err = (stderr.decode("utf-8", "replace") if stderr else "").strip()
        return {
            "return_code": proc.returncode,
            "stdout": out,
            "stderr": err,
            "command": cmd,
        }


async def _safe_touch_up(pointer_id: int, x: float, y: float) -> None:
    try:
        await RUNTIME.client.touch_up(pointer_id, x, y)
    except Exception:
        # 兜底释放触点失败时仅吞掉异常，避免覆盖主链路异常。
        pass


async def _system_key_action(action: str, steps: int = 1, interval_ms: int = 80) -> Dict[str, Any]:
    """
    执行系统按键动作（返回/home/最近任务/音量键）。
    """
    if action not in RUNTIME.keycodes:
        raise ValueError(f"unsupported_system_action:{action}")

    safe_steps = max(1, min(100, int(steps)))
    safe_interval_ms = max(0, min(5000, int(interval_ms)))
    keycode = RUNTIME.keycodes[action]

    last_result: Dict[str, Any] = {}
    for idx in range(safe_steps):
        last_result = await RUNTIME.send_keyevent(keycode)
        if last_result.get("return_code", 1) != 0:
            return {
                "status": "error",
                "action": action,
                "keycode": keycode,
                "step": idx + 1,
                "steps": safe_steps,
                "result": last_result,
            }
        if idx < safe_steps - 1 and safe_interval_ms > 0:
            await asyncio.sleep(safe_interval_ms / 1000.0)

    return {
        "status": "ok",
        "action": action,
        "keycode": keycode,
        "steps": safe_steps,
        "result": last_result,
    }


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
CONFIG_PATH = Path(os.environ.get("DSAPI_MCP_CONFIG", str(DEFAULT_CONFIG_PATH))).expanduser()
CONFIG = _load_config(CONFIG_PATH)
RUNTIME = RuntimeContext(CONFIG, CONFIG_PATH.resolve().parent)
mcp = FastMCP("dsapi-mcp-server")


@mcp.tool()
async def get_annotated_screen() -> mcp_types.CallToolResult:
    """
    拉取并处理一帧屏幕图像，返回 MCP 原生 image 内容 + 结构化元信息。
    """
    try:
        raw = await RUNTIME.client.get_raw_frame(
            last_frame_seq=0, wait_timeout_ms=RUNTIME.frame_wait_timeout_ms
        )
    except FrameTimeoutError:
        payload = {
            "status": "timeout",
            "message": "等待屏幕帧超时，请稍后重试。",
        }
        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            ],
            structuredContent=payload,
            isError=False,
        )

    processed = process_frame_to_image(raw, RUNTIME.image_cfg)
    await RUNTIME.update_screen_state(
        width=raw.width,
        height=raw.height,
        frame_seq=raw.frame_seq,
        scale_factor=processed.scale_factor,
    )

    payload = {
        "status": "ok",
        "scale_factor": processed.scale_factor,
        "screen": {
            "frame_seq": raw.frame_seq,
            "pixel_format": raw.pixel_format,
            "checksum": raw.checksum,
            "original_width": processed.original_width,
            "original_height": processed.original_height,
            "scaled_width": processed.output_width,
            "scaled_height": processed.output_height,
            "jpeg_quality": processed.jpeg_quality,
        },
    }
    image_block = MCPImage(data=processed.jpeg_bytes, format="jpeg").to_image_content()
    text_block = mcp_types.TextContent(
        type="text",
        text=json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return mcp_types.CallToolResult(
        content=[image_block, text_block],
        structuredContent=payload,
        isError=False,
    )


@mcp.tool()
async def tap(x: float, y: float) -> Dict[str, Any]:
    """
    单击：DOWN -> UP。
    """
    scale, width, height, _ = await RUNTIME.snapshot_transform()
    px, py = RUNTIME.model_to_physical_point(x, y, scale, width, height)

    await RUNTIME.client.touch_down(0, px, py)
    try:
        await asyncio.sleep(0.03)
    finally:
        await _safe_touch_up(0, px, py)

    return {
        "status": "ok",
        "scale_factor_used": scale,
        "physical_x": px,
        "physical_y": py,
    }


@mcp.tool()
async def long_press(x: float, y: float, duration_ms: int = 1000) -> Dict[str, Any]:
    """
    长按：DOWN -> sleep -> UP。
    """
    duration = _cfg_int(
        {"duration": duration_ms},
        "duration",
        _cfg_int(RUNTIME.gesture_cfg, "default_long_press_duration_ms", 1000, min_value=1),
        min_value=1,
        max_value=60000,
    )

    scale, width, height, _ = await RUNTIME.snapshot_transform()
    px, py = RUNTIME.model_to_physical_point(x, y, scale, width, height)

    await RUNTIME.client.touch_down(0, px, py)
    try:
        await asyncio.sleep(duration / 1000.0)
    finally:
        await _safe_touch_up(0, px, py)

    return {
        "status": "ok",
        "scale_factor_used": scale,
        "duration_ms": duration,
        "physical_x": px,
        "physical_y": py,
    }


@mcp.tool()
async def swipe(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    duration_ms: int = 300,
) -> Dict[str, Any]:
    """
    滑动：DOWN -> 多段 MOVE -> UP。
    """
    duration = _cfg_int(
        {"duration": duration_ms},
        "duration",
        _cfg_int(RUNTIME.gesture_cfg, "default_swipe_duration_ms", 300, min_value=1),
        min_value=1,
        max_value=60000,
    )

    scale, width, height, _ = await RUNTIME.snapshot_transform()
    sx, sy = RUNTIME.model_to_physical_point(start_x, start_y, scale, width, height)
    ex, ey = RUNTIME.model_to_physical_point(end_x, end_y, scale, width, height)

    steps = _calc_steps(duration)
    interval_s = duration / 1000.0 / steps

    await RUNTIME.client.touch_down(0, sx, sy)
    try:
        for i in range(1, steps + 1):
            t = i / steps
            mx = _lerp(sx, ex, t)
            my = _lerp(sy, ey, t)
            await RUNTIME.client.touch_move(0, mx, my)
            if i < steps:
                await asyncio.sleep(interval_s)
    finally:
        await _safe_touch_up(0, ex, ey)

    return {
        "status": "ok",
        "scale_factor_used": scale,
        "duration_ms": duration,
        "steps": steps,
        "physical_start": {"x": sx, "y": sy},
        "physical_end": {"x": ex, "y": ey},
    }


@mcp.tool()
async def drag_and_drop(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    hold_ms: int = 500,
    duration_ms: int = 500,
) -> Dict[str, Any]:
    """
    拖拽：同点 DOWN -> hold -> 多段 MOVE -> UP。
    """
    hold = _cfg_int(
        {"hold": hold_ms},
        "hold",
        _cfg_int(RUNTIME.gesture_cfg, "default_drag_hold_ms", 500, min_value=1),
        min_value=1,
        max_value=60000,
    )
    duration = _cfg_int(
        {"duration": duration_ms},
        "duration",
        _cfg_int(RUNTIME.gesture_cfg, "default_drag_duration_ms", 500, min_value=1),
        min_value=1,
        max_value=60000,
    )

    scale, width, height, _ = await RUNTIME.snapshot_transform()
    sx, sy = RUNTIME.model_to_physical_point(start_x, start_y, scale, width, height)
    ex, ey = RUNTIME.model_to_physical_point(end_x, end_y, scale, width, height)

    steps = _calc_steps(duration)
    interval_s = duration / 1000.0 / steps

    await RUNTIME.client.touch_down(0, sx, sy)
    try:
        await asyncio.sleep(hold / 1000.0)
        for i in range(1, steps + 1):
            t = i / steps
            mx = _lerp(sx, ex, t)
            my = _lerp(sy, ey, t)
            await RUNTIME.client.touch_move(0, mx, my)
            if i < steps:
                await asyncio.sleep(interval_s)
    finally:
        await _safe_touch_up(0, ex, ey)

    return {
        "status": "ok",
        "scale_factor_used": scale,
        "hold_ms": hold,
        "duration_ms": duration,
        "steps": steps,
        "physical_start": {"x": sx, "y": sy},
        "physical_end": {"x": ex, "y": ey},
    }


@mcp.tool()
async def zoom(
    center_x: float,
    center_y: float,
    direction: str,
    distance: int = 400,
    duration_ms: int = 300,
) -> Dict[str, Any]:
    """
    双指缩放。

    direction:
    - "in"  : 两指向中心移动（缩小）
    - "out" : 两指向外移动（放大）
    """
    direction_norm = direction.strip().lower()
    if direction_norm not in {"in", "out"}:
        raise ValueError("direction_must_be_in_or_out")

    duration = _cfg_int(
        {"duration": duration_ms},
        "duration",
        _cfg_int(RUNTIME.gesture_cfg, "zoom_default_duration_ms", 300, min_value=1),
        min_value=1,
        max_value=60000,
    )
    default_distance = _cfg_int(
        RUNTIME.gesture_cfg, "zoom_default_distance", 400, min_value=2
    )
    requested_distance = _cfg_int(
        {"distance": distance},
        "distance",
        default_distance,
        min_value=2,
        max_value=10000,
    )

    scale, width, height, _ = await RUNTIME.snapshot_transform()
    cx, cy = RUNTIME.model_to_physical_point(center_x, center_y, scale, width, height)
    physical_distance = RuntimeContext.model_to_physical_distance(
        requested_distance, scale
    )

    half = physical_distance / 2.0
    inner = max(2.0, half * 0.25)

    if direction_norm == "out":
        p1_start = (cx - inner, cy)
        p1_end = (cx - half, cy)
        p2_start = (cx + inner, cy)
        p2_end = (cx + half, cy)
    else:
        p1_start = (cx - half, cy)
        p1_end = (cx - inner, cy)
        p2_start = (cx + half, cy)
        p2_end = (cx + inner, cy)

    p1sx, p1sy = RUNTIME.model_to_physical_point(p1_start[0], p1_start[1], 1.0, width, height)
    p1ex, p1ey = RUNTIME.model_to_physical_point(p1_end[0], p1_end[1], 1.0, width, height)
    p2sx, p2sy = RUNTIME.model_to_physical_point(p2_start[0], p2_start[1], 1.0, width, height)
    p2ex, p2ey = RUNTIME.model_to_physical_point(p2_end[0], p2_end[1], 1.0, width, height)

    pointer_a = 10
    pointer_b = 11
    steps = _calc_steps(duration)
    interval_s = duration / 1000.0 / steps

    await RUNTIME.client.touch_down(pointer_a, p1sx, p1sy)
    try:
        await RUNTIME.client.touch_down(pointer_b, p2sx, p2sy)
        try:
            # 每轮先发指针 A，再发指针 B，满足“并发/交替发送 MOVE”的要求。
            for i in range(1, steps + 1):
                t = i / steps
                a_x = _lerp(p1sx, p1ex, t)
                a_y = _lerp(p1sy, p1ey, t)
                b_x = _lerp(p2sx, p2ex, t)
                b_y = _lerp(p2sy, p2ey, t)
                await RUNTIME.client.touch_move(pointer_a, a_x, a_y)
                await RUNTIME.client.touch_move(pointer_b, b_x, b_y)
                if i < steps:
                    await asyncio.sleep(interval_s)
        finally:
            await _safe_touch_up(pointer_b, p2ex, p2ey)
    finally:
        await _safe_touch_up(pointer_a, p1ex, p1ey)

    return {
        "status": "ok",
        "direction": direction_norm,
        "scale_factor_used": scale,
        "duration_ms": duration,
        "physical_distance": physical_distance,
        "steps": steps,
        "pointer_a": {"id": pointer_a, "start": {"x": p1sx, "y": p1sy}, "end": {"x": p1ex, "y": p1ey}},
        "pointer_b": {"id": pointer_b, "start": {"x": p2sx, "y": p2sy}, "end": {"x": p2ex, "y": p2ey}},
    }


@mcp.tool()
async def system_nav(
    action: str,
    repeat: int = 1,
    interval_ms: int = 80,
) -> Dict[str, Any]:
    """
    三合一系统导航按键工具。

    action 支持：
    - "back"
    - "home"
    - "recents" / "recent_tasks" / "recent"
    """
    action_raw = action.strip().lower()
    action_map = {
        "back": "back",
        "home": "home",
        "recent": "recents",
        "recents": "recents",
        "recent_tasks": "recents",
        "app_switch": "recents",
    }
    action_key = action_map.get(action_raw)
    if action_key is None:
        raise ValueError("action_must_be_back_home_or_recents")
    return await _system_key_action(action_key, steps=repeat, interval_ms=interval_ms)


@mcp.tool()
async def volume_up(steps: int = 1, interval_ms: int = 80) -> Dict[str, Any]:
    """音量增加。"""
    return await _system_key_action("volume_up", steps=steps, interval_ms=interval_ms)


@mcp.tool()
async def volume_down(steps: int = 1, interval_ms: int = 80) -> Dict[str, Any]:
    """音量减少。"""
    return await _system_key_action("volume_down", steps=steps, interval_ms=interval_ms)


if __name__ == "__main__":
    mcp.run()
