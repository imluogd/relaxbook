# RelaxBook 个人电子书听书工具

在电脑浏览器导入电子书，核对目录并试听音色。同一次转换生成带章节目录的整书 M4B 和自动命名的分章 MP3。手机优先导入 M4B，Android 使用 Voice，iOS 使用 BookPlayer。

## 在另一台电脑接手

接手开发的模型或开发者须自行检查新电脑的 SSH 环境和 USTC 仓库访问权限，选择该机已配置的 SSH 客户端、密钥或主机别名。本项目在当前 Windows 电脑上使用 WSL Git 和 WSL 内的 SSH 密钥。新电脑可使用本机已授权的身份，或生成专用密钥，将公钥添加到有仓库权限的 USTC 账号。私钥留在生成它的电脑上。

```sh
git ls-remote git@git.ustc.edu.cn:luogd/relaxbook.git refs/heads/main
git clone git@git.ustc.edu.cn:luogd/relaxbook.git
cd relaxbook
```

已检出仓库时，核对 `git remote -v` 后运行 `git fetch origin`。若认证失败，自行用 `ssh -T git@git.ustc.edu.cn` 检查身份、公钥授权和主机配置。需要账号所有者授予权限时，提供公钥指纹和实际错误信息。检出代码后按下文「安装 / 检查 / 启动」在本机完成配置。

## 环境要求

- 操作系统：Windows 10/11、macOS 12+ 或主流 Linux（x86_64）。
- Python 3.11 及以上（推荐 3.12）。Windows 安装时勾选 `py` 启动器或把 `python` 加入 PATH。
- **ffprobe（必需，由系统提供）**：用于校验生成的 MP3/M4B，`doctor` 会检查。Windows 可用 `winget install --id Gyan.FFmpeg -e`，macOS 用 `brew install ffmpeg`，Debian/Ubuntu 用 `sudo apt install ffmpeg`。任何发行版的 FFmpeg 安装包都同时包含 ffmpeg 与 ffprobe。
- **ffmpeg 不需要手动安装**：项目依赖 `imageio-ffmpeg`，其 wheel 自带 FFmpeg 9.0.2，`setup` 会安装它，`config.resolve_tools()` 会自动优先使用它。M4B 的内嵌章节标题需要 **FFmpeg 7 及以上**，自带版本满足要求；不要在 `config.json` 里手动指定更旧的 ffmpeg 路径。
- calibre（可选，仅 MOBI/AZW3 转换需要）：缺失时 `doctor` 给出警告，EPUB/PDF 仍可正常使用。
- 网络：默认语音引擎 Edge 为在线合成，需要能访问微软语音服务（见下文「语音合成」）。
- 依赖安装走 PyPI；在中国大陆网络或系统时钟不准的机器上，pip 直连 HTTPS 可能证书校验失败或卡在缓存，见下文「安装」。

## 安装

在仓库根目录运行 `setup`。它会创建/复用 `.venv`、按 `requirements.txt` 安装固定版本依赖、在数据目录写入默认 `config.json`、自动探测 ffmpeg/ffprobe/calibre，最后执行一遍关键检查并打印下一步命令。

PowerShell（Windows）：

```powershell
cd D:\path\to\relaxbook
.\scripts\setup.ps1 --data-dir D:\RelaxBookData
```

bash（macOS / Linux）：

```sh
cd ~/relaxbook
sh scripts/setup.sh --data-dir "$HOME/RelaxBookData"
```

