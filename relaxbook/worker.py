import logging
import time
import zipfile
from pathlib import Path

from filelock import FileLock

from .audio import (
    VOICE_PACKS, EdgeNarrator, LocalQwenNarrator, Narrator, QwenNarrator, VitsNarrator,
    encode, pack_directory, probe, speech_chunks, wave_info,
)
from .books import parse
from .store import Store, settings

LOG = logging.getLogger(__name__)


def file_record(path, duration, **fields):
    return {"id": path.stem, "name": path.name, "bytes": path.stat().st_size,
            "duration_ms": duration, **fields}


def import_book(store, config, book):
    path = store.root / "sources" / book["id"] / book["source_name"]
    try:
        title, author, chapters, warnings, cover = parse(path, config["calibre"])
        if not chapters:
            raise ValueError("没有找到可朗读正文。")
        for index, item in enumerate(chapters, 1):
            item["id"] = str(index)
        if cover:
            filename = "cover.png" if cover.startswith(b"\x89PNG") else "cover.jpg"
            (path.parent / filename).write_bytes(cover)
        else:
            filename = None
        revision = {"id": book["id"] + "-1", "book_id": book["id"], "chapters": chapters}
        with store.connect(write=True) as db:
            store.put(db, "revision", revision)
            store.put(db, "book", {**book, "status": "ready", "title": title or book["title"],
                                  "author": author, "cover": filename, "warnings": warnings,
                                  "revision": revision["id"], "revision_no": 1})
    except Exception as error:
        LOG.exception("Book import failed")
        store.update("book", book["id"], status="failed",
                     error=str(error) if isinstance(error, (ValueError, RuntimeError))
                     else "文件无法解析，请检查格式和内容。")


def render_preview(store, narrator, task):
    try:
        files = [narrator.render(store.root / "cache", text, task["voice"], task["speed"])
                 for text in speech_chunks(task["text"], task["voice"])]
        from .audio import combine
        output = store.root / "previews" / f"{task['id']}.wav"
        temp = output.with_suffix(".partial.wav")
        combine(files, temp)
        temp.replace(output)
        store.update("preview", task["id"], status="complete")
    except Exception as error:
        LOG.exception("Preview failed")
        store.update("preview", task["id"], status="failed", error=str(error))


def checkpoint(store, job_id, **update):
    with store.connect(write=True) as db:
        saved = store.read(db, "job", job_id)
        pause_requested = saved["status"] == "pausing"
        saved.update(update)
        if pause_requested:
            saved["status"] = "paused"
        store.put(db, "job", saved)
        return saved["status"] == "paused"


def was_exported(config, path, record):
    if record is None or not path.is_file() or path.stat().st_size != record["bytes"]:
        return False
    try:
        return probe(config["ffprobe"], path)[0] == record["duration_ms"]
    except (OSError, RuntimeError, ValueError):
        return False


