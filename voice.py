"""
Narration voice with automatic fallbacks:
Gemini TTS (natural, directed style, free) -> Microsoft Edge -> Google TTS -> offline espeak.
"""
import asyncio
import base64
import os
import re
import subprocess
import time
from pathlib import Path

import edge_tts
import requests

GEMINI_TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
GEMINI_VOICE = "Charon"  # deep, informative. Other options: Orus, Gacrux, Algieba, Fenrir
STYLE = ("Say in a deep, calm, awe-struck documentary narrator voice, like a space film "
         "trailer. Measured and dramatic, with short natural pauses")
EDGE_VOICES = ["en-US-GuyNeural", "en-US-AriaNeural"]

_gemini_failed = False
_edge_failed = False


def _ok(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 1000


def _pcm_to_mp3(pcm: bytes, rate: int, path: Path) -> None:
    """Raw 16-bit mono PCM -> mp3, trimming silence at both ends."""
    trim = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,"
            "areverse,"
            "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,"
            "areverse")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
         "-i", "-", "-af", trim, "-ar", "44100", str(path)],
        input=pcm, check=True,
    )


def _gemini(text: str, path: Path) -> bool:
    global _gemini_failed
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return False
    body = {
        "contents": [{"parts": [{"text": f"{STYLE}: {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}},
        },
    }
    for model in GEMINI_TTS_MODELS:
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body, timeout=120,
                )
            except requests.RequestException as e:
                print(f"Gemini TTS {model}: network error {type(e).__name__}")
                time.sleep(5)
                continue
            if r.ok:
                try:
                    part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                    pcm = base64.b64decode(part["data"])
                    m = re.search(r"rate=(\d+)", part.get("mimeType", ""))
                    _pcm_to_mp3(pcm, int(m.group(1)) if m else 24000, path)
                    if _ok(path):
                        time.sleep(1.5)  # stay under free-tier rate limits
                        return True
                except Exception as e:
                    print(f"Gemini TTS {model}: bad audio ({type(e).__name__})")
                break  # next model
            print(f"Gemini TTS {model}: HTTP {r.status_code}")
            if r.status_code in (429, 500, 503):
                time.sleep(15 * (attempt + 1))
                continue
            break  # 400/403/404: try next model
    _gemini_failed = True
    return False


async def _edge(text: str, path: Path, voice: str) -> None:
    await edge_tts.Communicate(text, voice, rate="+2%").save(str(path))


def make_voice(text: str, path: Path) -> None:
    global _edge_failed
    if not _gemini_failed and _gemini(text, path):
        return
    if not _edge_failed:
        errors = []
        for voice in EDGE_VOICES:
            try:
                asyncio.run(_edge(text, path, voice))
                if _ok(path):
                    return
            except Exception as e:
                errors.append(f"edge/{voice}: {type(e).__name__}")
        _edge_failed = True
        print("Edge TTS failed:", errors)
    try:
        from gtts import gTTS

        gTTS(text, lang="en").save(str(path))
        if _ok(path):
            return
    except Exception as e:
        print("Google TTS failed:", type(e).__name__, e)
    wav = path.with_suffix(".wav")
    subprocess.run(["espeak-ng", "-s", "150", "-w", str(wav), text], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(path)], check=True
    )