如果直接运行 `.ps1` 时提示「未对文件进行数字签名，无法加载脚本」，用执行策略放行参数运行（`setup.ps1` / `doctor.ps1` / `start.ps1` 通用）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 --data-dir D:\RelaxBookData
```

也可以直接调用统一入口（首次安装时 `.venv` 还不存在，先用系统 Python 执行 `setup`，由它创建虚拟环境）：

```powershell
cd D:\path\to\relaxbook
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
python -m relaxbook.cli setup --data-dir D:\RelaxBookData
```

依赖已满足时 `setup` 会跳过 `pip install`。需要重新安装依赖时，`setup`（或手动 pip）使用以下参数，本机已实测可用：

```text
--no-cache-dir -i http://mirrors.ustc.edu.cn/pypi/simple/ --trusted-host mirrors.ustc.edu.cn --timeout 20
```

原因：系统时钟超前会让 HTTPS 证书校验失败，必须走 http 镜像；pip 默认缓存会挂起，必须 `--no-cache-dir`。手动安装新依赖时同样加上这些参数。

## 检查

`doctor` 逐项检查：Python 版本、`.venv`、依赖是否齐全、ffmpeg（要求 7+，通常为 imageio-ffmpeg 自带）、ffprobe、calibre（可选）、数据目录可写、剩余空间（要求 ≥1 GB）、Edge TTS 试合成（合成一句「测试」并计时）、中文音色 ≥3 且英文音色 ≥1。缺失项会给出可直接复制的修复命令。

```powershell
.\scripts\doctor.ps1 --data-dir D:\RelaxBookData
```

```sh
sh scripts/doctor.sh --data-dir "$HOME/RelaxBookData"
```

等价的直接调用（之后统一使用 `.venv` 内的解释器）：

```powershell
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
& .\.venv\Scripts\python.exe -m relaxbook.cli doctor --data-dir D:\RelaxBookData
```

```sh
unset PYTHONPATH
.venv/bin/python -m relaxbook.cli doctor --data-dir "$HOME/RelaxBookData"
```

退出码：全部通过或只有警告时为 `0`（警告项对应功能受限，例如缺 calibre 时不能转 MOBI/AZW3）；存在阻塞项时为 `1`，按「修复建议」处理后重跑。离线或不想做在线试合成时加 `--skip-tts`。

## 启动

`start` 先做轻量检查（数据目录可写），再启动工作进程（日志写入数据目录的 `logs/worker.out`）和本机网页服务：

```powershell
.\scripts\start.ps1 --data-dir D:\RelaxBookData
```

```sh
sh scripts/start.sh --data-dir "$HOME/RelaxBookData"
```

等价的直接调用：

```powershell
& .\.venv\Scripts\python.exe -m relaxbook.cli start --data-dir D:\RelaxBookData
```

- 默认监听 `http://127.0.0.1:8765/`，启动后自动打开浏览器。端口被占用时会依次尝试 `port+1` … `port+20`，并打印实际地址；也可用 `--port 9000` 指定起始端口。
- `--no-browser` 不自动打开浏览器；`--dry-run` 只检查参数与端口选择，不启动任何进程；`--host` 可改监听地址。
- 按 `Ctrl+C` 停止：会同时结束后台工作进程并释放端口。工作进程被强制结束后重新 `start`，已完成的音频片段会复用，只需补做缺失部分。
- 需要新终端操作时直接再开一个窗口；网页与接口地址都以 `start` 打印的为准。

## 数据目录与配置

个人书籍、缓存、模型与音频都保存在数据目录，**不写入 Git 工作区**。默认位置：

| 平台 | 默认数据目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\RelaxBook` |
| macOS | `~/Library/Application Support/RelaxBook` |
| Linux | `$XDG_DATA_HOME/relaxbook`，未设置时为 `~/.local/share/relaxbook` |

覆盖方式（两者等价，环境变量对三个命令都生效，`--data-dir` 优先级最高）：

```powershell
$env:RELAXBOOK_DATA_DIR = 'D:\RelaxBookData'
```

```sh
export RELAXBOOK_DATA_DIR="$HOME/RelaxBookData"
```

建议显式指定到仓库工作区之外的固定目录（如上例），便于备份和换机。目录结构：

```text
<数据目录>/
  config.json        本机配置
  relaxbook.db       书库、任务、片段与成品记录
  books/             导入的原始电子书
  segments/          合成的音频片段缓存（按内容哈希，断点续做）
  jobs/<任务号>/v01/ 分章 MP3 与整书 M4B（结果页下载入口）
  previews/          试听样音
  logs/              worker.out / worker.log
  models/            预留的本地模型目录
