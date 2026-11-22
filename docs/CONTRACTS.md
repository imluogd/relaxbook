# 内部实现契约 v1

本文件是并行开发的接口约定。`relaxbook/config.py` 和 `relaxbook/db.py` 已实现，其余模块按下述签名实现。
**不得修改本文件以外的他人负责文件；不得 `git commit`。**

## 环境

- 工作目录 `D:\dsh\relaxbook`，虚拟环境 `D:\dsh\relaxbook\.venv`（Python 3.12.7）。
- 运行 Python：`D:\dsh\relaxbook\.venv\Scripts\python.exe`；运行前执行 `Remove-Item Env:PYTHONPATH`（本机全局 PYTHONPATH 有污染）。
- 已安装：fastapi uvicorn jinja2 python-multipart lxml pypdf pdfplumber edge-tts pytest Pillow curl_cffi。
- 需要新依赖时：`... -m pip install --timeout 20 -i http://mirrors.ustc.edu.cn/pypi/simple/ --trusted-host mirrors.ustc.edu.cn --no-cache-dir <pkg>`（本机系统时钟超前导致 HTTPS 证书校验失败，必须用 http 镜像；pip 默认缓存会挂起，必须 `--no-cache-dir`）。
- ffmpeg/ffprobe：`E:\ffmpeg\bin\ffmpeg.exe`、`E:\ffmpeg\bin\ffprobe.exe`（`relaxbook.config.locate_tool` 会自动找到）。
- 测试：`cd D:\dsh\relaxbook; & .venv\Scripts\python.exe -m pytest tests\test_xxx.py -q`。测试路径含中文时注意编码，脚本文件一律 UTF-8。
- 面向用户的错误、提示、网页文案一律中文。

## 通用约定

- 数据目录外置（`Settings.data_dir`），不得写入 Git 工作区；测试用 `tmp_path`。
- 所有文件写入遵循「临时文件 → 校验 → `os.replace` 原子改名」。
- 不允许静默丢正文：任何跳过都要有 `warnings` 记录并在 UI/接口可见。
- 时间统一毫秒整数；秒显示用 `ms/1000`，四舍五入到 1 位小数或整数按 SPEC。

---

## 1. `relaxbook/ingest/types.py`（负责人 A：ingest）

```python
from dataclasses import dataclass, field

@dataclass
class ChapterDraft:
    title: str
    text: str                                   # 已清理的朗读稿正文
    read: bool = True
    source_locator: dict = field(default_factory=dict)  # EPUB: {"href","anchor"} PDF: {"pages":[1,2]}
    warnings: list[str] = field(default_factory=list)

@dataclass
class ParsedBook:
    title: str
    author: str | None
    format: str                                 # epub|pdf|mobi|azw3
    chapters: list[ChapterDraft]
    cover_bytes: bytes | None = None
    cover_ext: str = "jpg"
    warnings: list[str] = field(default_factory=list)
```

## 2. `relaxbook/ingest/__init__.py`（A）

```python
class IngestError(Exception): ...               # message 即用户可见中文原因

SUPPORTED_FORMATS: set[str] = {"epub", "pdf", "mobi", "azw3"}

def detect_format(filename: str, head: bytes | None = None) -> str
    # 扩展名优先，其次 magic（ZIP->epub 可能，PDF->%PDF）。未知格式 raise IngestError("不支持的文件格式…")

def parse_book(path: Path, *, format: str, settings: Settings) -> ParsedBook
    # epub  -> epub.parse_epub(path)
    # pdf   -> pdf.parse_pdf(path)
    # mobi/azw3 -> convert.to_epub(path, settings) 后再 parse_epub
    # 整本无可读正文（如扫描 PDF）raise IngestError("未提取到可读正文，疑似扫描件，需要先 OCR。")
```

## 3. `relaxbook/ingest/epub.py`（A）

```python
def parse_epub(path: Path) -> ParsedBook
```

