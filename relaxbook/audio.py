import hashlib
import json
import logging
import re
import subprocess
import wave
from functools import cached_property
from importlib.metadata import version
from pathlib import Path

import sherpa_onnx

LOG = logging.getLogger(__name__)
MODEL_NAME = "kokoro-int8-multi-lang-v1_1"
VOICES = [
    {"id": 53, "label": "普通话 53", "language": "zh"},
    {"id": 54, "label": "普通话 54", "language": "zh"},
    {"id": 58, "label": "普通话 58", "language": "zh"},
    {"id": 0, "label": "English 0", "language": "en"},
]


def chunks(text, limit=160):
    for paragraph in text.splitlines():
        remaining = paragraph.strip()
        while len(remaining) > limit:
            boundaries = list(re.finditer(r"[。！？；，、,.!?;:\s]", remaining[:limit]))
            end = boundaries[-1].end() if boundaries and boundaries[-1].end() >= limit // 2 else limit
            yield remaining[:end].strip()
            remaining = remaining[end:].lstrip()
        if remaining:
            yield remaining


def safe_name(text, fallback):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", text).strip(" .")
    return name[:60].rstrip(" .") or fallback


def chapter_name(index, count, title):
    return f"{index:0{max(3, len(str(count)))}d} - {safe_name(title, '未命名章节')}.mp3"


def run(args, timeout=None):
    result = subprocess.run([str(arg) for arg in args], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} 执行失败：{result.stderr.strip()[-500:]}")
    return result.stdout


def wave_info(path):
    with wave.open(str(path), "rb") as stream:
        return stream.getparams()


class Narrator:
    def __init__(self, directory, threads):
        self.directory = Path(directory)
        self.threads = threads

    @cached_property
    def model(self):
        return next((p for p in [self.directory / "model.int8.onnx", self.directory / "model.onnx"]
                     if p.is_file()), self.directory / "model.int8.onnx")

    @cached_property
    def fingerprint(self):
        if not self.model.is_file():
            raise ValueError("本机还没有 Kokoro 模型，请运行 setup。")
        digest = hashlib.sha256(f"segments-v1:sherpa-onnx-{version('sherpa-onnx')}".encode())
        for path in sorted(self.directory.rglob("*")):
            if path.is_file() and path.name not in {"README.md", "LICENSE"}:
                digest.update(path.relative_to(self.directory).as_posix().encode())
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024**2), b""):
                        digest.update(block)
        return digest.hexdigest()

    @cached_property
    def engine(self):
        d = self.directory
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(self.model), voices=str(d / "voices.bin"),
                    tokens=str(d / "tokens.txt"), data_dir=str(d / "espeak-ng-data"),
                    lexicon=",".join(str(d / name) for name in ["lexicon-us-en.txt", "lexicon-zh.txt"]),
                ),
                num_threads=self.threads, provider="cpu",
            ),
            rule_fsts=",".join(str(d / name) for name in ["phone-zh.fst", "date-zh.fst", "number-zh.fst"]),
            max_num_sentences=1,
        )
        if not config.validate():
            raise ValueError("Kokoro 模型文件不完整，请核对配置并重新安装模型。")
        return sherpa_onnx.OfflineTts(config)

    def render(self, directory, text, voice, speed):
        key = hashlib.sha256(json.dumps(
            [self.fingerprint, voice["id"], voice["language"], speed, text],
            ensure_ascii=False,
        ).encode()).hexdigest()
        path = directory / f"{key}.wav"
        try:
            with wave.open(str(path), "rb") as stream:
                if (stream.getnchannels() == 1 and stream.getsampwidth() == 2
                    and stream.getframerate() == 24000 and stream.getnframes() > 0
                    and path.stat().st_size >= stream.getnframes() * 2 + 44):
                    LOG.info("Reused segment %s", key)
                    return path
        except (OSError, EOFError, wave.Error):
            pass
        tmp = path.with_suffix(".partial.wav")
        audio = self.engine.generate(text, sid=voice["id"], speed=speed)
        if not sherpa_onnx.write_wave(str(tmp), audio.samples, audio.sample_rate):
            raise RuntimeError("无法写入语音片段。")
        info = wave_info(tmp)
        if info.nframes <= 0 or (info.nchannels, info.sampwidth, info.framerate) != (1, 2, 24000):
            raise RuntimeError("语音引擎返回空片段或不支持的采样格式。")
        tmp.replace(path)
        LOG.info("Synthesized segment %s", key)
        return path


def combine(inputs, output):
    first = wave_info(inputs[0])
    with wave.open(str(output), "wb") as dest:
        dest.setparams(first)
        for path in inputs:
            with wave.open(str(path), "rb") as source:
                if source.getparams()[:3] != first[:3]:
                    raise ValueError("音频片段采样格式不一致。")
                while block := source.readframes(32768):
                    dest.writeframesraw(block)


def probe(ffprobe, path):
    data = json.loads(run([ffprobe, "-v", "error", "-show_format", "-show_chapters",
                           "-select_streams", "a:0", "-show_streams", "-of", "json", path]))
    duration = round(float(data["format"]["duration"]) * 1000)
    if duration <= 0 or len(data.get("streams", [])) != 1:
        raise ValueError("音频文件无可用音轨。")
    return duration, data.get("chapters", [])


def escape_metadata(text):
    return re.sub(r"([\\=;#])", r"\\\1", text).replace("\r", "").replace("\n", "\\\n")


def encode(ffmpeg, ffprobe, inputs, output, album, artist, title,
           track=None, chapters=None, cover=None):
    prefix = [ffmpeg, "-nostdin", "-v", "error", "-y",
              "-f", "concat", "-safe", "0", "-i", output.with_suffix(".concat")]
    output.with_suffix(".concat").write_text(
        "".join("file '" + path.as_posix().replace("'", "'\\''") + "'\n" for path in inputs),
        encoding="utf-8",
    )
    extra = []
    if chapters is not None:
        meta = output.with_suffix(".metadata")
        meta.write_text(";FFMETADATA1\n" + "".join(
            f"[CHAPTER]\nTIMEBASE=1/24000\nSTART={start}\nEND={end}\ntitle={escape_metadata(name)}\n"
            for name, start, end in chapters
        ), encoding="utf-8")
        prefix += ["-f", "ffmetadata", "-i", meta]
        extra += ["-map_chapters", "1", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart"]
        if cover and cover.is_file():
            prefix += ["-i", cover]
            extra += ["-map", "2:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
    else:
        extra = ["-c:a", "libmp3lame", "-b:a", "64k", "-metadata", f"track={track}"]
    temp = output.with_name(f".partial{output.suffix}")
    run(prefix + ["-map", "0:a:0"] + extra + [
        "-metadata", f"album={album}", "-metadata", f"artist={artist}",
        "-metadata", f"title={title}", temp,
    ])
    duration, actual = probe(ffprobe, temp)
    if chapters is not None:
        expected = [(name, round(a / 24), round(b / 24)) for name, a, b in chapters]
        measured = [(item.get("tags", {}).get("title"),
                     round(float(item["start_time"]) * 1000),
                     round(float(item["end_time"]) * 1000)) for item in actual]
        if len(expected) != len(measured) or any(
            name != found or abs(a-x) > 100 or abs(b-y) > 100 or x >= y
            for (name, a, b), (found, x, y) in zip(expected, measured)
        ) or abs(duration - expected[-1][2]) > 100:
            raise ValueError("M4B 目录或总时长与合成音频不一致。")
    run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", temp, "-map", "0:a:0", "-f", "null", "-"])
    temp.replace(output)
    return duration, actual
