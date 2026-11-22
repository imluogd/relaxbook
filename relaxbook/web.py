import hashlib
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audio import chapter_name, safe_name, speech_chunks, voice_options
from .store import Store, settings, uid

config = settings()
store = Store(config)
app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static), name="static")


@app.middleware("http")
async def local_origin(request, call_next):
    if request.method not in {"GET", "HEAD"}:
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers['host']}":
            return JSONResponse({"detail": "请从 RelaxBook 本地网页提交操作。"}, 403)
    return await call_next(request)


class ChapterEdit(BaseModel):
    title: str = Field(max_length=1000)
    text: str = Field(max_length=5_000_000)
    source: str = Field(max_length=2000)
    skip: bool = False


class RevisionEdit(BaseModel):
    revision: str
    chapters: list[ChapterEdit] = Field(min_length=1, max_length=5000)


class PreviewInput(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    voice: int
    speed: float = Field(ge=0.7, le=1.5)


class JobInput(BaseModel):
    book_id: str
    revision: str
    chapter_ids: list[str] | None = Field(default=None, min_length=1, max_length=5000)
    voice: int
    speed: float = Field(ge=0.7, le=1.5)
    request_id: str = Field(min_length=1, max_length=128)


def find(kind, key, db=None):
    item = store.get(kind, key) if db is None else store.read(db, kind, key)
    if item is None:
        raise HTTPException(404, "找不到记录。")
    return item


def voice_by_id(voice_id):
    voice = next((v for v in voice_options(config) if v["id"] == voice_id), None)
    if voice is None:
        raise HTTPException(422, "请选择已配置的 Qwen 音色。")
    return voice


@app.get("/")
def index():
    return FileResponse(static / "index.html")


@app.get("/api/books")
def library():
    with store.connect() as db:
        return store.all(db, "book")


@app.post("/api/books", status_code=202)
def upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".epub", ".pdf", ".mobi", ".azw3"}:
        raise HTTPException(415, "请选择 EPUB、PDF、MOBI 或 AZW3。")
    key = uid()
    folder = store.root / "sources" / key
    folder.mkdir()
    path = folder / f"source{suffix}"
    digest, size = hashlib.sha256(), 0
    accepted = False
    try:
        with path.open("wb") as stream:
            while data := file.file.read(1024**2):
                size += len(data)
                if size > 250 * 1024**2:
                    raise HTTPException(413, "源文件最大 250 MB。")
                digest.update(data)
                stream.write(data)
        if size == 0:
            raise HTTPException(422, "文件为空。")
        with store.connect(write=True) as db:
            existing = next((b for b in store.all(db, "book")
                             if b["sha256"] == digest.hexdigest() and b["status"] != "failed"), None)
            if existing:
                return {"book_id": existing["id"]}
            store.put(db, "book", {
                "id": key, "source_name": path.name, "title": Path(file.filename).stem,
                "sha256": digest.hexdigest(), "status": "queued", "format": suffix[1:],
            })
        accepted = True
        return {"book_id": key}
    finally:
        if not accepted:
            path.unlink(missing_ok=True)
            folder.rmdir()


@app.get("/api/books/{key}")
def book_info(key: str):
    book = find("book", key)
    if book["status"] == "ready":
        ch = find("revision", book["revision"])["chapters"]
        book["chapters"] = [{**{k: v for k, v in c.items() if k != "text"},
                             "excerpt": c["text"][:70], "end": c["text"][-70:]} for c in ch]
    with store.connect() as db:
        book["jobs"] = [{"id": j["id"], "voice": j["voice"], "status": j["status"],
                         "filename": j["filename"]} for j in store.all(db, "job") if j["book_id"] == key]
    return book


@app.get("/api/books/{key}/chapters/{chapter}")
def chapter_info(key: str, chapter: str, revision: str | None = None):
    book = find("book", key)
    original = find("revision", revision or book.get("revision"))
    if original["book_id"] == key:
        for record in original["chapters"]:
            if record["id"] == chapter:
                return record
    raise HTTPException(404, "找不到章节。")


@app.put("/api/books/{key}/revision")
def save_revision(key: str, edit: RevisionEdit):
    if any(not c.text.strip() for c in edit.chapters if not c.skip):
        raise HTTPException(422, "所有朗读章节都需要有正文。")
    with store.connect(write=True) as db:
        book = find("book", key, db)
        if edit.revision != book.get("revision"):
            raise HTTPException(409, "章节版本已更新，请重新打开书籍。")
        number = book["revision_no"] + 1
        revision = key + "-" + str(number)
        store.put(db, "revision", {"id": revision, "book_id": key, "chapters": [
            {**c.model_dump(), "id": str(i), "text": c.text.strip()}
            for i, c in enumerate(edit.chapters, 1)
        ]})
        store.put(db, "book", {**book, "revision": revision, "revision_no": number})
    return {"revision": revision}