要求（SPEC「输入格式与目录判定」）：读 `META-INF/container.xml` → OPF；`metadata` 的 dc:title/dc:creator；`manifest`；`spine` 定正文顺序；EPUB3 `nav` 或 EPUB2 `NCX` 取标题与片段锚点；同一 XHTML 内不同锚点切章；目录未覆盖的可读正文列入「待核对」章节（`warnings=["目录未覆盖，按正文补入"]`）；目录锚点失效或多项指向同一位置时记 warning 并回退到按文件切分。用 `lxml.html` 提取文本，保留段落换行（`\n`）。`cover_bytes` 取 OPF `meta name="cover"` 或 manifest `properties="cover-image"`。

## 4. `relaxbook/ingest/pdf.py`（A）

```python
def parse_pdf(path: Path) -> ParsedBook
```

要求：`pypdf` 读 outline（书签）与页数；`pdfplumber` 逐页提取文本。有书签时按书签页码映射章范围（同页多标题定不到就记 warning）；无书签时按标题模式（正则：`第[一二三四五六七八九十百零\d]+[章节卷回]`、`^\s*\d+[\.、]` 等）提出候选章，找不到就**整本作为一章**并 `warnings=["未识别到章节结构，整本作为一章"]`。清理页眉页脚：每页首尾行的重复文本（跨页出现频率高）视为页眉页脚剔除并记 warning。低文本量页（`<40` 可见字符）记 warning 并列出页码。**整本文本量 < 100 字符 → raise IngestError**（扫描件）。`source_locator={"pages":[start,end]}`。

## 5. `relaxbook/ingest/convert.py`（A）

```python
def to_epub(path: Path, settings: Settings, work_dir: Path) -> Path
```

`ebook-convert <src> <dst> --no-images=false` 调 calibre；未配置/找不到 calibre 时 `raise IngestError("未找到 calibre（ebook-convert），无法转换 MOBI/AZW3。请安装 calibre 或在 config.json 配置 calibre_convert。")`；转换失败带上 stderr 尾部。

## 6. `relaxbook/textnorm.py`（A）

```python
RULES_VERSION = "1"

def clean_text(raw: str) -> str
    # 统一换行、去零宽字符、修连字符断行（"-\\n" -> ""）、合并软换行、压缩空白、
    # 去掉纯页码行、保留段落分隔（两个 \n）

def normalize_title(raw: str) -> str            # 去空白/序号噪声，空则 "未命名章节"

def segment_text(text: str, *, max_chars: int = 220) -> list[str]
    # 先按段落，再按中文标点 。！？；…： 切分（标点留在句尾），
    # 过短句（<8 字）与相邻句合并直到 >= 20 字或 end；
    # 单句 > max_chars 时按 ，、, 与空格再切，仍超长则硬切。
    # 返回去空白后的非空片段；不得丢字（拼回去 == 清理后的正文，忽略空白差异）

def segment_cache_key(text: str, *, engine: str, model: str, voice: str, rate: str) -> str
    # sha256(json.dumps({"t":text,"e":engine,"m":model,"v":voice,"r":rate,"rules":RULES_VERSION},
    #                   ensure_ascii=False, sort_keys=True)).hexdigest()
```

## 7. `relaxbook/naming.py`（A2：audio）

```python
def sanitize_title(title: str, *, fallback: str = "未命名章节", max_len: int = 80) -> str
    # 替换 \\ / : * ? " < > | 和控制字符为全角或空格；去尾部空格与句点；截断；空则 fallback
def seq_width(total: int) -> int                # max(3, len(str(total)))
def mp3_filename(index: int, title: str, total: int) -> str      # "001 - 序言.mp3"
def m4b_filename(book_title: str, voice: str, version: int) -> str  # "书名 - 音色 - v01.m4b"
def allocate_version_dir(parent: Path) -> tuple[int, Path]      # parent/v01, v02 ... 取未存在的最小号并 mkdir
```

## 8. `relaxbook/audio.py`（A2）

纯 ffmpeg/ffprobe 封装，均接受显式工具路径（`ffmpeg: str`、`ffprobe: str`），失败 `raise AudioError(msg)`。

