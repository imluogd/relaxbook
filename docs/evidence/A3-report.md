# RelaxBook A3 验收证据

- 生成时间：2026-11-22T13:21:24
- Git 提交：b1493cb
- 平台：Windows-10-10.0.19045-SP0 / Python 3.12.7
- 数据目录：D:\dsh\tmp\rb-accept-evidence
- 引擎/音色/语速：auto（解析为 edge） / zh-CN-XiaoxiaoNeural / +0%（并发 3）
- ffmpeg：D:\dsh\relaxbook\.venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v9.0.2.exe
- ffprobe：E:\ffmpeg\bin\ffprobe.EXE

## 任务与成品

- 任务号：1，状态：**done**
- 片段：17/17 完成，0 失败
- 章数：3/3
- M4B：`示例书 - 晓晓（女·温暖） - v01.m4b`，0.98 MB，122.232 秒（探测），内嵌 3 章
- 三个 MP3 合计：0.93 MB，122376 毫秒（探测合计）
- 章节衔接最大间隙：0 毫秒

## 章节表（序号 / 章名 / MP3 文件名 / 大小 / 时长 / M4B 起止）

| 序号 | 章名 | MP3 文件名 | 大小(MB) | 时长(s) | M4B 起点(s) | M4B 终点(s) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 序言 | 001 - 序言.mp3 | 0.21 | 27.6 | 0.0 | 27.552 |
| 2 | 出发 | 002 - 出发.mp3 | 0.29 | 37.776 | 27.552 | 65.28 |
| 3 | 出发 | 003 - 出发.mp3 | 0.44 | 57.0 | 65.28 | 122.232 |

## 网页结果页显示值 vs 成品探测值（秒值误差要求 ≤ 1 秒）

| 序号 | 网页时长(s) | 探测时长(s) | 时长差(ms) | 网页 M4B 起止(s) | 探测 M4B 起止(s) | 起止差(ms) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 27.6 | 27.6 | 0 | 0.0-27.552 | 0.0-27.552 | 0/0 |
| 2 | 37.776 | 37.776 | 0 | 27.552-65.28 | 27.552-65.28 | 0/0 |
| 3 | 57.0 | 57.0 | 0 | 65.28-122.232 | 65.28-122.232 | 0/0 |

## 生成效率（每小时成品音频所需生成时间）

- 本次生成耗时：9.43 秒（真实合成（全部片段重新合成））
- 缓存：命中 0 个片段，未命中 17 个片段
- 成品音频 122.232 秒；每 1 小时成品音频约需 278 秒（4.6 分钟）

## 检查项

通过 38/38 项

## 复现命令

```sh
ffprobe -v error -show_format -show_chapters -of json "示例书 - 晓晓（女·温暖） - v01.m4b"
ffprobe -v error -show_format -of json "001 - 序言.mp3"
ffprobe -v error -show_format -of json "002 - 出发.mp3"
ffprobe -v error -show_format -of json "003 - 出发.mp3"
ffmpeg -v error -xerror -i "示例书 - 晓晓（女·温暖） - v01.m4b" -map 0:a:0 -f null -
```

## 证据文件

- `ffprobe_m4b.json`：M4B 的原始 ffprobe JSON（含 format 与 chapters）
- `ffprobe_mp3/*.json`：每个 MP3 的原始 ffprobe JSON
- `ffmpeg_decode_check.txt`：`ffmpeg -f null` 校验命令与退出码
- `chapters.csv`：章节表（含网页值与探测值对照）
- `checks.json`：逐项检查结果
- `summary.json`：本次验收的机器可读汇总
- `round1/`、`round2/`：每次执行任务的进度证据（含 A5 恢复轮次）