@app.get("/api/voices")
def voices():
    return voice_options(config)


@app.post("/api/previews", status_code=202)
def preview(request: PreviewInput):
    if not request.text.strip():
        raise HTTPException(422, "请填写试听正文。")
    item = {**request.model_dump(), "id": uid(), "status": "queued", "voice": voice_by_id(request.voice)}
    with store.connect(write=True) as db:
        store.put(db, "preview", item)
    return {"id": item["id"]}


@app.get("/api/previews/{key}")
def preview_status(key: str):
    item = find("preview", key)
    if item["status"] == "complete":
        item["url"] = f"/api/previews/{key}/audio"
    return item


@app.get("/api/previews/{key}/audio")
def preview_audio(key: str):
    item = find("preview", key)
    if item["status"] != "complete":
        raise HTTPException(409, "样音仍在准备。")
    return FileResponse(store.root / "previews" / f"{key}.wav", media_type="audio/wav")


@app.post("/api/jobs", status_code=202)
def create_job(request: JobInput):
    with store.connect(write=True) as db:
        book = find("book", request.book_id, db)
        existing = [j for j in store.all(db, "job") if j["book_id"] == book["id"]]
        duplicate = next((j for j in existing if j["request_id"] == request.request_id), None)
        if duplicate:
            return {"job_id": duplicate["id"]}
        voice = voice_by_id(request.voice)
        if request.revision != book.get("revision"):
            raise HTTPException(409, "请打开并核对最新章节版本。")
        revision = find("revision", request.revision, db)
        chapters = revision["chapters"]
        active = [(number, c) for number, c in enumerate(chapters, 1) if not c["skip"]]
        if not active:
            raise HTTPException(422, "至少需要一个朗读章节。")
        scope = ""
        if request.chapter_ids is None:
            planned = list(enumerate((c for _, c in active), 1))
            filename_count = len(planned)
        else:
            selected = set(request.chapter_ids)
            planned = [(number, c) for number, c in active if c["id"] in selected]
            if len(planned) != len(request.chapter_ids):
                raise HTTPException(422, "请选择不同且未跳过的章节。")
            filename_count = len(chapters)
            scope = f" - 第 {planned[0][0]} 章" if len(planned) == 1 else f" - 选定 {len(planned)} 章"
        total = sum(len(list(speech_chunks(c["text"], voice))) for _, c in planned)
        index = len(existing) + 1
        label = safe_name(voice["label"], "语音")
        filename = f"{safe_name(book['title'], '有声书')}{scope} - {label} - v{index:02}.m4b"
        job = {
            **request.model_dump(), "id": uid(), "title": book["title"], "voice": voice,
            "filename": filename, "status": "queued", "total": total, "done": 0, "clean_text": True,
            "chapters": [{"id": c["id"], "index": i, "title": c["title"] or "未命名章节",
                          "filename": chapter_name(i, filename_count, c["title"]), "status": "pending"}
                         for i, c in planned],
        }
        store.put(db, "job", job)
    return {"job_id": job["id"]}


@app.get("/api/jobs/{key}")
def job_info(key: str):
    job = find("job", key)
    job["completed_chapters"] = sum(c["status"] == "complete" for c in job["chapters"])
    job["planned_chapters"] = len(job["chapters"])
    job["duration_ms"] = (job.get("artifact") or {}).get("duration_ms")
    return job


@app.post("/api/jobs/{key}/{action}", status_code=202)
def control(key: str, action: str):
    with store.connect(write=True) as db:
        job = find("job", key, db)
        current = job["status"]
        if action == "pause" and current in {"queued", "running", "pausing", "paused"}:
            job["status"] = "pausing" if current in {"running", "pausing"} else "paused"
        elif action in {"resume", "retry"} and current in {"paused", "failed", "queued"}:
            job.update(status="queued", error=None)
        elif action == "zip" and current == "complete":
            if job.get("archive_status") != "complete":
                job["archive_status"] = "queued"
        else:
            raise HTTPException(409, "当前任务状态不支持该操作。")
        store.put(db, "job", job)
    return {"status": job["status"]}


@app.get("/api/artifacts/{job_id}/{artifact_id}")
def download(job_id: str, artifact_id: str):
    job = find("job", job_id)
    if artifact_id == "m4b":
        name = (job.get("artifact") or {}).get("name")
    elif artifact_id == "zip":
        name = job.get("archive")
    else:
        row = next((c for c in job["chapters"] if c["id"] == artifact_id), {})
        name = (row.get("artifact") or {}).get("name")
    path = store.root / "jobs" / job_id / name if name else None
    if path is None or not path.is_file():
        raise HTTPException(404, "文件尚未生成或已被移除。")
    return FileResponse(path, filename=name)
