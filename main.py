"""
Fully automatic YouTube Shorts bot.
One run = one Short: write script -> voiceover -> stock footage -> captions.
The finished video and its title/description are saved in out/. The workflow then
publishes them as a GitHub Release and (optionally) pings a Make.com webhook that
uploads the video to YouTube.
Needs ffmpeg installed. Secrets come from environment variables (see SETUP.md).
"""
import asyncio
import json
import os
import random
import re
import subprocess
import textwrap
from pathlib import Path

import edge_tts
import requests

from voice import make_voice
# ---- Settings you can change ----
NICHE = "mind-blowing science and space facts"
MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.6-flash"]  # tried in order
VOICE = "en-US-GuyNeural"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# ----------------------------------

WORK = Path("work")
WORK.mkdir(exist_ok=True)
HISTORY = Path("history.json")


def ask_ai(prompt: str) -> str:
    """Free Gemini API. Tries each model in MODELS until one works."""
    last = ""
    for model in MODELS:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"],
                     "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=120,
        )
        if r.ok:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        last = f"{model}: {r.status_code} {r.text[:200]}"
    raise RuntimeError(f"All Gemini models failed. Last error: {last}")


def generate_script(history: list) -> dict:
    used = [h["topic"] for h in history[-60:]]
    raw = ask_ai(
        f"Write a YouTube Short (40-50 seconds, about 110 spoken words) for a channel about {NICHE}.\n"
        f"Pick a fresh, specific topic. Do NOT reuse these topics: {used}\n\n"
        "Rules: the first sentence is a curiosity hook. Only state facts you are "
        "confident are true; leave out anything uncertain. Short punchy sentences, "
        "no filler. End with a line that makes people want to rewatch.\n\n"
        "Return ONLY JSON with keys:\n"
        '"topic" (short), "title" (under 60 chars), "description" (2 sentences),\n'
        '"tags" (5-8 words, no # symbol),\n'
        '"scenes": list of 5-8 objects {"text": sentence(s) spoken in this scene, '
        '"visual": 2-4 word stock-video search for it}.'
    )
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    assert 3 <= len(data["scenes"]) <= 12, "unexpected scene count"
    return data


async def _tts(text: str, path: Path) -> None:
    await edge_tts.Communicate(text, VOICE, rate="+8%").save(str(path))


def duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)]
    )
    return float(out)


def get_clip(query: str, dest: Path) -> bool:
    r = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": os.environ["PIXABAY_API_KEY"], "q": query,
                "per_page": 20, "safesearch": "true"},
        timeout=30,
    )
    r.raise_for_status()
    hits = r.json().get("hits", [])
    random.shuffle(hits)
    for h in hits:
        for size in ("large", "medium"):
            url = h["videos"].get(size, {}).get("url")
            if url:
                dest.write_bytes(requests.get(url, timeout=120).content)
                return True
    return False


def render_scene(i: int, scene: dict) -> Path:
    audio, clip = WORK / f"a{i}.mp3", WORK / f"c{i}.mp4"
    txt, out = WORK / f"t{i}.txt", WORK / f"s{i}.mp4"

    make_voice(scene["text"], audio)
    length = duration(audio) + 0.25

    if not (get_clip(scene["visual"], clip) or get_clip("abstract background", clip)):
        raise RuntimeError(f"No footage found for scene {i}")

    txt.write_text("\n".join(textwrap.wrap(scene["text"], 24)))
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,"
        f"drawtext=fontfile={FONT}:textfile={txt}:expansion=none:fontsize=64:"
        "fontcolor=white:borderw=6:bordercolor=black:line_spacing=12:"
        "x=(w-text_w)/2:y=h*0.62"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(clip),
         "-i", str(audio), "-t", f"{length:.2f}", "-vf", vf, "-af", "apad",
         "-map", "0:v", "-map", "1:a", "-r", "30", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)],
        check=True,
    )
    return out


def concat(parts: list) -> Path:
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    final = WORK / "final.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(final)],
        check=True,
    )
    return final


def compress(src: Path, dst: Path) -> None:
    """Shrink under ~4.8 MB so Make.com's free plan (5 MB file limit) accepts it."""
    for kbps in (550, 400, 280):
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-vf", "scale=720:1280", "-c:v", "libx264", "-b:v", f"{kbps}k",
             "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k",
             "-movflags", "+faststart", str(dst)],
            check=True,
        )
        if dst.stat().st_size < 4_800_000:
            return


def main() -> None:
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    script = generate_script(history)
    print("Topic:", script["topic"])

    parts = [render_scene(i, s) for i, s in enumerate(script["scenes"])]
    final = concat(parts)

    out = Path("out")
    out.mkdir(exist_ok=True)
    compress(final, out / "short.mp4")
    title = f"{script['title']} #Shorts"
    hashtags = " ".join("#" + t.replace(" ", "") for t in script["tags"][:5])
    (out / "title.txt").write_text(title)
    (out / "description.txt").write_text(
        f"{script['description']}\n\n{hashtags} #Shorts\n"
    )
    (out / "notes.txt").write_text(
        f"TITLE (copy this):\n{title}\n\n"
        f"DESCRIPTION (copy this):\n{script['description']}\n\n{hashtags} #Shorts\n"
    )

    history.append({"topic": script["topic"], "title": script["title"]})
    HISTORY.write_text(json.dumps(history, indent=2))
    print("Saved out/short.mp4")


if __name__ == "__main__":
    main()
