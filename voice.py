"""Text-to-speech with fallbacks: Microsoft Edge voices -> Google TTS -> offline espeak."""
import asyncio
import subprocess
from pathlib import Path

import edge_tts

VOICES = ["en-US-GuyNeural", "en-US-AriaNeural"]
_edge_failed = False


async def _edge(text: str, path: Path, voice: str) -> None:
    await edge_tts.Communicate(text, voice, rate="+8%").save(str(path))


def _ok(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 1000


def make_voice(text: str, path: Path) -> None:
    global _edge_failed
    errors = []
    if not _edge_failed:
        for voice in VOICES:
            try:
                asyncio.run(_edge(text, path, voice))
                if _ok(path):
                    return
            except Exception as e:
                errors.append(f"edge/{voice}: {type(e).__name__}")
        _edge_failed = True
        print("Edge TTS failed, switching to backup voices:", errors)
    try:
        from gtts import gTTS

        gTTS(text, lang="en").save(str(path))
        if _ok(path):
            return
    except Exception as e:
        print("Google TTS failed:", type(e).__name__, e)
    wav = path.with_suffix(".wav")
    subprocess.run(["espeak-ng", "-s", "160", "-w", str(wav), text], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(path)], check=True
    )