```python
class AudioError(Exception): ...

def probe(path: Path, *, ffprobe: str) -> dict
    # ffprobe -show_format -show_streams -of json；返回 {"duration_ms","sample_rate","channels","bit_rate","format_name"}
def probe_chapters(path: Path, *, ffprobe: str) -> list[dict]
    # {"id","title","start_ms","end_ms"}，无章节返回 []
def verify_decodable(path: Path, *, ffmpeg: str) -> None     # ffmpeg -v error -xerror -i p -map 0:a:0 -f null -
def decode_to_wav(src: Path, dst: Path, *, ffmpeg: str) -> None  # -ar 24000 -ac 1 -c:a pcm_s16le
def wav_frames(path: Path) -> int                             # wave 模块读帧数（只读头）
def wav_duration_ms(path: Path) -> int                        # frames*1000//framerate
def concat_wavs(wavs: Sequence[Path], out: Path) -> None       # wave 模块逐帧复制，统一 24000/1/16bit
def encode_mp3(wav: Path, out: Path, *, bitrate: str, ffmpeg: str) -> None   # libmp3lame, -ar 24000 -ac 1
def encode_m4b(wav: Path, out: Path, *, title: str, author: str | None,
               chapters: Sequence[dict], bitrate: str, ffmpeg: str) -> None
    # chapters: [{"title","start_ms","end_ms"}]；写 FFMETADATA1 章节文件，
    # -map_metadata 1 -c:a aac -b:a <bitrate> -f ipod out.m4b
```

## 9. `relaxbook/packaging.py`（A2）

```python
def assemble_chapter(segment_wavs: Sequence[Path], out_mp3: Path, *,
                     bitrate: str, ffmpeg: str, ffprobe: str) -> dict
    # concat -> encode -> verify_decodable -> probe -> 原子 os.replace
    # 返回 {"path": str, "bytes": int, "duration_ms": int}

def assemble_m4b(chapters: Sequence[dict], out_path: Path, *, title: str, author: str | None,
                 cover_path: Path | None, bitrate: str, ffmpeg: str, ffprobe: str) -> dict
    # chapters: [{"index": int, "title": str, "segment_wavs": [Path,...]}]
    # 用 wav_frames 累计实际 PCM 样本数得到每章 start_ms/end_ms（24000Hz），
    # concat 全部 -> encode_m4b -> probe_chapters 核对章数/标题/时间单调性 -> 原子改名
    # 返回 {"path": str, "bytes": int, "duration_ms": int,
    #       "chapters": [{"index","title","start_ms","end_ms","duration_ms"}]}

def verify_m4b(path: Path, *, ffmpeg: str, ffprobe: str, expected_chapters: int | None = None) -> dict
    # {"ok": bool, "duration_ms": int, "chapters": [...]}；不 ok 时 "error": str
```

## 10. `relaxbook/tts/base.py`、`edge.py`、`kokoro.py`、`__init__.py`（A3：worker-tts）

```python
@dataclass
class Voice:
    id: str; name: str; language: str    # "zh-CN" / "en-US"
    gender: str                          # "Female"/"Male"
    engine: str                          # "edge"

class TTSError(Exception): ...

class Engine(Protocol):
    name: str
    model: str
    def voices(self) -> list[Voice]: ...
    def synthesize(self, text: str, voice: str, rate: str) -> bytes: ...   # 返回 MP3 字节，24kHz mono 48kbps

def get_engine(name: str, settings) -> Engine
def list_voices(settings) -> list[Voice]
```

`edge.py` 必须使用 **curl_cffi + impersonate="chrome" 的 WebSocket**（直接 OpenSSL/aiohttp 到
`speech.platform.bing.com` 会被重置；`edge_tts` 包的 helper 可复用）。已在本机验证通过的最小实现：

