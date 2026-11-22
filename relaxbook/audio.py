import asyncio
import hashlib
import json
import logging
import re
import subprocess
import wave
from functools import cached_property
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path

from . import qwen, qwen_local

LOG = logging.getLogger(__name__)
MODEL_NAME = "kokoro-int8-multi-lang-v1_1"
# Model locations used by jobs created before the Qwen-only voice catalog.
VOICE_PACKS = {
    "melo": {
        "directory": "vits-melo-tts-zh_en",
        "sha256": "e58351ed7149f290a54534538badd4077cdbe6fddc964b24d0bee870415d1514",
    },
    "vits": {
        "directory": "sherpa-onnx-vits-zh-ll",
        "sha256": "f7393d1bbc59709d5f52ea76cd5bceeec62f6a29f9c1f79ff64b4c15ec841f3a",
    },
}


def pack_directory(config, pack):
    return Path(config["model_dir"]).parent / pack["directory"]


def voice_options(config):
    options = qwen.VOICES.copy() if qwen.api_key() else []
    if config.get("qwen_local_port") is not None:
        options.extend(qwen_local.VOICES)
    return options


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


def speech_chunks(text, voice):
    engine = voice.get("engine")
    if engine == "qwen_local":
        for paragraph in text.splitlines():
            sentences = re.findall(r"[^。！？!?]*[。！？!?]+[”’」』）)]*|[^。！？!?]+$", paragraph)
            for start in range(0, len(sentences), 3):
                yield from chunks("".join(sentences[start:start + 3]), limit=120)
        return
    if engine not in {"edge", "qwen"}:
        yield from chunks(text)
        return
    # Keep paragraph breaks and punctuation in the same request where possible.
    remaining = text.strip()
    limit = 650
    while len(remaining) > limit:
        window = remaining[limit // 2:limit]
        for stops in (r"[。！？.!?\n]", r"[；;]", r"[，、,\s]"):
            boundaries = list(re.finditer(stops, window))
            if boundaries:
                break
        end = limit // 2 + boundaries[-1].end() if boundaries else limit
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
            raise ValueError("本机缺少所选音色的模型，请运行 setup 或 setup-voices。")
        try:
            runtime = version("sherpa-onnx")
        except PackageNotFoundError:
            raise ValueError("继续这个历史任务需安装原来的 sherpa-onnx 依赖。") from None
        digest = hashlib.sha256(f"{self.cache_version}:sherpa-onnx-{runtime}".encode())
        for path in sorted(self.directory.rglob("*")):
            if path.is_file() and path.name not in {"README.md", "LICENSE"}:
                digest.update(path.relative_to(self.directory).as_posix().encode())
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024**2), b""):
                        digest.update(block)
        return digest.hexdigest()

    cache_version = "segments-v1"

    @cached_property
    def engine(self):
        import sherpa_onnx

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
                    and stream.getframerate() > 0 and stream.getnframes() > 0
                    and path.stat().st_size >= stream.getnframes() * 2 + 44):
                    LOG.info("Reused segment %s", key)
                    return path
        except (OSError, EOFError, wave.Error):
            pass
        tmp = path.with_suffix(".partial.wav")
        self.synthesize(text, voice, speed, tmp)
        info = wave_info(tmp)
        if info.nframes <= 0 or (info.nchannels, info.sampwidth) != (1, 2):
            raise RuntimeError("语音引擎返回空片段或不支持的采样格式。")
        tmp.replace(path)
        LOG.info("Synthesized segment %s", key)
        return path

    def synthesize(self, text, voice, speed, output):
        import sherpa_onnx

        audio = self.engine.generate(text, sid=voice.get("speaker", voice["id"]), speed=speed)
        if not sherpa_onnx.write_wave(str(output), self.prepare_samples(audio.samples), audio.sample_rate):
            raise RuntimeError("无法写入语音片段。")

    def prepare_samples(self, samples):
        return samples


class VitsNarrator(Narrator):
    cache_version = "vits-fp32-level-v1"

    @cached_property
    def model(self):
        return self.directory / "model.onnx"

    @cached_property
    def engine(self):
        import sherpa_onnx

        d = self.directory
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(self.model), tokens=str(d / "tokens.txt"), lexicon=str(d / "lexicon.txt"),
                ),
                num_threads=self.threads, provider="cpu",
            ),
            rule_fsts=",".join(str(d / name) for name in ("phone.fst", "date.fst", "number.fst")
                               if (d / name).is_file()),
            max_num_sentences=1,
        )
        if not config.validate():
            raise ValueError("离线语音包不完整，请重新安装模型。")
        return sherpa_onnx.OfflineTts(config)

    def prepare_samples(self, samples):
        peak = max(map(abs, samples), default=0)
        if peak < 1e-5:
            raise ValueError("语音引擎返回了静音。请检查原文或试听其他音色。")
        rms = (sum(value * value for value in samples) / len(samples)) ** 0.5
        # Use one gain per segment, without a compressor or a change in timing.
        gain = min(10 ** (8 / 20), 10 ** (-2 / 20) / peak, 10 ** (-20 / 20) / rms)
        return [sample * gain for sample in samples]


