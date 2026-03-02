# dsapi-mcp-server

基于 Python 的 MCP Server，用于通过 DirectScreenAPI 获取 Android 屏幕帧并执行触控/系统按键操作。

## 功能

- 获取带坐标刻度的屏幕截图（JPEG）。
- 触控动作：`tap`、`long_press`、`swipe`、`drag_and_drop`、`zoom`。
- 系统按键：`back/home/recents`、`volume_up/down`。

## 环境要求

- Python 3.10+
- Android 端已运行 DirectScreenAPI，并可访问其 Unix Domain Socket。

## 安装

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 配置

默认读取同目录下 `config.yaml`。

如需使用其他配置文件，可设置：

```bash
export DSAPI_MCP_CONFIG=/path/to/config.yaml
```

## 启动

```bash
python server.py
```

## 许可证

本项目采用 [MIT License](LICENSE)。

