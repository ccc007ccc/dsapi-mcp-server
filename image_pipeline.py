from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from PIL import Image, ImageDraw, ImageFont

from dsapi_client import RawFrame


@dataclass(frozen=True)
class ProcessedImage:
    """图像流水线输出结果。"""

    jpeg_bytes: bytes
    scale_factor: float
    original_width: int
    original_height: int
    output_width: int
    output_height: int
    jpeg_quality: int


def process_frame_to_image(frame: RawFrame, image_cfg: Dict[str, Any]) -> ProcessedImage:
    """
    原始帧处理主流程：
    1. RGBA 字节 -> PIL Image
    2. 绘制红色边缘坐标轴刻度（可开关）
    3. 按最大长边等比缩放
    4. JPEG 压缩（内存字节流）
    """
    image = Image.frombytes("RGBA", (frame.width, frame.height), frame.rgba, "raw", "RGBA")

    if _cfg_bool(image_cfg, "enable_axis", True):
        _draw_axis_ticks(
            image=image,
            axis_step=_cfg_int(image_cfg, "axis_step", 100, min_value=1),
            tick_size=_cfg_int(image_cfg, "axis_tick_size", 10, min_value=1),
            line_width=_cfg_int(image_cfg, "axis_line_width", 2, min_value=1),
        )

    max_long_edge = _cfg_int(image_cfg, "max_long_edge", 1024, min_value=1)
    scale_factor, output_size = _compute_scale(frame.width, frame.height, max_long_edge)
    if output_size != (frame.width, frame.height):
        image = image.resize(output_size, Image.Resampling.LANCZOS)

    jpeg_quality = _cfg_int(image_cfg, "jpeg_quality", 80, min_value=1, max_value=95)
    rgb_image = image.convert("RGB")
    jpeg_bytes = _to_jpeg_bytes(rgb_image, jpeg_quality)

    return ProcessedImage(
        jpeg_bytes=jpeg_bytes,
        scale_factor=scale_factor,
        original_width=frame.width,
        original_height=frame.height,
        output_width=output_size[0],
        output_height=output_size[1],
        jpeg_quality=jpeg_quality,
    )


def process_frame_to_base64(frame: RawFrame, image_cfg: Dict[str, Any]) -> ProcessedImage:
    """
    兼容旧函数名：
    历史上此函数返回 base64，但当前实现已经升级为内存 JPEG 字节。
    为避免外部调用报错，保留同名别名并委托到新函数。
    """
    return process_frame_to_image(frame, image_cfg)


def _draw_axis_ticks(
    image: Image.Image,
    axis_step: int,
    tick_size: int,
    line_width: int,
) -> None:
    """
    在图像四条边绘制红色刻度与坐标标签。

    目标是让大模型能直接读取坐标位置，减少“看图估点”的偏差。
    """
    width, height = image.size
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    color = (255, 0, 0, 255)

    # 先画外边框，形成明显“坐标边界”。
    draw.rectangle((0, 0, width - 1, height - 1), outline=color, width=line_width)

    # X 轴刻度：顶部 + 底部。
    for x in range(0, width, axis_step):
        draw.line([(x, 0), (x, min(height - 1, tick_size))], fill=color, width=line_width)
        draw.line(
            [(x, height - 1), (x, max(0, height - 1 - tick_size))],
            fill=color,
            width=line_width,
        )

        label = str(x)
        draw.text((min(x + 2, max(0, width - 40)), 2), label, fill=color, font=font)

    # Y 轴刻度：左侧 + 右侧。
    for y in range(0, height, axis_step):
        draw.line([(0, y), (min(width - 1, tick_size), y)], fill=color, width=line_width)
        draw.line(
            [(width - 1, y), (max(0, width - 1 - tick_size), y)],
            fill=color,
            width=line_width,
        )

        label = str(y)
        draw.text((2, min(y + 2, max(0, height - 14))), label, fill=color, font=font)
        draw.text(
            (max(0, width - 38), min(y + 2, max(0, height - 14))),
            label,
            fill=color,
            font=font,
        )


def _compute_scale(width: int, height: int, max_long_edge: int) -> Tuple[float, Tuple[int, int]]:
    """
    计算等比缩放比例。

    scale_factor 定义为：
    - 缩放后尺寸 / 原始尺寸
    """
    long_edge = max(width, height)
    if long_edge <= max_long_edge:
        return 1.0, (width, height)

    scale = max_long_edge / float(long_edge)
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    return scale, (new_w, new_h)


def _to_jpeg_bytes(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


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
