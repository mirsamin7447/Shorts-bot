"""
Narration with several voices per video (narrator plus characters).
Order: Kokoro (free, offline, no quota) -> Gemini TTS -> Microsoft Edge -> Google TTS -> espeak.
Each scene names a speaker role: narrator, female, male_brit, young, old or gruff.
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

# ---- Voice settings ----
# Kokoro voices per role (American = a..., British = b...). Change any name to another Kokoro voice.
KOKORO_VOICES = {
    "narrator": "am_adam",
    "female": "af_sarah",
    "male_brit": "bm_lewis",
    "young": "af_bella",
    "old": "bm_george",
    "gruff": "am_michael",
}
GEMINI_VOICES = {  # Gemini TTS prebuilt voices, used only if Kokoro fails
    "narrator": "Charon", "female": "Kore", "male_brit": "Puck",
    "young": "Aoede", "old": "Orus", "gruff": "Fenrir",
}
EDGE_VOICES = {  # Microsoft Edge voices, used only if Gemini fails
    "narrator": "en-US-GuyNeural", "female": "en-US-AriaNeural", "male_brit": "en-GB-RyanNeural",
    "young": "en-US-JennyNeural", "old": "en-GB-RyanNeural", "gruff": "en-US-GuyNeural",
}
GEMINI_TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
STYLE = ("Say in an expressive, cinematic voice with clear emotion, dramatic pauses and "
         "a steady, engaging pace")
SPEED = 1.10  # extra speed-up applied to every voice (1.0 = normal)
# ------------------------

_kokoro_pipes = {}
_kokoro_failed = False
_gemini_failed = False
_edge_failed = False


def _ok(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 1000


def _kokoro(text: str, path: Path, voice_id: str) -> bool:
    """Kokoro runs locally. Its model is downloaded once per run from Hugging Face."""
    global _kokoro_failed
    if _kokoro_failed:
        return False
    try:
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline

        lang = "b" if voice_id.startswith("b") else "a"  # b = British, a = American
        if lang not in _kokoro_pipes:
            _kokoro_pipes[lang] = KPipeline(lang_code=lang)
        chunks = [audio for _, _, audio in _kokoro_pipes[lang](text, voice=voice_id, speed=1.0)]
        if not chunks:
            raise RuntimeError("no audio returned")
        wav = path.with_suffix(".wav")
        sf.write(str(wav), np.concatenate(chunks), 24000)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ar", "44100", str(path)],
            check=True,
        )
        return _ok(path)
    except Exception as e:
        print("Kokoro failed:", type(e).__name__, str(e)[:200])
        _kokoro_failed = True
        return False


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


def _gemini(text: str, path: Path, role: str) -> bool:
    global _gemini_failed
    key = os.environ.get("GEMINI_API_KEY")
    if not key or _gemini_failed:
        return False
    body = {
        "contents": [{"parts": [{"text": f"{STYLE}: {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                "voiceName": GEMINI_VOICES.get(role, GEMINI_VOICES["narrator"])}}},
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


def _make_voice_raw(text: str, path: Path, role: str) -> None:
    global _edge_failed
    if _kokoro(text, path, KOKORO_VOICES.get(role, KOKORO_VOICES["narrator"])):
        return
    if _gemini(text, path, role):
        return
    if not _edge_failed:
        errors = []
        try:
            asyncio.run(_edge(text, path, EDGE_VOICES.get(role, EDGE_VOICES["narrator"])))
            if _ok(path):
                return
        except Exception as e:
            errors.append(type(e).__name__)
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


def make_voice(text: str, path: Path, role: str = "narrator") -> None:
    """Create the narration for one scene with the chosen voice role, then speed it up slightly."""
    _make_voice_raw(text, path, role)
    fast = path.with_name(path.stem + "_fast.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-af", f"atempo={SPEED}", "-ar", "44100", str(fast)],
        check=True,
    )
    fast.replace(path)