```python
import uuid
from curl_cffi.requests import AsyncSession
from curl_cffi.const import CurlWsFlag
from edge_tts.constants import WSS_URL, WSS_HEADERS, SEC_MS_GEC_VERSION
from edge_tts.drm import DRM
from edge_tts.communicate import date_to_string

url = (f"{WSS_URL}&ConnectionId={uuid.uuid4().hex}"
       f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}")
async with AsyncSession() as session:
    async with session.ws_connect(url, impersonate="chrome",
                                  headers=DRM.headers_with_muid(WSS_HEADERS)) as ws:
        await ws.send_str(config_msg)   # Path:speech.config，metadataoptions 全 false
        await ws.send_str(ssml_msg)     # X-RequestId/Content-Type:application/ssml+xml/Path:ssml
        while True:
            data, flags = await ws.recv(timeout=60)
            if flags & CurlWsFlag.TEXT:   # Path:turn.end 结束；Path:response 里含 error 则抛错
                ...
            elif flags & CurlWsFlag.BINARY:
                n = int.from_bytes(data[0:2], "big")
                if b"Path:audio\r\n" in data[2:2+n]:
                    audio += data[2+n:]
```

- 音色清单：内置精选列表（不联网获取），至少 `zh-CN-XiaoxiaoNeural`(女, 温暖)、`zh-CN-XiaoyiNeural`(女, 活泼)、`zh-CN-YunxiNeural`(男, 年轻)、`zh-CN-YunjianNeural`(男, 沉稳)、`zh-CN-YunyangNeural`(男, 新闻)、`en-US-AriaNeural`(女)、`en-US-GuyNeural`(男)，`Voice.name` 写中文描述。
- `synthesize` 为**同步**方法（内部 `asyncio.run` 包一层），可被多线程并发调用；失败重试 2 次（指数退避），最终 `raise TTSError`。
- `kokoro.py` 只提供占位实现：`class KokoroEngine` 的 `voices()` 返回 `[]`，`synthesize` 抛 `TTSError("本地 Kokoro 引擎未安装（可选的离线方案）")`。`get_engine("kokoro")` 不得在导入时失败。

## 11. `relaxbook/worker.py`（A3）

独立进程：`python -m relaxbook.worker [--data-dir DIR]`。单实例锁（`data_dir/worker.lock`，独占打开）。启动时 `db.reset_running_jobs` 把中断任务重新排队。

```python
class JobRunner:
    def __init__(self, settings: Settings, engine: Engine | None = None): ...
    def run_job(self, job_id: int) -> None: ...
    def loop_forever(self) -> None: ...
```

`run_job` 流程：

1. 打开连接，取 job / revision / chapters（`read=1`）。`engine` 缺省用 `tts.get_engine(job.engine, settings)`。
2. 对每个朗读章用 `textnorm.segment_text` 切片，`segment_cache_key` 计算键；若 job 尚无 segment 记录则 `db.create_segments`，并 `db.set_job_state('synthesizing')` + `freeze_revision`。已有记录则只补 `status!='done'` 的。
3. 并发合成（`ThreadPoolExecutor(max_workers=settings.concurrency)`，默认 4）：
   - 缓存命中：`segments_dir/<key[:2]>/<key>.wav` 存在且 `wav_duration_ms>0` → 直接 `mark_segment_done`，**不得再次调用合成接口**（A5 要核对调用日志）。
   - 未命中：`engine.synthesize(text, voice, rate)` 得 MP3 字节 → 临时文件 → `audio.decode_to_wav` 统一为 24kHz mono → `wav_duration_ms` 校验 > 0 → 原子改名到缓存 → `mark_segment_done`。
   - 失败：`mark_segment_failed`，继续其它片段；重试后仍失败则任务 `failed`，已完成片段保留。
   - 每完成 1 个片段就 `update_job_progress`，并**在每次取新片段前读 job.state**：为 `paused` 则停止取新活、等所有在飞的完成、退出（状态保持 paused）。