class EdgeNarrator(Narrator):
    def __init__(self, config):
        self.enabled = config.get("microsoft_online", False)
        self.ffmpeg = config["ffmpeg"]

    @cached_property
    def fingerprint(self):
        if not self.enabled:
            raise ValueError("微软在线语音已禁用，请在本机配置中启用后继续。")
        if not find_spec("edge_tts"):
            raise ValueError("请先安装 requirements-edge.txt 中的微软在线语音依赖。")
        return f"edge-read-aloud-v1:{version('edge-tts')}"

    def synthesize(self, text, voice, speed, output):
        import edge_tts

        rate = f"{round((speed - 1) * 100):+d}%"
        source = output.with_suffix(".mp3")

        async def save():
            stream = edge_tts.Communicate(text, voice["speaker"], rate=rate,
                                          connect_timeout=10, receive_timeout=30)
            await asyncio.wait_for(stream.save(str(source)), timeout=65)

        try:
            try:
                asyncio.run(save())
            except Exception as error:
                raise RuntimeError(
                    "微软在线语音暂时无法生成音频。请检查网络后重试，或另选本地音色。"
                    f"（{type(error).__name__}）"
                ) from error
            decode_online_audio(self.ffmpeg, source, output, 24000)
        finally:
            source.unlink(missing_ok=True)


class QwenNarrator(Narrator):
    def __init__(self, config):
        self.ffmpeg = config["ffmpeg"]

    @cached_property
    def fingerprint(self):
        instruction = hashlib.sha256(qwen.INSTRUCTION.encode()).hexdigest()
        # Keep the original fingerprint so interrupted 龙安灵心 jobs can resume.
        return f"qwen-websocket-v1:{qwen.MODEL}:{qwen.VOICES[0]['speaker']}:{instruction}"

    def synthesize(self, text, voice, speed, output):
        key = qwen.api_key()
        if not key:
            raise ValueError("请设置北京地域的 DASHSCOPE_API_KEY 后重试。")
        audio = asyncio.run(qwen.synthesize(key, text, voice["speaker"], speed))
        source = output.with_suffix(".mp3")
        try:
            source.write_bytes(audio)
            decode_online_audio(self.ffmpeg, source, output, qwen.SAMPLE_RATE)
        finally:
            source.unlink(missing_ok=True)


class LocalQwenNarrator(Narrator):
    def __init__(self, config):
        self.port = config.get("qwen_local_port")
        self.ffmpeg = config["ffmpeg"]

    @cached_property
    def fingerprint(self):
        engine, quantization, revision = qwen_local.model_details(self.port)
        instruction = hashlib.sha256(qwen_local.INSTRUCTION.encode()).hexdigest()
        return f"qwen-local-v1:{qwen_local.MODEL}:{engine}:{quantization}:{revision}:{instruction}"

    def synthesize(self, text, voice, speed, output):
        audio = qwen_local.synthesize(self.port, text, voice["speaker"])
        if speed == 1:
            output.write_bytes(audio)
            return
        source = output.with_suffix(".native.wav")
        try:
            source.write_bytes(audio)
            run([self.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-y", "-i", source,
                 "-af", f"atempo={speed}", "-c:a", "pcm_s16le", output], timeout=30)
        finally:
            source.unlink(missing_ok=True)


def decode_online_audio(ffmpeg, source, output, sample_rate):
    run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-y", "-i", source,
         "-map", "0:a:0", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", output],
        timeout=30)


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
    sample_rate = wave_info(inputs[0]).framerate
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
            f"[CHAPTER]\nTIMEBASE=1/{sample_rate}\nSTART={start}\nEND={end}\ntitle={escape_metadata(name)}\n"
            for name, start, end in chapters
        ), encoding="utf-8")
        prefix += ["-f", "ffmetadata", "-i", meta]
        extra += ["-map_chapters", "1", "-c:a", "aac", "-b:a", "112k", "-movflags", "+faststart"]
        if cover and cover.is_file():
            prefix += ["-i", cover]
            extra += ["-map", "2:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
    else:
        extra = ["-c:a", "libmp3lame", "-q:a", "2", "-metadata", f"track={track}"]
    temp = output.with_name(f".partial{output.suffix}")
    run(prefix + ["-map", "0:a:0"] + extra + [
        "-metadata", f"album={album}", "-metadata", f"artist={artist}",
        "-metadata", f"title={title}", temp,
    ])
    duration, actual = probe(ffprobe, temp)
    if chapters is not None:
        expected = [(name, round(a * 1000 / sample_rate), round(b * 1000 / sample_rate))
                    for name, a, b in chapters]
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
