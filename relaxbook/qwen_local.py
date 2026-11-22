import http.client
import io
import json
import wave


MODEL = "Qwen3-TTS-12Hz-1.7B-CustomVoice"
VOICE = {"id": 310, "label": "Qwen3-TTS · Serena（离线）",
         "language": "zh", "engine": "qwen_local", "speaker": "serena"}
INSTRUCTION = "用普通话以正常语速朗读。语气平实自然，长句连贯，按标点自然停顿。"
SAMPLE_RATE = 24000


def request(port, path, payload=None, timeout=5):
    if port is None:
        raise ValueError("请在本机配置中设置 qwen_local_port 后重试。")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        if payload is None:
            connection.request("GET", path)
        else:
            body = json.dumps(payload, ensure_ascii=False).encode()
            connection.request("POST", path, body, {"Content-Type": "application/json"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f"本机 Qwen3-TTS 服务返回 HTTP {response.status}，请检查模型和服务日志。")
            return response.read(), response.getheader("X-Speech-Stop")
    except (OSError, http.client.HTTPException) as error:
        raise RuntimeError(
            f"本机 Qwen3-TTS 服务无法连接或响应超时。请检查 127.0.0.1:{port} 后重试。"
        ) from error
    finally:
        connection.close()


def model_details(port):
    body, _ = request(port, "/v1/models")
    try:
        model = next((item for item in json.loads(body)["data"] if item["id"] == MODEL), None)
        if model is not None:
            speech = model["speech"]
            voices = {voice["name"] for voice in speech["voices"]}
            if VOICE["speaker"] in voices and speech["sample_rate"] == SAMPLE_RATE:
                return model["version"], speech["weight_type"], speech["source"]["revision"]
    except (ValueError, KeyError, TypeError):
        pass
    raise RuntimeError(f"本机端口 {port} 请加载 {MODEL} 模型，并使用兼容的 speech.cpp 版本。")


def synthesize(port, text, speaker):
    payload = {
        "model": MODEL, "input": text, "voice": speaker, "language": "zh",
        "instructions": INSTRUCTION, "response_format": "wav",
        "temperature": 0.9, "top_k": 50, "repetition_penalty": 1.05,
        "max_seconds": max(30, len(text)),
    }
    body, stop = request(port, "/v1/audio/speech", payload, timeout=600)
    if stop != "complete":
        raise RuntimeError("本机 Qwen3-TTS 提前结束。请重试，或在章节正文中把长句分成短段。")
    try:
        with wave.open(io.BytesIO(body), "rb") as clip:
            frames = clip.getnframes()
            if ((clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) == (1, 2, SAMPLE_RATE)
                and frames > 0 and len(clip.readframes(frames)) == frames * 2):
                return body
    except (wave.Error, EOFError):
        pass
    raise RuntimeError("本机 Qwen3-TTS 返回的 WAV 音频为空或不完整。")
