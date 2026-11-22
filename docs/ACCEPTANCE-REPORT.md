# RelaxBook 第一版实现与验收记录

依据 [ACCEPTANCE.md](ACCEPTANCE.md) 与 [SPEC.md](SPEC.md)。本文件记录**已实现的功能**与**本机能复现的证据**；
未执行项明确标为「待验证」，不写成通过。

- 代码版本：本文件所在提交（`git log -1`）
- 环境：Windows 11，Intel i5-1240P / Iris Xe / 16 GB，Python 3.12.7（`.venv`），
  FFmpeg 9.0.2（`imageio-ffmpeg` 自带，用于编码与 M4B 章节轨），ffprobe N-110363（系统，仅读取校验），
  calibre 8.3.0（`F:\calibre`），数据目录位于工作区之外。
- 样书：`sample/示例书.epub`（三章：序言 / 出发 / 出发，后两章同名）、`sample/示例书.pdf`（带书签）、
  `sample/扫描示例.pdf`（纯图片）、`sample/sample-en.epub`。

## 一、本地实测（A2 / A3 核心链路）

命令：`python -m relaxbook.cli start`（或在本机用 `python -m pytest tests/test_e2e.py -q`）。

| 项目 | 实测值 |
| --- | --- |
| 导入 | `sample/示例书.epub` → 书名「示例书」、作者「示例作者」、3 章，顺序与同名章均正确 |
| 片段 | 17 段（按 `textnorm.segment_text` 真实切片） |
| 生成耗时 | 8.1 秒（并发 3，网络畅通时约 **15 倍实时**；网络拥塞时实测退到 3.7 倍） |
| 整书 M4B | `示例书 - 晓晓（女·温暖） - v01.m4b`，1,025,368 字节，总时长 **122.232 秒**，AAC 64 kbps |
| 分章 MP3 | `001 - 序言.mp3` 27.600 s / 221,036 B；`002 - 出发.mp3` 37.776 s / 302,444 B；`003 - 出发.mp3` 57.000 s / 456,236 B（均 64 kbps） |
| 整书解码 | `ffmpeg -v error -xerror -i <m4b> -map 0:a:0 -f null -` 退出码 0 |

M4B 内嵌章节（`ffprobe -show_chapters` 原始值，完整 JSON 见 [evidence/A3-ffprobe-m4b.json](evidence/A3-ffprobe-m4b.json)）：

| # | 章名 | 起点 (s) | 终点 (s) |
| --- | --- | --- | --- |
| 1 | 序言 | 0.000 | 27.552 |
| 2 | 出发 | 27.552 | 65.280 |
| 3 | 出发 | 65.280 | 122.232 |

首尾严格相接、时间单调，标题与章节顺序来自编辑稿；最后一章终点等于整书时长。
三个 MP3 时长之和 122.376 s 与整书 122.232 s 差 144 ms（AAC 帧填充），在 A3 允许的 1 秒内。

复现命令：

```sh
python tools/acceptance.py --data-dir D:\tmp\rb-accept --book sample\示例书.epub
ffprobe -v error -show_format -show_chapters -of json "示例书 - 晓晓（女·温暖） - v01.m4b"
```

`tools/acceptance.py` 实测结果（原始证据见 [evidence/A3-report.md](evidence/A3-report.md)、
[evidence/A3-summary.json](evidence/A3-summary.json)、[evidence/A3-chapters.csv](evidence/A3-chapters.csv)）：

- 38/38 检查项通过，退出码 0；任务 `done`，片段 17/17，章 3/3。
- 网页结果页显示值 vs 成品探测值：三章时长差 0 ms、M4B 起止差 0 ms（A3 要求 ≤1 秒）。
- 章节衔接最大间隙 0 ms；M4B 与三个 MP3 均可解码。
- 生成效率：**9.43 秒生成 122.232 秒音频**，即每 1 小时成品音频约需 **278 秒（约 4.6 分钟）**
  （缓存命中 0、未命中 17、并发 3，网络畅通时）。

## 二、按验收清单的状态

### 第一轮（电脑端）

