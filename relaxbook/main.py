"""FastAPI application entry point for RelaxBook.

``uvicorn relaxbook.main:app`` serves the API (``/api/...``), the three HTML
pages (``/``, ``/books/{id}``, ``/jobs/{id}``) and the static assets under
``/static``.  Everything is bound to the data directory described by
``Settings``; the web directory is resolved relative to this file, never
relative to the current working directory.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import db
from .api import build_router
from .config import Settings, load_settings

logger = logging.getLogger("relaxbook.main")

WEB_ROOT = Path(__file__).resolve().parent / "web"
TEMPLATES_DIR = WEB_ROOT / "templates"
STATIC_DIR = WEB_ROOT / "static"

STATE_LABELS = {
    # book states
    "parsing": "解析中",
    "ready": "待核对",
    "failed": "导入失败",
    # job states
    "queued": "排队中",
    "synthesizing": "合成中",
    "packaging": "封装中",
    "done": "已完成",
    "paused": "已暂停",
}


def _default_settings() -> Settings:
    """Load settings from the system default data directory.

    The location comes from :func:`relaxbook.config.default_data_dir`
    (``%LOCALAPPDATA%\\RelaxBook`` on Windows, etc.) and is *never* replaced by
    a self-invented fallback path: on Windows paths are case-insensitive, so a
    fallback such as ``<temp>/RelaxBook`` next to this repository would point at
    the ``relaxbook`` source package itself.  When the default location is not
    writable we stop with an explicit Chinese error instead.
    """
    try:
        return load_settings()
    except OSError as exc:
        raise RuntimeError(
            "无法创建默认数据目录。请用 --data-dir 或环境变量 RELAXBOOK_DATA_DIR "
            f"指定一个可写的数据目录（{exc}）。"
        ) from exc


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application for ``settings`` (loaded when omitted)."""
    settings = settings or _default_settings()
    settings.ensure_dirs()
    db.init_db(settings.db_path)

    app = FastAPI(
        title="RelaxBook",
        version="1.0.0",
        description="个人电子书听书工具：导入、核对目录、试听音色、生成 M4B 与分章 MP3。",
    )
    app.state.settings = settings

    app.include_router(build_router(settings))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    def _connect():
        return db.connect(settings.db_path)

    # -- pages ----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def library_page(request: Request) -> Any:
        conn = _connect()
        try:
            books = db.list_books(conn)
            for book in books:
                book["warnings"] = db.loads(book.get("warnings"), [])
                book["state_label"] = STATE_LABELS.get(book["status"], book["status"])
                jobs = db.list_jobs(conn, int(book["id"]), limit=10)
                for job in jobs:
                    job["state_label"] = STATE_LABELS.get(job["state"], job["state"])
                book["jobs"] = jobs
        finally:
            conn.close()
        return templates.TemplateResponse(
            request, "library.html", {"books": books, "title": "书库"}
        )

    @app.get("/books/{book_id}", response_class=HTMLResponse)
    def book_page(request: Request, book_id: int) -> Any:
        conn = _connect()
        try:
            row = db.get_book(conn, book_id)
            if row is None:
                raise HTTPException(status_code=404, detail="书籍不存在")
            book: dict[str, Any] = dict(row)
            book["warnings"] = db.loads(book.get("warnings"), [])
        finally:
            conn.close()
        return templates.TemplateResponse(
            request,
            "book.html",
            {"book": book, "title": "目录与正文"},
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int) -> Any:
        conn = _connect()
        try:
            row = db.get_job(conn, job_id)
            if row is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            job: dict[str, Any] = dict(row)
            book_row = db.get_book(conn, int(job["book_id"]))
            book: dict[str, Any] | None = dict(book_row) if book_row is not None else None
        finally:
            conn.close()
        return templates.TemplateResponse(
            request,
            "job.html",
            {
                "job": job,
                "book": book,
                "state_label": STATE_LABELS.get(job["state"], job["state"]),
                "title": "转换结果",
            },
        )

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    """``python -m relaxbook.main [--data-dir DIR] [--host H] [--port P]``."""
    parser = argparse.ArgumentParser(
        prog="relaxbook", description="RelaxBook 本机听书服务（网页 + 后台接口）"
    )
    parser.add_argument("--data-dir", default=None, help="数据目录（默认使用系统用户数据目录）")
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.data_dir) if args.data_dir else _default_settings()
    except (OSError, RuntimeError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = int(args.port)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    import uvicorn

    url = f"http://{settings.host}:{settings.port}/"
    print(f"RelaxBook 已启动：{url}", flush=True)
    uvicorn.run(create_app(settings), host=settings.host, port=int(settings.port))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
