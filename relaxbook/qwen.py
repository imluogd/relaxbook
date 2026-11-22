import asyncio
import os
import re
import sys
import uuid

import aiohttp

MODEL = "qwen-audio-3.1-tts-plus"
INSTRUCTION = (
    "用平实、克制的有声书旁白语气朗读，语速适中。读完标题稍停，"
    "逗号处短停，分号处略停，句号处停顿更明显。按语意连贯朗读长句，避免过度表演。"
)
SAMPLE_RATE = 24000
ENDPOINT = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
VOICES = [
    {"id": key, "label": f"Qwen · {name} · 有声书朗读指令（联网）", "language": "zh",
     "engine": "qwen", "speaker": speaker}
    for key, name, speaker in [
        (300, "龙安灵心", "longanlingxin_v3.1"),
        (301, "许南川", "xunanchuan_v3.1"),
        (302, "龙安欢", "longanhuan_v3.1"),
        (303, "龙安风悦", "longanfengyue_v3.1"),
    ]
]


def api_key():
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if key or sys.platform != "win32":
        return key

    import winreg

    # A launcher that was already open may have an older environment snapshot.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as registry:
            return winreg.QueryValueEx(registry, "DASHSCOPE_API_KEY")[0].strip()
    except FileNotFoundError:
        return ""


async def synthesize(key, text, speaker, speed):
    task_id = uuid.uuid4().hex

    def command(action, payload):
        return {"header": {"action": action, "task_id": task_id, "streaming": "duplex"},
                "payload": payload}

    start = command("run-task", {
        "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer", "model": MODEL,
        "parameters": {"text_type": "PlainText", "voice": speaker, "format": "mp3",
                       "sample_rate": SAMPLE_RATE, "rate": speed, "instruction": INSTRUCTION},
        "input": {},
    })
    timeout = aiohttp.ClientTimeout(total=None, connect=12)
    chunks = []
    sent_text = False
    try:
        async with asyncio.timeout(120), aiohttp.ClientSession(timeout=timeout) as client:
            async with client.ws_connect(ENDPOINT, headers={"Authorization": f"Bearer {key}"}) as ws:
                await ws.send_json(start)
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        chunks.append(message.data)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    header = message.json().get("header", {})
                    event = header.get("event")
                    if event == "task-started" and not sent_text:
                        await ws.send_json(command("continue-task", {"input": {"text": text}}))
                        await ws.send_json(command("finish-task", {"input": {}}))
                        sent_text = True
                    elif event == "task-failed":
                        code = header.get("error_code")
                        detail = code if isinstance(code, str) and re.fullmatch(r"[\w.-]{1,80}", code) else "未知错误"
                        raise RuntimeError(f"百炼语音合成失败（{detail}）。请检查账号权限、额度或正文后重试。")
                    elif event == "task-finished":
                        if chunks:
                            return b"".join(chunks)
                        break
    except aiohttp.WSServerHandshakeError as error:
        raise RuntimeError(
            f"无法连接阿里云百炼（HTTP {error.status}）。请检查北京地域的 API Key 和模型权限。"
        ) from None
    except (aiohttp.ClientError, TimeoutError):
        raise RuntimeError("连接阿里云百炼异常或超时。请检查网络后重试。") from None
    raise RuntimeError("阿里云百炼未返回完整音频。请检查正文或重试。")
