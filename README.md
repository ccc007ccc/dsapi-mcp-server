# dsapi-mcp-server

一个面向 Android 自动化的 MCP Server：通过 DirectScreenAPI 拉取屏幕帧，并提供点击、滑动、缩放、系统按键等能力。

## 特性

- 屏幕截图：返回 JPEG 图片和结构化元信息（分辨率、缩放比、frame_seq）。
- 手势控制：`tap`、`long_press`、`swipe`、`drag_and_drop`、`zoom`。
- 系统按键：`system_nav`、`volume_up`、`volume_down`。
- 坐标换算：自动把模型坐标映射回物理屏幕坐标，减少点位偏差。

## 项目结构

```text
.
├── server.py          # MCP 工具注册与运行时编排
├── dsapi_client.py    # DirectScreenAPI 协议客户端（取帧/触控）
├── image_pipeline.py  # 图像处理（刻度绘制、缩放、JPEG 编码）
├── config.yaml        # 运行配置
└── requirements.txt   # Python 依赖
```

## 环境要求

- Python 3.10+
- Android 端已启动 DirectScreenAPI
- 本机可访问 UDS 文件（默认：`DirectScreenAPI/artifacts/run/*.sock`）

## 快速开始

1. 安装依赖：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

2. 检查 Socket 文件（按需修改 `config.yaml`）：

```bash
ls -l DirectScreenAPI/artifacts/run/dsapi*.sock
```

3. 启动服务：

```bash
python server.py
```

## 配置说明

默认读取同目录下 `config.yaml`，也可通过环境变量覆盖：

```bash
export DSAPI_MCP_CONFIG=/path/to/config.yaml
```

常用配置项：

- `uds.data_socket`：数据通道 socket 路径。
- `timeouts.frame_wait_timeout_ms`：单帧等待超时。
- `image.max_long_edge`：图片最长边，值越大越清晰（带宽/体积更高）。
- `image.jpeg_quality`：JPEG 质量（推荐 85~92）。
- `image.jpeg_subsampling`：色度子采样（`0` 清晰度最高）。
- `image.enable_sharpen`：是否启用锐化增强。
- `image.axis_font_size`：坐标字体大小（`0` 表示自动）。
- `image.enable_axis`：是否绘制边框刻度。
- `system_control.use_su`：是否通过 `su -c input ...` 发送系统按键。

路径解析规则：

- `config.yaml` 里的 `uds.*socket` 支持相对路径，服务会自动按 `config.yaml` 所在目录解析为绝对路径，避免受启动 cwd 影响。

## 客户端接入示例

### Claude Desktop

在 Claude Desktop 的 MCP 配置文件中加入：

```json
{
  "mcpServers": {
    "dsapi-mcp-server": {
      "command": "/path/to/dsapi-mcp-server/.venv/bin/python",
      "args": [
        "/path/to/dsapi-mcp-server/server.py"
      ],
      "env": {
        "DSAPI_MCP_CONFIG": "/path/to/dsapi-mcp-server/config.yaml"
      }
    }
  }
}
```

### 通用 stdio MCP 客户端

若客户端支持以命令启动 MCP Server，可使用：

```bash
/path/to/dsapi-mcp-server/.venv/bin/python /path/to/dsapi-mcp-server/server.py
```

并设置环境变量：

```bash
DSAPI_MCP_CONFIG=/path/to/dsapi-mcp-server/config.yaml
```

### 接入后验证

1. 在 MCP 客户端中刷新工具列表，确认出现 `get_annotated_screen`。
2. 调用 `get_annotated_screen()`，应返回一张图片和 `status: ok` 的结构化结果。
3. 再调用 `tap(x, y)` 做一次点击验证触控链路。

## MCP 工具清单

- `get_annotated_screen()`：获取带坐标刻度的屏幕截图和元信息。
- `tap(x, y)`：单击。
- `long_press(x, y, duration_ms=1000)`：长按。
- `swipe(start_x, start_y, end_x, end_y, duration_ms=300)`：滑动。
- `drag_and_drop(start_x, start_y, end_x, end_y, hold_ms=500, duration_ms=500)`：拖拽。
- `zoom(center_x, center_y, direction, distance=400, duration_ms=300)`：双指缩放，`direction` 取 `in/out`。
- `system_nav(action, repeat=1, interval_ms=80)`：系统导航键，`action` 支持 `back/home/recents`。
- `volume_up(steps=1, interval_ms=80)`：音量增加。
- `volume_down(steps=1, interval_ms=80)`：音量减少。

## 常见问题

- `touch_stream_handshake_*`：通常是 `uds.data_socket` 路径不对，先确认 DirectScreenAPI 已启动。
- `Permission denied`（系统按键）：设备未授权 `su` 或不支持 root，改为 `system_control.use_su: false` 后重试。
- 截图超时：增大 `timeouts.frame_wait_timeout_ms`，并检查设备端是否持续产出帧。

## 许可证

本项目采用 [MIT License](LICENSE)。