4. 某章所有片段 done → 立刻 `packaging.assemble_chapter` 生成 MP3 到 `jobs_dir/<job_id>/v01/<mp3_filename>`，`upsert_artifact(kind='mp3', chapter_idx=…)`，`update_job_progress(done_chapters=…)`。这样第一章可在整书生成中单独下载。
5. 全部章完成：`set_job_state('packaging')` → `assemble_m4b`（章序、标题来自 revision，书名/作者来自 book）→ 校验 → `upsert_artifact(kind='m4b')` → `set_job_result`（m4b_bytes/duration/filename/mp3_complete）→ `set_job_state('done')`。封装失败：`set_job_state('failed', error=…)`，保留已成功 MP3；`POST /api/jobs/{id}/retry` 只重做封装（若 segments 全 done）。
6. 日志：`logging` 写入 `data_dir/logs/worker.log`，每个合成片段记一行 `job=<id> seg=<id> cache=hit|miss synth_ms=<n>`（A5 证据）。

任务状态机：`queued -> synthesizing -> packaging -> done`；任何阶段 `failed`；`paused` 仅由 API 置入。`mp3` 与 `m4b` 两种成品都校验成功才 `done`。

## 12. `relaxbook/api.py`、`relaxbook/main.py`（A4：web-api）

```python
# main.py
def create_app(settings: Settings | None = None) -> FastAPI
app = create_app()      # 供 `uvicorn relaxbook.main:app`
def main(argv=None) -> int   # 解析 --data-dir/--host/--port，起 uvicorn
```

`api.py` 提供 `router = APIRouter(prefix="/api")`，并 `def build_router(settings) -> APIRouter`（便于测试注入）。所有接口只用 `db` + `ingest` + `textnorm` + `tts` + `naming`，**不导入 worker**。上传解析在 `threading.Thread` 中执行（`parse_book` → `create_revision` → `set_book_parsed`/`set_book_failed`）。

