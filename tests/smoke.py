"""Run against an isolated RelaxBook instance with the real model and codecs."""
import io
import json
import sys
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from samples import CHAPTERS, sample_epub
from relaxbook.audio import probe, run
from relaxbook.store import settings


BASE = sys.argv[1]
OUTPUT = Path(sys.argv[2]).resolve()
CONFIG = settings()


def request(url, method="GET", body=None, content_type="application/json"):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    req = Request(BASE + url, method=method, data=body, headers={"Content-Type": content_type})
    with urlopen(req, timeout=30) as response:
        return response.read()


def api(url, method="GET", body=None, content_type="application/json"):
    return json.loads(request("/api/" + url, method, body, content_type))


def wait(url, states, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = api(url)
        if result["status"] in states:
            return result
        if result["status"] == "failed":
            raise AssertionError(result)
        time.sleep(0.3)
    raise TimeoutError(url)


def upload(path):
    body = (f'--sample\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + path.read_bytes() + b"\r\n--sample--\r\n"
    return api("books", "POST", body, "multipart/form-data; boundary=sample")["book_id"]


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source = sample_epub(OUTPUT / "sample.epub")
    book_id = upload(source)
    book = wait(f"books/{book_id}", {"ready"})
    assert upload(source) == book_id
    assert [c["title"] for c in book["chapters"]] == [name for name, _ in CHAPTERS]
    for chapter, (_, expected) in zip(book["chapters"], CHAPTERS):
        body = api(f"books/{book_id}/chapters/{chapter['id']}")["text"]
        assert expected in body
    available = api("voices")
    assert all(voice["engine"] in {"qwen", "qwen_local"} for voice in available)
    voices = [voice for voice in available if voice["engine"] == "qwen_local"]
    assert {voice["speaker"] for voice in voices} == {"serena", "vivian"}
    chosen = next(voice for voice in voices if voice["speaker"] == "vivian")
    for voice in voices:
        sample = "清 晨的列 车驶出站 台 。"
        preview = api("previews", "POST", {"text": sample, "voice": voice["id"], "speed": 1})["id"]
        result = wait(f"previews/{preview}", {"complete"})
        assert result["voice"] == voice
        clip = request(result["url"])
        assert clip[:4] == b"RIFF"
        if voice == chosen:
            cleaned = api("previews", "POST", {
                "text": "清晨的列车驶出站台。", "voice": voice["id"], "speed": 1,
            })["id"]
            assert request(wait(f"previews/{cleaned}", {"complete"})["url"]) == clip
    config = {"book_id": book_id, "revision": book["revision"], "voice": chosen["id"],
              "speed": 1, "request_id": "smoke-" + str(time.time_ns())}
    job_id = api("jobs", "POST", config)["job_id"]
    assert api("jobs", "POST", config)["job_id"] == job_id
    result = wait(f"jobs/{job_id}", {"complete"}, seconds=600)
    assert result["voice"] == chosen
    assert result["done"] == result["total"]
    assert result["completed_chapters"] == result["planned_chapters"] == 3
    names = ["001 - 序言.mp3", "002 - 出发.mp3", "003 - 出发.mp3"]
    for i, (row, name) in enumerate(zip(result["chapters"], names)):
        assert row["filename"] == name
        out = OUTPUT / name
        out.write_bytes(request(f"/api/artifacts/{job_id}/{row['id']}"))
        assert out.stat().st_size == row["artifact"]["bytes"]
        assert probe(CONFIG["ffprobe"], out)[0] == row["artifact"]["duration_ms"]
        assert i == 0 or result["chapters"][i-1]["end_ms"] == row["start_ms"]
    output = OUTPUT / result["artifact"]["name"]
    output.write_bytes(request(f"/api/artifacts/{job_id}/m4b"))
    duration, chapter_info = probe(CONFIG["ffprobe"], output)
    assert duration == result["duration_ms"]
    assert output.stat().st_size == result["artifact"]["bytes"]
    assert [item["tags"]["title"] for item in chapter_info] == [name for name, _ in CHAPTERS]
    assert [round(float(item["start_time"]) * 1000) for item in chapter_info] == [
        row["start_ms"] for row in result["chapters"]]
    run([CONFIG["ffmpeg"], "-v", "error", "-xerror", "-i", output, "-map", "0:a:0", "-f", "null", "-"])
    api(f"jobs/{job_id}/zip", "POST")
    for _ in range(100):
        if api(f"jobs/{job_id}")["archive_status"] == "complete":
            break
        time.sleep(0.3)
    with zipfile.ZipFile(io.BytesIO(request(f"/api/artifacts/{job_id}/zip"))) as archive:
        assert archive.namelist() == names
    (OUTPUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: {len(voices)} offline samples, 3 tagged MP3s, chaptered M4B and ZIP ({duration} ms)")


if __name__ == "__main__":
    main()