def export_job(store, config, narrator, job):
    job_id = job["id"]
    revision = store.get("revision", job["revision"])
    book = store.get("book", revision["book_id"])
    directory = store.root / "jobs" / job_id
    directory.mkdir(exist_ok=True)
    all_segments, boundaries = [], []
    cursor = done = 0
    # Older jobs keep their original chunk counts and cache keys.
    clean_text = job.get("clean_text", False)
    chapters = {c["id"]: c for c in revision["chapters"] if not c["skip"]}
    rows = job["chapters"]
    try:
        model = narrator.fingerprint
        if job.get("model") and model != job["model"]:
            raise ValueError("模型与原任务不同，请恢复原模型或创建新版本。")
        if checkpoint(store, job_id, model=model, done=0):
            return
        for row in rows:
            chapter = chapters[row["id"]]
            if checkpoint(store, job_id, current=row["title"]):
                return
            parts = []
            try:
                for text in speech_chunks(chapter["text"], job["voice"], clean=clean_text):
                    parts.append(narrator.render(store.root / "cache", text, job["voice"], job["speed"]))
                    done += 1
                    if checkpoint(store, job_id, done=done):
                        return
                end = cursor + sum(wave_info(p).nframes for p in parts)
                boundaries.append((row["title"], cursor, end))
                cursor = end
                all_segments.extend(parts)
                path = directory / row["filename"]
                if not was_exported(config, path, row.get("artifact")):
                    duration, _ = encode(
                        config["ffmpeg"], config["ffprobe"], parts, path, book["title"],
                        book["author"], row["title"], track=row["index"],
                    )
                    row["artifact"] = file_record(path, duration)
                row.update(status="complete", error=None)
            except Exception as error:
                row.update(status="failed", error=str(error))
                raise
            if checkpoint(store, job_id, chapters=rows):
                return
        if checkpoint(store, job_id, status="packaging"):
            return
        path = directory / job["filename"]
        cover = (store.root / "sources" / book["id"] / book["cover"]) if book.get("cover") else None
        if was_exported(config, path, job.get("artifact")):
            duration, actual = probe(config["ffprobe"], path)
        else:
            duration, actual = encode(
                config["ffmpeg"], config["ffprobe"], all_segments, path, book["title"],
                book["author"], book["title"], chapters=boundaries, cover=cover,
            )
        for row, item in zip(rows, actual):
            row["start_ms"] = round(float(item["start_time"]) * 1000)
            row["end_ms"] = round(float(item["end_time"]) * 1000)
        store.update("job", job_id, status="complete", current=None, error=None,
                     chapters=rows, artifact=file_record(path, duration))
    except Exception as error:
        LOG.exception("Conversion failed")
        store.update("job", job_id, status="failed", chapters=rows, error=str(error))


def archive_job(store, job):
    folder = store.root / "jobs" / job["id"]
    name = Path(job["filename"]).with_suffix(".zip").name
    out = folder / name
    try:
        with zipfile.ZipFile(folder / ".partial.zip", "w") as archive:
            for row in job["chapters"]:
                path = folder / row["filename"]
                archive.write(path, path.name)
        (folder / ".partial.zip").replace(out)
        store.update("job", job["id"], archive_status="complete", archive=name)
    except Exception as error:
        LOG.exception("Archive failed")
        store.update("job", job["id"], archive_status="failed", archive_error=str(error))


def work(store, config):
    narrators = {
        "kokoro": Narrator(config["model_dir"], config["threads"]),
        "edge": EdgeNarrator(config),
        "qwen": QwenNarrator(config),
        "qwen_local": LocalQwenNarrator(config),
    }
    for name, pack in VOICE_PACKS.items():
        narrators[name] = VitsNarrator(pack_directory(config, pack), config["threads"])
    with FileLock(str(store.root / "worker.lock"), timeout=0):
        with store.connect(write=True) as db:
            for kind, interrupted in [
                ("book", {"parsing"}), ("job", {"running", "packaging", "pausing"}),
                ("preview", {"running"}),
            ]:
                for item in store.all(db, kind):
                    if item["status"] in interrupted:
                        item["status"] = "paused" if kind == "job" else "queued"
                        store.put(db, kind, item)
        while True:
            selected = None
            with store.connect(write=True) as db:
                for kind, state in [("book", "parsing"), ("preview", "running"), ("job", "running")]:
                    item = next((r for r in reversed(store.all(db, kind)) if r["status"] == "queued"), None)
                    if item:
                        item.update(status=state)
                        store.put(db, kind, item)
                        selected = kind, item
                        break
                if selected is None:
                    item = next((r for r in store.all(db, "job") if r.get("archive_status") == "queued"), None)
                    if item:
                        selected = "archive", item
            if selected is None:
                time.sleep(0.4)
            else:
                kind, item = selected
                if kind == "book":
                    import_book(store, config, item)
                elif kind == "preview":
                    render_preview(store, narrators[item["voice"].get("engine", "kokoro")], item)
                elif kind == "job":
                    export_job(store, config, narrators[item["voice"].get("engine", "kokoro")], item)
                else:
                    archive_job(store, item)


def run_worker(config):
    logging.basicConfig(level=logging.INFO)
    work(Store(config), config)


if __name__ == "__main__":
    run_worker(settings())
