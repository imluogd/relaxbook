## Motivation

白天用眼太多，想把电子书转成音频，少看屏幕也能继续阅读。

## 安装

准备 Python 3.11、FFmpeg（含 ffprobe）和 calibre。在仓库根目录安装依赖。

```sh
python -m pip install -r requirements.txt
```

## 部署

- 线上先设置北京百炼的 `DASHSCOPE_API_KEY`，再运行 `python -m relaxbook start`。
- 离线需要 Windows WSL 或 x86-64 Linux。运行 `python -m relaxbook start --with-local-qwen`，首次会自动下载模型。

[打开本机网页](http://127.0.0.1:8765) · [部署指南](docs/setup.md)
