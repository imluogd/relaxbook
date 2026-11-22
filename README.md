# RelaxBook 个人电子书听书工具

在电脑浏览器导入电子书，核对目录并试听音色。同一次转换生成带章节目录的整书 M4B 和自动命名的分章 MP3。手机优先导入 M4B，Android 使用 Voice，iOS 使用 BookPlayer。

## 在另一台电脑接手

接手开发的模型或开发者须自行检查新电脑的 SSH 环境和 USTC 仓库访问权限，选择该机已配置的 SSH 客户端、密钥或主机别名。本项目在当前 Windows 电脑上使用 WSL Git 和 WSL 内的 SSH 密钥。新电脑可使用本机已授权的身份，或生成专用密钥，将公钥添加到有仓库权限的 USTC 账号。私钥留在生成它的电脑上。

```sh
git ls-remote git@git.ustc.edu.cn:luogd/relaxbook.git refs/heads/main
git clone git@git.ustc.edu.cn:luogd/relaxbook.git
cd relaxbook
```

已检出仓库时，核对 `git remote -v` 后运行 `git fetch origin`。若认证失败，自行用 `ssh -T git@git.ustc.edu.cn` 检查身份、公钥授权和主机配置。需要账号所有者授予权限时，提供公钥指纹和实际错误信息。

## 实现交接

阅读 [产品意图](docs/intent.md) 和 [实现规格](docs/SPEC.md)。当前提交是实现起点，选型、安装、整书转换与真机测试均待执行。

1. 先用相同的中文 EPUB 和文本 PDF 验证两个现成 Web 工具，对照 Spec 记录支持项和缺口，再选择复用方案。
2. 首个可运行版本应打通本地网页导入、目录核对、同段多音色试听、整书 M4B 与独立 MP3 下载。每章按序号和章名自动命名，例如 `001 - 序言.mp3`。结果页显示整书文件名、章数和总播放时长，并逐章列出章名、MP3 文件名、起止位置、时长和下载入口。按 Spec 核对显示值与实际音频，验证手机导入后的章节跳转。
3. 按规格验证四种输入、中文命名与重名、断点恢复、双平台手机收听以及第二台电脑复装。每项记录实测环境和结果。

软件从同一 Git 版本安装到各台电脑，个人书籍、缓存、模型及音频保存在本机数据目录。云服务器部署留到后续版本。
