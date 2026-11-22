import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


def settings():
    path = Path(os.environ.get("RELAXBOOK_CONFIG", "relaxbook.local.json"))
    supplied = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "RelaxBook"
    supplied.setdefault("data_dir", str(root / "data"))
    supplied.setdefault("model_dir", str(root / "models" / "kokoro-int8-multi-lang-v1_1"))
    supplied["data_dir"] = str(Path(supplied["data_dir"]).expanduser().resolve())
    supplied["model_dir"] = str(Path(supplied["model_dir"]).expanduser().resolve())
    return {"port": 8765, "threads": 2, "ffmpeg": "ffmpeg", "ffprobe": "ffprobe",
            "calibre": "ebook-convert", **supplied}


def uid():
    return uuid.uuid4().hex


class Store:
    def __init__(self, config):
        self.root = Path(config["data_dir"])
        for directory in ["sources", "cache", "jobs", "previews"]:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY (kind, id)
                );
            """)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.root / "relaxbook.sqlite", timeout=15)
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def read(db, kind, key):
        row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def put(db, kind, value):
        db.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                   (kind, value["id"], json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def all(db, kind):
        rows = db.execute("SELECT body FROM records WHERE kind=? ORDER BY rowid DESC", (kind,))
        return [json.loads(row[0]) for row in rows]

    def get(self, kind, key):
        with self.connect() as db:
            return self.read(db, kind, key)

    def update(self, kind, key, **fields):
        with self.connect(write=True) as db:
            record = self.read(db, kind, key)
            record.update(fields)
            self.put(db, kind, record)
            return record
