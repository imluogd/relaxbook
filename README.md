# RelaxBook 个人电子书听书工具

在电脑浏览器导入电子书，核对目录并试听音色。同一次转换生成带章节目录的整书 M4B 和自动命名的分章 MP3。手机优先导入 M4B，Android 使用 Voice，iOS 使用 BookPlayer。

## 安装和启动

准备 Python 3.11、带 AAC 和 libmp3lame 编码器的 FFmpeg（含 ffprobe），以及 calibre 的 ebook-convert。将两个工具目录加入 PATH，或在 `relaxbook.local.json` 中填写它们的完整路径。模型安装一次后即可在本机离线合成。

在仓库根目录执行。Windows PowerShell 使用以下命令：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m relaxbook setup
.venv\Scripts\python.exe -m relaxbook setup-voices
.venv\Scripts\python.exe -m relaxbook doctor
.venv\Scripts\python.exe -m relaxbook start
```

macOS 和 Linux 使用以下命令：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m relaxbook setup
.venv/bin/python -m relaxbook setup-voices
.venv/bin/python -m relaxbook doctor
.venv/bin/python -m relaxbook start
```

浏览器打开 [本机书库](http://127.0.0.1:8765)。选择电子书并检查章节，试听音色后点击「生成有声书」。完成后下载 M4B 导入手机，也可以逐章下载 MP3 或打包下载。

### 使用 Qwen 龙安灵心

在运行 RelaxBook 的电脑上配置阿里云百炼华北 2（北京）的 `DASHSCOPE_API_KEY`。Windows 可搜索「编辑账户的环境变量」，添加同名的用户变量。macOS 和 Linux 在启动进程的环境中设置。密钥留在本机，避免写入项目配置或提交到 Git。[获取百炼 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)

检测到密钥时，网页默认选择「Qwen · 龙安灵心 · 有声书朗读指令」。试听和整书使用同一个 `qwen-audio-3.1-tts-plus` 模型、音色和朗读指令。使用北京 WebSocket 接入，只需配置 API Key。在已有安装上更新代码后，先用当前虚拟环境执行 `python -m pip install -r requirements.txt`，再重启网页服务。

Qwen 合成会把待朗读的文字发送给百炼，可能按字符计费。书籍文件、音频和转换进度保存在本机。网络或额度问题导致任务失败时，可在结果页重试，已成功合成的片段从本机缓存复用。[模型与计费](https://help.aliyun.com/zh/model-studio/qwen-audio-3-1-tts-plus)

### 使用微软在线音色

安装可选依赖 `python -m pip install -r requirements-edge.txt`，在 `relaxbook.local.json` 中把 `microsoft_online` 设为 `true`，再重启。启动虚拟环境中的 Python 执行命令。可选择晓晓、云扬和英文 Jenny，离线音色仍可选择。

微软音色通过 [edge-tts](https://github.com/rany2/edge-tts) 调用 Edge 在线朗读服务，无需 Azure API 密钥。使用该音色时，待朗读的文字会发送到微软服务，书籍文件、导出的音频和转换进度保存在本机。联网合成失败时可在结果页重试，已完成的片段从本机缓存复用。该服务与 [Azure Speech 正式计费接口](https://azure.microsoft.com/en-us/pricing/details/speech/) 分开。Edge 在线朗读服务的可用性和限制由微软决定。

`setup-voices` 安装可选的两套离线普通话语音包，压缩下载合计约 286 MB。安装后重新启动网页，即可试听 Melo 中英混读女声及 VITS 的五种普通话音色。只使用离线音色时，网页优先选择 Melo，原 Kokoro 音色仍可用于对照和续跑。在同一段正文上保持语速一致，勾选多个音色进行比较。要给已完成的书换音色，请在原书页面选择新音色并再次生成。语音合成在本机完成。

Melo 的原生采样率为 44.1 kHz，VITS 普通话语音包为 16 kHz。转换保留引擎的原生采样率，M4B 按实际样本数计算章节时间。新语音片段使用有限的增益调整，并为编码留出峰值余量。M4B 使用更高的 AAC 码率，MP3 使用 LAME 可变码率质量档 2。试听可以区分朗读节奏和音色，采样率和码率只反映音频格式。

复制 `relaxbook.example.json` 为 `relaxbook.local.json` 可调整端口、工具路径、`data_dir` 和 `model_dir`。也可通过环境变量 `RELAXBOOK_CONFIG` 指向配置文件。默认的数据库及模型位于 Windows 的 `%LOCALAPPDATA%\RelaxBook` 或 macOS/Linux 的 `~/.local/share/RelaxBook`。浏览器关闭后，启动终端继续运行转换。退出并重启后，在结果页点击「继续」恢复中断任务。

## 验证开发版本

这版采用自己的本地网页和任务管理，直接复用 Kokoro、FFmpeg、calibre 和 PDF 解析库。预先启动一个空数据目录的实例，随后在仓库根目录运行下方命令。Windows 将 `python` 换成虚拟环境的 `.venv\Scripts\python.exe`，设置 `$env:PYTHONPATH = '.'`；macOS/Linux 先运行 `export PYTHONPATH=.`。

```sh
python tests/smoke.py http://127.0.0.1:8765 path/to/evidence
```

该检查生成三章 EPUB，调用当前安装的离线音色，核对真实 M4B 章节、分章 MP3、ZIP 和结果数据。人工试听、真机播放和新电脑复装按 [验收清单](docs/ACCEPTANCE.md) 执行。

分支采用 Spec 中的独立实现基线，直接集成 [sherpa-onnx Kokoro](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html)，以便复用语音片段并单独重试封装。abogen 和 ebook2audiobook 的完整 Web 界面实测对比仍待补充。首版的扫描件会提示 OCR，复杂版式和分章错误可通过网页检查并编辑正文。

Windows 首轮实测覆盖四种可读格式、扫描 PDF 提示、三种普通话及一种英文样音、浏览器下载、M4B 与 MP3 文件核对、工作进程退出后续跑，以及封装失败后的重试。换机复装、Android/iOS 真机和真实长书听感按验收清单继续记录。

## 在另一台电脑接手

接手开发的模型或开发者须自行检查新电脑的 SSH 环境和 USTC 仓库访问权限，选择该机已配置的 SSH 客户端、密钥或主机别名。本项目在当前 Windows 电脑上使用 WSL Git 和 WSL 内的 SSH 密钥。新电脑可使用本机已授权的身份，或生成专用密钥，将公钥添加到有仓库权限的 USTC 账号。私钥留在生成它的电脑上。

```sh
git ls-remote git@git.ustc.edu.cn:luogd/relaxbook.git refs/heads/main
git clone git@git.ustc.edu.cn:luogd/relaxbook.git
cd relaxbook
```

已检出仓库时，核对 `git remote -v` 后运行 `git fetch origin`。若认证失败，自行用 `ssh -T git@git.ustc.edu.cn` 检查身份、公钥授权和主机配置。需要账号所有者授予权限时，提供公钥指纹和实际错误信息。

## 实现交接

开发与交付时使用[第一版验收清单](docs/ACCEPTANCE.md)，其中列有样书、成品核对方法、真机步骤和结果记录模板。

继续开发时请阅读 [产品意图](docs/intent.md)、[实现规格](docs/SPEC.md) 和 [验收清单](docs/ACCEPTANCE.md)。变更导出流程后，核对结果页与下载成品，再验证手机导入后的章节跳转。

软件从同一 Git 版本安装到各台电脑，个人书籍、缓存、模型及音频保存在本机数据目录。云服务器部署留到后续版本。
