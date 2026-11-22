# RelaxBook 个人电子书听书工具

在电脑浏览器导入电子书，核对目录并试听音色。同一次转换生成带章节目录的整书 M4B 和自动命名的分章 MP3。手机优先导入 M4B，Android 使用 Voice，iOS 使用 BookPlayer。

## 安装和启动

准备 Python 3.11、带 AAC 和 libmp3lame 编码器的 FFmpeg（含 ffprobe），以及 calibre 的 ebook-convert。将两个工具目录加入 PATH，或在 `relaxbook.local.json` 中填写它们的完整路径。模型安装一次后即可在本机离线合成。

先按下文配置至少一种 Qwen 转换方式，再在仓库根目录运行以下命令。Windows 使用 PowerShell。

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m relaxbook doctor
.venv\Scripts\python.exe -m relaxbook start
```

macOS 和 Linux 使用以下命令：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m relaxbook doctor
.venv/bin/python -m relaxbook start
```

浏览器打开 [本机书库](http://127.0.0.1:8765)。选择电子书并检查章节，试听音色后点击「生成有声书」。完成后下载 M4B 导入手机，也可以逐章下载 MP3 或打包下载。

### 使用百炼 Qwen 在线音色

在运行 RelaxBook 的电脑上配置阿里云百炼华北 2（北京）的 `DASHSCOPE_API_KEY`。Windows 可搜索「编辑账户的环境变量」，添加同名的用户变量。macOS 和 Linux 在启动进程的环境中设置。密钥留在本机，避免写入项目配置或提交到 Git。[获取百炼 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)

检测到密钥时，网页提供 `qwen-audio-3.1-tts-plus` 的四种系统音色：龙安灵心、许南川、龙安欢和龙安风悦。龙安灵心默认选中。试听可勾选多个音色，整书按下拉框中选定的音色生成。四种音色共用有声书朗读指令。北京 WebSocket 接入只需配置 API Key。在已有安装上更新代码后，先用当前虚拟环境执行 `python -m pip install -r requirements.txt`，再重启网页服务。[百炼音色列表](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list)

Qwen 合成会把待朗读的文字发送给百炼，可能按字符计费。书籍文件、音频和转换进度保存在本机。网络或额度问题导致任务失败时，可在结果页重试，已成功合成的片段从本机缓存复用。[模型与计费](https://help.aliyun.com/zh/model-studio/qwen-audio-3-1-tts-plus)

### 使用 Qwen3-TTS 离线音色

在 x86-64 Linux 或 Windows 的 WSL 中运行 Qwen3-TTS 1.7B CustomVoice。Vivian、Serena 和 Uncle_Fu 共用一个模型，均可在 CPU 上运行。首次安装需联网，运行器和模型约 1.34 GB，安装在仓库的 `.local/qwen3-tts`。安装完成后，合成只访问本机回环端口。

在仓库根目录运行。脚本需要 Ubuntu 等 Linux 环境中的 `bash`、`curl`、`python3`、`sha256sum` 和 `flock`。Windows PowerShell 可使用现有 WSL 发行版，Linux 上省略命令前的 `wsl.exe`。

```powershell
wsl.exe bash scripts/qwen-local.sh setup
wsl.exe bash scripts/qwen-local.sh serve
```

保持模型服务的终端运行。在 `relaxbook.local.json`（或 `RELAXBOOK_CONFIG` 指定的文件）中设 `"qwen_local_port": 8777`，然后启动或重启 RelaxBook。网页可试听三种离线音色，也可在整书音色下拉框中选择任一音色生成。端口占用时可用 `serve 8877`，并将配置中的端口改为相同值。已有兼容的本机 speech.cpp 服务也可以直接配置其端口。

安装脚本校验固定版本的运行器和模型。下载中断可重新运行 `setup` 续传。也可以从以下发布页下载 `speech-0.11.2-linux-x64-cpu.zip` 和 `Qwen3-TTS-12Hz-1.7B-CustomVoice-Q4_K.gguf`，放在 `.local/qwen3-tts` 后运行 `setup` 校验并解压。网页提供联机和离线选项。选定离线音色后，服务不可用时结果页会提示检查服务再重试。[运行器发布页](https://github.com/nyosegawa/speech.cpp/releases/tag/v0.11.2) · [量化模型](https://huggingface.co/sakasegawa/Qwen3-TTS-12Hz-1.7B-CustomVoice-GGUF)

复制 `relaxbook.example.json` 为 `relaxbook.local.json` 可调整端口、工具路径、`data_dir` 和 `model_dir`。也可通过环境变量 `RELAXBOOK_CONFIG` 指向配置文件。默认的数据库及模型位于 Windows 的 `%LOCALAPPDATA%\RelaxBook` 或 macOS/Linux 的 `~/.local/share/RelaxBook`。浏览器关闭后，启动终端继续运行转换。退出并重启后，在结果页点击「继续」恢复中断任务。

使用早期音色生成的成品可在历史任务中下载。继续这些任务时，请使用原来的模型、配置和 Python 环境。

## 验证开发版本

用空数据目录启动 RelaxBook，并启用 Qwen3-TTS 离线服务。随后在仓库根目录运行下方命令。Windows 将 `python` 换成虚拟环境的 `.venv\Scripts\python.exe`，运行前设置 `$env:PYTHONPATH = '.'`。macOS/Linux 先运行 `export PYTHONPATH=.`。

```sh
python tests/smoke.py http://127.0.0.1:8765 path/to/evidence
```

该检查验证三种离线音色试听，从三章 EPUB 生成并核对 M4B 章节、分章 MP3、ZIP 和结果数据。Qwen 两种模式的核对步骤、真机播放和换机复装见 [验收清单](docs/ACCEPTANCE.md)。

Windows 首轮实测覆盖四种可读格式、扫描 PDF 提示、三种普通话及一种英文样音、浏览器下载、M4B 与 MP3 文件核对、工作进程退出后续跑，以及封装失败后的重试。换机复装、Android/iOS 真机和真实长书听感按验收清单继续记录。

## 在另一台电脑接手

接手开发的模型或开发者须自行检查新电脑的 SSH 环境和 USTC 仓库访问权限，选择该机已配置的 SSH 客户端、密钥或主机别名。本项目在当前 Windows 电脑上使用 WSL Git 和 WSL 内的 SSH 密钥。新电脑可使用本机已授权的身份，或生成专用密钥，将公钥添加到有仓库权限的 USTC 账号。私钥留在生成它的电脑上。

```sh
git ls-remote git@git.ustc.edu.cn:luogd/relaxbook.git refs/heads/codex/relaxbook-v1
git clone --branch codex/relaxbook-v1 git@git.ustc.edu.cn:luogd/relaxbook.git
cd relaxbook
```

已检出仓库时，核对 `git remote -v` 后运行 `git fetch origin`。若认证失败，自行用 `ssh -T git@git.ustc.edu.cn` 检查身份、公钥授权和主机配置。需要账号所有者授予权限时，提供公钥指纹和实际错误信息。

## 实现交接

开发与交付时使用[第一版验收清单](docs/ACCEPTANCE.md)，其中列有样书、成品核对方法、真机步骤和结果记录模板。

继续开发时请阅读 [产品意图](docs/intent.md)、[实现规格](docs/SPEC.md) 和 [验收清单](docs/ACCEPTANCE.md)。变更导出流程后，核对结果页与下载成品，再验证手机导入后的章节跳转。

软件从同一 Git 版本安装到各台电脑，个人书籍、缓存、模型及音频保存在本机数据目录。云服务器部署留到后续版本。