```

`config.json` 常用字段：`host`、`port`、`engine`（默认 `auto`：有 Qwen Key 时用 `qwen`，否则用 `edge`）、`voice`（Edge 默认 `zh-CN-XiaoxiaoNeural`；Qwen 默认 `Cherry`）、`rate`（默认 `+0%`，语速）、`concurrency`（并发合成数，默认 3）、`qwen_api_key` / `qwen_model` / `qwen_base_url` / `qwen_timeout`（Qwen-TTS 配置）、`ffmpeg` / `ffprobe` / `calibre_convert`（留空自动探测）、`mp3_bitrate` / `m4b_bitrate`、`zip_packaging`、`open_browser`。

## 语音合成

两个成熟的在线引擎，通过 `config.json` 的 `engine` 切换；网页试听与整书生成使用同一引擎与音色。

| 引擎 | `engine` 值 | 音色 | 前置条件 | 语速 |
| --- | --- | --- | --- | --- |
| Qwen-TTS（阿里云百炼 / DashScope，`qwen3-tts-flash`） | `qwen` | 14 个：芊悦/Cherry、苏瑶/Serena、晨煦/Ethan、千雪/Chelsie、月白/Moon、四月/Maia、凯/Kai、不吃鱼/Nofish、萌宝/Bella、茉兔/Momo、十三/Vivian、Jada（上海话）、Dylan（北京话）、Peter（天津话） | 需要 `DASHSCOPE_API_KEY` 或 `config.json` 的 `qwen_api_key`，按字符计费 | 暂不支持 `rate`，请用 `+0%` |
| 微软 Edge 语音 | `edge` | 5 个中文（晓晓、晓伊、云希、云健、云扬）+ 2 个英文 | 免费，需联网 | 支持 `+0%`、`+10%` 等 |

- 默认 `engine` 是 `auto`：**配置了 DashScope Key 就用 Qwen-TTS，否则自动使用 Edge**。`doctor` 会报告 Qwen 是否已配置（未配置只是警告，不阻塞）。
- 启用 Qwen：`setx DASHSCOPE_API_KEY "sk-..."`（重开终端生效），或写进 `config.json` 的 `qwen_api_key`；随后 `doctor` 应显示「Qwen-TTS（可选）通过」，`/api/voices` 会列出全部 14 个 Qwen 音色（网页音色列表标注 `Qwen`/`Edge`）。
- 选音色即选引擎：网页里勾选哪个音色，任务就用该音色所属的引擎（Qwen 与 Edge 音色可以混选试听，正式生成时建议同一本书统一用一个音色）。
- 实测速度（本机，`qwen3-tts-flash`，并发 4）：约 **x10.9 实时**，即每 1 小时成品音频约 5.5 分钟；并发 1 时因单请求延迟约 8 秒会掉到 x1.2，所以 `concurrency` 不要设为 1。
- Edge 在中国大陆直连微软语音服务需要浏览器 TLS 指纹，项目已用 `curl_cffi` 的 `impersonate="chrome"` WebSocket 处理，无需额外配置；`requirements.txt` 已锁定 `edge-tts` 与 `curl_cffi` 版本。
- 公网端点偶发连接重置：Edge 引擎内置 25 秒单次超时、最多 5 次退避重试；仍失败时任务保留已完成片段，可在结果页点「重试」继续，不会重做已合成部分。
- 本地离线引擎（Kokoro / IndexTTS）当前未内置：它们需要 GPU 才有可用速度，本项目所在机器为核显，因此优先接成熟云端引擎；`doctor` 不要求它们。

## 常见问题

- **doctor 报 ffmpeg 缺失或过旧**：运行 `setup` 重新安装依赖（`imageio-ffmpeg` 自带 FFmpeg 9.0.2，满足 M4B 章节标题要求）；若 `config.json` 里手动写过旧路径，删掉该字段后重跑 `setup`。不需要手动安装 FFmpeg 本体。
- **doctor 报 ffprobe 缺失**：安装系统 FFmpeg（见「环境要求」）；或把 ffprobe 的绝对路径填入 `config.json` 的 `ffprobe` 字段。
- **calibre 缺失**：只是警告，MOBI/AZW3 不可用；EPUB/PDF 不受影响。需要时安装 calibre，或把 `ebook-convert` 绝对路径填入 `config.json` 的 `calibre_convert`。
- **端口被占用**：`start` 会自动改用后续端口并打印实际地址；也可 `--port` 指定，或修改 `config.json` 的 `port`。
- **音频存在哪里**：都在数据目录里。分章 MP3 与整书 M4B 位于 `jobs/<任务号>/v01/`（M4B 命名为「书名 - 音色 - v01.m4b」），网页结果页提供下载；断点续做用的片段缓存在 `segments/`，可整体备份。
- **手机上怎么听**：把整书 M4B 复制到手机——Android 用 Voice 导入，iOS 从「文件」导入 BookPlayer，通过内嵌目录选章；也可以只复制需要的分章 MP3（文件名形如 `001 - 序言.mp3`，按序排列即按章播放）。
- **pip 安装失败/卡住**：确认使用「安装」一节给出的镜像参数（`--no-cache-dir` 与 http 镜像）；系统时钟偏差大时先校准时间。
- **`PYTHONPATH` 污染**：本机会把无关路径注入 `PYTHONPATH`。`scripts/*` 已自动清理；手动调用前先执行 `Remove-Item Env:PYTHONPATH`（PowerShell）或 `unset PYTHONPATH`（bash）。
- **默认数据目录不可写**：部分受限环境无法创建 `%LOCALAPPDATA%\RelaxBook`。用 `--data-dir` 或 `RELAXBOOK_DATA_DIR` 指定一个可写目录（且不要在仓库工作区内）即可，`doctor` 会明确报告此阻塞。
- **换机复装验收**：按 [第一版验收清单](docs/ACCEPTANCE.md) 的 B3，在第二台电脑上只按本 README 完成安装、检查、启动，用浏览器导入样书并下载两种音频；记录 `doctor` 输出的操作系统、Python、依赖与工具版本。开发规格见 [实现规格](docs/SPEC.md) 和 [产品意图](docs/intent.md)。
