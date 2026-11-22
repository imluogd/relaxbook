import argparse
import hashlib
import multiprocessing
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path
from urllib.request import urlopen

import uvicorn
from filelock import FileLock, Timeout

from .audio import MODEL_NAME, VOICES, Narrator
from .store import Store, settings
from .worker import run_worker

MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{MODEL_NAME}.tar.bz2"
MODEL_SHA256 = "259c9c8bc906d6078e4e0570421885dabb68b539c2f970fcbb890a58eeaf3e9c"


def setup(config):
    model = Narrator(config["model_dir"], config["threads"])
    if model.model.is_file():
        print(f"Model found at {model.directory}")
        return
    model.directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=model.directory.parent) as tmp:
        archive = Path(tmp) / "model.tar.bz2"
        print(f"Downloading Kokoro from {MODEL_URL}", flush=True)
        with urlopen(MODEL_URL, timeout=90) as source, archive.open("wb") as out:
            shutil.copyfileobj(source, out)
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != MODEL_SHA256:
            raise ValueError("模型压缩包的 SHA-256 与发布版本不符。")
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        (Path(tmp) / MODEL_NAME).rename(model.directory)
        print(f"Installed Kokoro at {model.directory}")


def doctor(config):
    Store(config)
    failures = []
    for name in ["ffmpeg", "ffprobe", "calibre"]:
        executable = shutil.which(config[name])
        if executable:
            result = subprocess.run([executable, "--version" if name == "calibre" else "-version"],
                                    capture_output=True, text=True, errors="replace")
            if result.returncode == 0:
                print(result.stdout.splitlines()[0])
                continue
        failures.append(f"{name} unavailable: {config[name]}")
    try:
        narrator = Narrator(config["model_dir"], config["threads"])
        print(f"Kokoro model fingerprint {narrator.fingerprint[:16]}")
        audio = narrator.engine.generate("你好，欢迎使用 RelaxBook。", sid=VOICES[0]["id"])
        if not len(audio.samples):
            raise ValueError("语音模型测试返回空音频。")
        print("Kokoro rendered an offline voice sample.")
    except Exception as error:
        failures.append(str(error))
    print(f"Data directory: {config['data_dir']}")
    print(f"Free disk: {shutil.disk_usage(config['data_dir']).free / 1024**3:.1f} GiB")
    for error in failures:
        print(error, file=sys.stderr)
    return 1 if failures else 0


def start(config):
    store = Store(config)
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            process = multiprocessing.get_context("spawn").Process(
                target=run_worker, args=(config,), daemon=True,
            )
            process.start()
            try:
                while not stop.wait(0.3) and process.is_alive():
                    pass
                if not process.is_alive() and not stop.is_set():
                    print("转换进程已退出。将重新启动，未完成的任务可在网页继续。", flush=True)
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=4)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
            stop.wait(1)

    with FileLock(str(store.root / "server.lock"), timeout=0):
        thread = threading.Thread(target=worker, name="relaxbook-worker", daemon=True)
        thread.start()
        try:
            uvicorn.run("relaxbook.web:app", host="127.0.0.1", port=config["port"])
        finally:
            stop.set()
            thread.join(timeout=6)


def main():
    parser = argparse.ArgumentParser(prog="python -m relaxbook")
    parser.add_argument("command", choices=["setup", "doctor", "start"])
    command = parser.parse_args().command
    config = settings()
    try:
        if command == "setup":
            setup(config)
        elif command == "doctor":
            return doctor(config)
        else:
            start(config)
    except Timeout:
        print("此数据目录已经由另一个 RelaxBook 实例使用。", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
