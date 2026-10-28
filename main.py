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
MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"]  # tried in order
VOICE = "en-US-GuyNeural"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FORMATS = [  # a random one is picked each run so videos don't feel repetitive
    "a mind-blowing scale comparison (how big, fast, old, hot or far something is)",
    "myth vs truth: a common belief that is actually wrong",
    "what would actually happen if... (a strange hypothetical, answered with real science)",
    "a weird-but-true fact that sounds fake",
    "a mystery scientists still cannot fully explain",
]
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
    fmt = random.choice(FORMATS)
    raw = ask_ai(
        f"You write viral YouTube Shorts for a channel about {NICHE}.\n"
        f"Format for this video: {fmt}\n"
        f"Pick a fresh, specific topic. Do NOT reuse these topics: {used}\n\n"
        "Length: 30-40 seconds, about 85-100 spoken words in total.\n"
        "Structure:\n"
        "1. HOOK (first scene, max 12 words): a surprising claim or question that makes "
        "people need the answer. Never start with 'Did you know' or 'Imagine'.\n"
        "2. BUILD: 4-7 punchy scenes. Each adds a new surprising detail, using real "
        "numbers and comparisons people can picture.\n"
        "3. PAYOFF: the most mind-blowing fact, near the end.\n"
        "4. LOOP: the last line should flow naturally back into the hook so viewers "
        "rewatch. Do not say follow or subscribe.\n"
        "Rules: only facts you are confident are true (never invent statistics). "
        "Simple words a 12-year-old understands. Each scene is 6-12 words, written "
        "the way people really talk.\n\n"
        "Return ONLY JSON with keys:\n"
        '"topic" (short), "title" (curiosity-driven, under 55 chars, truthful), '
        '"description" (2 sentences with the main keywords), '
        '"tags" (6-8 keywords, no # symbol),\n'
        '"scenes": list of 8-12 objects {"text": words spoken in this scene, '
        '"visual": 2-3 word stock-video search that literally shows it, using concrete '
        'things such as "galaxy", "rocket launch", "ocean waves"; avoid abstract words}.'
    )
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    assert 5 <= len(data["scenes"]) <= 14, "unexpected scene count"
    return data


async def _tts(text: str, path: Path) -> None:
    await edge_tts.Communicate(text, VOICE, rate="+8%").save(str(path))


def duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)]
    )
    return float(out)


_used_clips: set = set()


def get_clip(query: str, dest: Path, min_dur: float = 0.0) -> bool:
    r = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": os.environ["PIXABAY_API_KEY"], "q": query,
                "per_page": 30, "safesearch": "true"},
        timeout=30,
    )
    r.raise_for_status()
    hits = r.json().get("hits", [])
    random.shuffle(hits)
    hits.sort(key=lambda h: h.get("duration", 0) < min_dur)  # long-enough clips first
    for h in hits:
        if h["id"] in _used_clips:
            continue
        for size in ("medium", "large", "small"):
            url = h["videos"].get(size, {}).get("url")
            if url:
                dest.write_bytes(requests.get(url, timeout=120).content)
                _used_clips.add(h["id"])
                return True
    return False


def caption_filters(i: int, text: str, length: float) -> str:
    """Big 2-3 word captions that pop up in time with the voice."""
    words = text.upper().split()
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or len(" ".join(cur + [w])) > 16):
            chunks.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        chunks.append(" ".join(cur))

    speech = max(length - 0.25, 0.5)
    total = sum(len(c) for c in chunks) or 1
    t, parts = 0.0, []
    for k, chunk in enumerate(chunks):
        dur = speech * len(chunk) / total
        f = WORK / f"cap{i}_{k}.txt"
        f.write_text(chunk)
        end = length if k == len(chunks) - 1 else t + dur
        parts.append(
            f"drawtext=fontfile={FONT}:textfile={f}:expansion=none:fontsize=78:"
            "fontcolor=0xFFE14D:borderw=9:bordercolor=black:"
            "x=(w-text_w)/2:y=(h-text_h)/2+260:"
            f"enable='between(t,{t:.2f},{end:.2f})'"
        )
        t += dur
    return ",".join(parts)


def render_scene(i: int, scene: dict) -> Path:
    audio, clip, out = WORK / f"a{i}.mp3", WORK / f"c{i}.mp4", WORK / f"s{i}.mp4"

    make_voice(scene["text"], audio)
    length = duration(audio) + 0.25

    visual = scene["visual"]
    queries = [visual, visual.split()[-1], "abstract background"]
    if not any(get_clip(q, clip, length) for q in queries):
        raise RuntimeError(f"No footage found for scene {i}")

    start = random.uniform(0, max(0.0, duration(clip) - length))
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,"
        "eq=contrast=1.08:saturation=1.2:brightness=-0.05,"
        + caption_filters(i, scene["text"], length)
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-ss", f"{start:.2f}",
         "-i", str(clip), "-i", str(audio), "-t", f"{length:.2f}", "-vf", vf,
         "-af", "apad", "-map", "0:v", "-map", "1:a", "-r", "30", "-c:v", "libx264",
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
    for kbps in (900, 700, 520, 380, 280):
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
        f"{script['description']}\n\nFollow for a new mind-blowing fact every day!\n\n"
        f"{hashtags} #Shorts\n"
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