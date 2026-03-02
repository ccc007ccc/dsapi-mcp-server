from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

import PIL
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
    2. 按最大长边等比缩放
    3. 可选锐化（提升文字/边缘可读性）
    4. 绘制边缘坐标轴刻度（可开关）
    5. JPEG 压缩（内存字节流）
    """
    image = Image.frombytes("RGBA", (frame.width, frame.height), frame.rgba, "raw", "RGBA")

    max_long_edge = _cfg_int(image_cfg, "max_long_edge", 1600, min_value=1)
    scale_factor, output_size = _compute_scale(frame.width, frame.height, max_long_edge)
    if output_size != (frame.width, frame.height):
        image = image.resize(output_size, Image.Resampling.LANCZOS)

    if _cfg_bool(image_cfg, "enable_sharpen", True):
        image = image.filter(
            ImageFilter.UnsharpMask(
                radius=_cfg_float(image_cfg, "sharpen_radius", 1.2, min_value=0.1, max_value=5.0),
                percent=_cfg_int(image_cfg, "sharpen_percent", 135, min_value=0, max_value=500),
                threshold=_cfg_int(image_cfg, "sharpen_threshold", 3, min_value=0, max_value=255),
            )
        )

    if _cfg_bool(image_cfg, "enable_axis", True):
        _draw_axis_ticks(
            image=image,
            axis_step=_cfg_int(image_cfg, "axis_step", 100, min_value=1),
            tick_size=_cfg_int(image_cfg, "axis_tick_size", 26, min_value=1),
            line_width=_cfg_int(image_cfg, "axis_line_width", 4, min_value=1),
            font_size=_cfg_int(image_cfg, "axis_font_size", 0, min_value=0),
        )

    jpeg_quality = _cfg_int(image_cfg, "jpeg_quality", 90, min_value=1, max_value=95)
    jpeg_subsampling = _cfg_int(image_cfg, "jpeg_subsampling", 0, min_value=0, max_value=2)
    rgb_image = image.convert("RGB")
    jpeg_bytes = _to_jpeg_bytes(rgb_image, jpeg_quality, jpeg_subsampling)

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
    font_size: int,
) -> None:
    """
    在图像四条边绘制红色刻度与坐标标签。

    目标是让大模型能直接读取坐标位置，减少“看图估点”的偏差。
    """
    width, height = image.size
    draw = ImageDraw.Draw(image)
    font = _load_axis_font(width, height, font_size)
    color = (255, 0, 0, 255)
    label_color = (255, 255, 0, 255)
    stroke_fill = (0, 0, 0, 255)
    stroke_width = max(1, line_width // 2)

    # 先画外边框，形成明显“坐标边界”。
    draw.rectangle((0, 0, width - 1, height - 1), outline=color, width=line_width)

    # X 轴刻度：顶部 + 底部。
    min_x_label_gap = max(axis_step // 2, tick_size * 2)
    next_x_label_at = 0
    for x in range(0, width, axis_step):
        draw.line([(x, 0), (x, min(height - 1, tick_size))], fill=color, width=line_width)
        draw.line(
            [(x, height - 1), (x, max(0, height - 1 - tick_size))],
            fill=color,
            width=line_width,
        )

        if x >= next_x_label_at:
            label = str(x)
            bbox = draw.textbbox((0, 0), label, font=font, stroke_width=stroke_width)
            label_w = max(1, bbox[2] - bbox[0])
            top_y = 2
            bottom_y = max(0, height - (bbox[3] - bbox[1]) - 2)
            text_x = min(max(0, x + 2), max(0, width - label_w - 2))
            _draw_label_bg(draw, text_x, top_y, label_w, bbox[3] - bbox[1])
            draw.text(
                (text_x, top_y),
                label,
                fill=label_color,
                font=font,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
            _draw_label_bg(draw, text_x, bottom_y, label_w, bbox[3] - bbox[1])
            draw.text(
                (text_x, bottom_y),
                label,
                fill=label_color,
                font=font,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
            next_x_label_at = x + min_x_label_gap

    # Y 轴刻度：左侧 + 右侧。
    min_y_label_gap = max(axis_step // 2, tick_size * 2)
    next_y_label_at = 0
    for y in range(0, height, axis_step):
        draw.line([(0, y), (min(width - 1, tick_size), y)], fill=color, width=line_width)
        draw.line(
            [(width - 1, y), (max(0, width - 1 - tick_size), y)],
            fill=color,
            width=line_width,
        )

        if y >= next_y_label_at:
            label = str(y)
            bbox = draw.textbbox((0, 0), label, font=font, stroke_width=stroke_width)
            label_w = max(1, bbox[2] - bbox[0])
            label_h = max(1, bbox[3] - bbox[1])
            text_y = min(max(0, y + 2), max(0, height - label_h - 2))
            _draw_label_bg(draw, 2, text_y, label_w, label_h)
            draw.text(
                (2, text_y),
                label,
                fill=label_color,
                font=font,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
            right_x = max(0, width - label_w - 2)
            _draw_label_bg(draw, right_x, text_y, label_w, label_h)
            draw.text(
                (right_x, text_y),
                label,
                fill=label_color,
                font=font,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
            next_y_label_at = y + min_y_label_gap


def _draw_label_bg(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    pad = 2
    left = max(0, x - pad)
    top = max(0, y - pad)
    right = max(left + 1, x + w + pad)
    bottom = max(top + 1, y + h + pad)
    draw.rectangle((left, top, right, bottom), fill=(0, 0, 0, 150))


def _load_axis_font(width: int, height: int, requested_size: int) -> ImageFont.ImageFont:
    """
    选择更大号字体，优先使用 truetype，失败时回退默认字体。
    """
    if requested_size > 0:
        size = requested_size
    else:
        short_edge = max(1, min(width, height))
        size = max(20, min(80, short_edge // 32))

    pil_font = Path(PIL.__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"
    font_candidates = (
        str(pil_font),
        "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for font_path in font_candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


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


def _to_jpeg_bytes(image: Image.Image, quality: int, subsampling: int) -> bytes:
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=quality,
        subsampling=subsampling,
        optimize=True,
    )
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


def _cfg_float(
    cfg: Dict[str, Any],
    key: str,
    default: float,
    min_value: float = 0.0,
    max_value: float = 1e9,
) -> float:
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value
