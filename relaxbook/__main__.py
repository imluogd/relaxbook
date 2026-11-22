import argparse
import hashlib
import multiprocessing
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from contextlib import nullcontext
from pathlib import Path
from urllib.request import urlopen

import uvicorn
from filelock import FileLock, Timeout

from . import qwen_local
from .audio import MODEL_NAME, VOICE_PACKS, Narrator, pack_directory, voice_options
from .store import Store, settings
from .worker import run_worker

MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
MODEL_SHA256 = "259c9c8bc906d6078e4e0570421885dabb68b539c2f970fcbb890a58eeaf3e9c"


def setup(config):
    model = Narrator(config["model_dir"], config["threads"])
    install_model(MODEL_NAME, model.directory, MODEL_SHA256, model.model)


def install_model(name, directory, expected, model_path):
    if model_path.is_file():
        print(f"Model found at {directory}")
        return
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory.parent) as tmp:
        archive = Path(tmp) / "model.tar.bz2"
        url = f"{MODEL_URL}/{name}.tar.bz2"
        print(f"Downloading {name} from {url}", flush=True)
        with urlopen(url, timeout=90) as source, archive.open("wb") as out:
            shutil.copyfileobj(source, out)
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != expected:
            raise ValueError("模型压缩包的 SHA-256 与发布版本不符。")
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        (Path(tmp) / name).rename(directory)
        print(f"Installed {name} at {directory}")


def setup_voices(config):
    for pack in VOICE_PACKS.values():
        directory = pack_directory(config, pack)
        install_model(pack["directory"], directory, pack["sha256"], directory / "model.onnx")


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
    voices = voice_options(config)
    if voices:
        print(f"{len(voices)} Qwen voice options configured.")
    else:
        failures.append("请配置百炼 DASHSCOPE_API_KEY 或本机 qwen_local_port。")
    if (port := config.get("qwen_local_port")) is not None:
        try:
            engine, quantization, _ = qwen_local.model_details(port)
            print(f"Local Qwen3-TTS ready on port {port} (speech.cpp {engine}, {quantization}).")
        except RuntimeError as error:
            failures.append(str(error))
    print(f"Data directory: {config['data_dir']}")
    print(f"Free disk: {shutil.disk_usage(config['data_dir']).free / 1024**3:.1f} GiB")
    for error in failures:
        print(error, file=sys.stderr)
    return 1 if failures else 0


def start(config, with_local_qwen=False):
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

    service = qwen_local.running_service(config["qwen_local_port"]) if with_local_qwen else nullcontext()
    with FileLock(str(store.root / "server.lock"), timeout=0), service:
        thread = threading.Thread(target=worker, name="relaxbook-worker", daemon=True)
        thread.start()
        try:
            uvicorn.run("relaxbook.web:app", host="127.0.0.1", port=config["port"])
        finally:
            stop.set()
            thread.join(timeout=6)


def main():
    parser = argparse.ArgumentParser(prog="python -m relaxbook")
    parser.add_argument("command", choices=["setup", "setup-voices", "doctor", "start"])
    parser.add_argument("--with-local-qwen", action="store_true",
                        help="install and start local Qwen3-TTS with the web service")
    args = parser.parse_args()
    command = args.command
    if args.with_local_qwen and command != "start":
        parser.error("--with-local-qwen requires start")
    try:
        config = settings()
        if command == "setup":
            setup(config)
        elif command == "setup-voices":
            setup_voices(config)
        elif command == "doctor":
            return doctor(config)
        else:
            if args.with_local_qwen:
                port = config.get("qwen_local_port") or qwen_local.DEFAULT_PORT
                if port == config["port"]:
                    raise ValueError("RelaxBook 网页与本机 Qwen3-TTS 需要不同的端口。")
                config["qwen_local_port"] = port
                os.environ["RELAXBOOK_QWEN_LOCAL_PORT"] = str(port)
            start(config, args.with_local_qwen)
    except Timeout:
        print("此数据目录已经由另一个 RelaxBook 实例使用。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