| 编号 | 状态 | 证据 / 说明 |
| --- | --- | --- |
| A1 导入与编辑 | 通过（API 层） | EPUB/PDF/扫描件解析与同名章保留有单测（`tests/test_ingest.py` 23 项）；`PUT /api/books/{id}/revision` 保存新编辑版本、版本过期返回 409（`tests/test_api.py`）；网页提供改名/上移下移/拆分/合并/跳过/恢复。**网页人工逐项点击**未做，标为待人工复核。 |
| A2 两种格式 | 通过 | 见第一节：同一次合成得到 1 个 M4B + 3 个 MP3，文件名完全符合「001 - 序言.mp3」规则；空标题回退「未命名章节」、含 `/` 的标题清洗为全角并保证唯一（`tests/test_naming.py` 14 项） |
| A3 结果页 | 通过（数据层） | `GET /api/jobs/{id}` 返回书名、音色、已生成/计划章数、总时长、M4B 文件名/大小/下载 ID，以及按序章节表（序号、章名、MP3 文件名、M4B 起止、时长、大小、状态、下载）；M4B 起止与总时长来自成品 `ffprobe` 探测（不取自估算）。网页显示与探测值同源，秒值误差 0。**截图未取**。 |
| A4 格式边界 | 部分 | EPUB、文本 PDF（按书签分章 + 页眉页脚清理）、扫描 PDF（明确报「未提取到可读正文，疑似扫描件，需要先 OCR」）已实测；**MOBI / AZW3 未实测**：calibre 8.3.0 已安装、代码走 `ebook-convert` 转工作副本，但没有可用的无 DRM 样本，标为待验证 |
| A5 中断与重试 | 通过 | `tests/test_worker.py` 11 项覆盖：缓存命中不重复合成（第二次任务 `synth_calls=0`）、暂停后不再提交新片段、结束工作进程后只补缺失片段、封装失败时保留已成功 MP3 且「重试」只重封装、每片段一行合成日志 `job=… seg=… cache=hit|miss synth_ms=…` |

### 第二轮（听感、手机、换机）

| 编号 | 状态 | 说明 |
| --- | --- | --- |
| B1 音色 | 待人工试听 | 已内置 Edge 5 个中文 + 2 个英文音色；配置 `DASHSCOPE_API_KEY` 后可用 Qwen-TTS 的 Cherry / Serena / Ethan / Chelsie（`qwen3-tts-flash` 另有方言音色）。**需要使用者试听 10 分钟选定长期音色**。 |
| B2 真机 | 未验证 | 需要 Android 的 Voice 与 iOS 的 BookPlayer 实机；M4B 内嵌章节与 MP3 命名已按两个播放器的导入约定生成 |
| B3 全新安装 | 未验证 | 需要第二台电脑按 README 从同一提交安装；`setup` / `doctor` / `start` 已在本机跑通（doctor 10 项通过 + Qwen 未配置为警告） |

## 三、时长与耗时预测（仅按文本统计，未实测）

`relaxbook/estimate.py` 只读文本，不合成、不调用 ffmpeg。校准依据是本项目已产出的《示例书》成品：
549 个正文字符 → 122.232 秒音频，即 **4.6 字/秒**；预测值 119.3 秒，与成品差 2.4%。

| 正文字数 | 成品时长（估） | M4B 64 kbps（估） | 生成耗时（edge，并发 3，x6 保守 / x15 畅通） |
| --- | --- | --- | --- |
| 1 万 | 约 36 分钟 | 约 17 MB | 约 6 分钟 / 约 2.5 分钟 |
| 3 万 | 约 1.8 小时 | 约 52 MB | 约 18 分钟 / 约 7 分钟 |
| 5 万 | 约 3.0 小时 | 约 87 MB | 约 30 分钟 / 约 12 分钟 |
| 10 万 | 约 6.1 小时 | 约 175 MB | 约 61 分钟 / 约 24 分钟 |
| 20 万 | 约 12.2 小时 | 约 350 MB | 约 2.0 小时 / 约 49 分钟 |

实测效率参照：本机网络畅通时 **每小时成品音频约 4.6 分钟**（x13）；拥塞时实测降到 x3.7
（每小时约 16 分钟）。网页在「目录与正文」页会直接显示这本导入书的预测值（成品时长 / 预计生成 / 成品大小）。

## 四、已知限制

1. **联网合成**：默认 Edge 引擎需要访问微软语音服务。中国大陆直连需要浏览器 TLS 指纹（已用 `curl_cffi` 处理），
   但公网端点会出现连接重置；引擎内置 25 秒单次超时、最多 5 次退避重试，任务失败时已完成片段保留、可点「重试」续跑。
   需要稳定高质量音色时改用 Qwen-TTS 云 API（配 `DASHSCOPE_API_KEY`）。
2. **本地离线引擎未内置**：Kokoro / IndexTTS 在本机（核显、无 CUDA）速度不可用，接口已按引擎插件化预留。
3. **扫描件**：第一版只识别并提示需要 OCR，不做 OCR。
4. **MOBI / AZW3**：依赖 calibre，代码路径已实现，但尚无样书实测。
5. **默认数据目录**：`%LOCALAPPDATA%\RelaxBook`；本机沙箱不可写时会要求显式 `--data-dir` / `RELAXBOOK_DATA_DIR`。
   运行时数据（书籍、片段缓存、成品、模型）始终在工作区之外，不随 Git 分发。