接口（响应 JSON 字段与 SPEC 一致）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/books` | multipart `file`；202 `{"book_id","status","duplicate":bool}`；相同 sha256 返回已有书 |
| GET | `/api/books` | `{"books":[{id,title,author,format,status,revision_id,chapter_count,created_at,updated_at,error}]}` |
| GET | `/api/books/{id}` | `{"book":{…},"revision":{id,chapter_count},"chapters":[{"id","idx","title","read","warnings","locator"}],"warnings":[]}` |
| GET | `/api/books/{id}/chapters/{cid}` | `{"chapter":{id,idx,title,text,read,source_locator,warnings}}` |
| PUT | `/api/books/{id}/revision` | body `{"base_revision_id":int,"note":str,"chapters":[{"title","text","read","source_locator"}]}`；返回 `{"revision_id"}`；`base_revision_id != 当前最新` → 409 `{"detail":"版本已过期，请刷新后重试"}` |
| GET | `/api/books/{id}/cover` | 图片或 404 |
| GET | `/api/voices` | `{"engines":[{"name","model","available":bool}],"voices":[{id,name,language,gender,engine}]}`，至少 3 中文 + 1 英文才会 available |
| POST | `/api/previews` | body `{"text":str,"voice":str,"rate":str}`；202 `{"preview_id"}`；后台线程合成到 `previews_dir/{id}.mp3` |
| GET | `/api/previews/{id}` | `{"status","audio_url","duration_ms","error"}`，`audio_url=/api/previews/{id}/audio` |
| GET | `/api/previews/{id}/audio` | FileResponse(mp3) |
| POST | `/api/jobs` | body `{"book_id","revision_id","voice","engine"?, "rate"?,"formats"?}`；同一 revision+voice+rate 未失败时返回既有 job（`request_key` 去重）；202 `{"job_id","duplicate":bool}` |
| GET | `/api/jobs/{id}` | 见下 |
| POST | `/api/jobs/{id}/pause` | 状态置 `paused`（仅 `queued/synthesizing` 合法，否则 409） |
| POST | `/api/jobs/{id}/resume` | `paused -> queued` |
| POST | `/api/jobs/{id}/retry` | `failed -> queued`，同时 `reset_segment_failures` |
| GET | `/api/artifacts/{id}` | FileResponse，`Content-Disposition` 带 UTF-8 文件名 |
| GET | `/api/jobs/{id}/zip` | 按需打包全部 MP3，返回 zip（`zip_packaging` 关闭时 404 或按需生成） |

`GET /api/jobs/{id}` 响应（结果页唯一数据源）：

```json
{
  "job": {"id":1,"state":"done","voice":"zh-CN-XiaoxiaoNeural","engine":"edge",
          "rate":"+0%","created_at":"…","updated_at":"…","error":null,
          "total_segments":30,"done_segments":30,"failed_segments":0,
          "done_chapters":3,"total_chapters":3},
  "book": {"id":1,"title":"示例书","author":"…"},
  "m4b": {"artifact_id":9,"filename":"示例书 - 晓晓 - v01.m4b","bytes":123456,
          "duration_ms":600000,"verified":true},
  "chapters": [
    {"index":1,"title":"序言","read":true,"mp3_filename":"001 - 序言.mp3",
     "mp3_artifact_id":3,"mp3_bytes":20480,"mp3_duration_ms":200000,
     "m4b_start_ms":0,"m4b_end_ms":200000,"state":"done"}
  ]
}
```

- `m4b` 的 `start_ms/end_ms` 与 `duration_ms` 必须来自成品（`packaging.assemble_m4b` 实测值存 `artifact`/`job`），未就绪为 `null`。
- 章节 `state`：有 mp3 artifact 且 verified 为 `done`，否则 `pending`。
- 需要新增 `db` 查询时**不要改 `db.py`**，在 `api.py` 内用 `conn.execute` 直接查。

网页（Jinja2 + 原生 JS，无前端构建）：
- `GET /` 书库：上传表单、书籍列表（书名/状态/章数/时间）、进入书籍或任务。
- `GET /books/{id}` 目录与正文编辑：章节列表可改名、拆分（在光标处）、合并、跳过/恢复、修正错行；试听面板（选 3+ 音色，同段原文，逐个播放）；「开始生成」。
- `GET /jobs/{id}` 转换结果页：进度、当前章、已完成/总片段、失败位置；主按钮「下载有章节的 M4B」；书名/音色/已生成章数/计划章数/总播放时长/文件名/大小；章节表（序号、章名、MP3 文件名、M4B 起止、本章时长、大小、状态、下载）；暂停/继续/重试/打包下载。
- 前端每 2 秒轮询 `/api/…`；页面文案中文；样式简洁（`static/style.css`），不引入外部 CDN。

## 13. `tools/make_samples.py`、`sample/`、端到端测试（A5：验收）

生成并提交可再分发的样书（自写中文，无版权问题）：
- `sample/示例书.epub`：三章，标题依次「序言」「出发」「出发」，首句分别为「清晨的列车」「午后的车站」「傍晚的灯光」，每章末句不同，三段长度不同；EPUB3 nav + NCX 都要有。
- `sample/预期清单.md`：抄录每章标题、首句、末句、一个中段句子。
- `sample/示例书.pdf`：带书签的文本型 PDF（`fpdf2` + `pypdf` 加 outline；未安装可 pip 装）。
- `sample/扫描示例.pdf`：仅含图片、无可提取文本（Pillow 生成）。
- 英文 EPUB、MOBI/AZW3（需 calibre，缺失时跳过并记录）。

## 14. `relaxbook/cli.py`、`scripts/*`、`README.md`（A6：运维）

`python -m relaxbook.cli setup|doctor|start` 与 `scripts/setup.ps1`、`scripts/doctor.ps1`、`scripts/start.ps1`（另加 `.sh` 版本）。
- `setup`：创建/复用 venv，`pip install -r requirements.txt`（用本机镜像参数），写默认 config，检查工具。
- `doctor`：逐项报告 Python 版本、ffmpeg/ffprobe、calibre、数据目录写权限与剩余空间、TTS 引擎可用性（edge 需要联网，可用 1 句试合成探测），中文音色数量；缺失项给出修复命令；有阻塞项时退出码 1。
- `start`：启动 worker 子进程 + uvicorn，端口占用时尝试后续端口并打印实际地址；`Ctrl+C` 一起退出。
