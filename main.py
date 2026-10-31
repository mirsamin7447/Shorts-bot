"""
Fully automatic cinematic YouTube Shorts bot.
One run = one Short: script -> voiceover -> stock footage -> grading + captions -> music mix.
The finished video and its title/description are saved in out/. The workflow then
publishes them as a GitHub Release and pings a Make.com webhook that uploads them.
Needs ffmpeg. Secrets come from environment variables (see SETUP.md).
"""
import json
import os
import random
from datetime import datetime, timezone
import re
import subprocess
import textwrap
from pathlib import Path

import requests

from voice import make_voice

# ---- Settings you can change ----
NICHE = "mind-blowing science and space facts"
CHANNEL = "COSMIC BITES"  # small watermark shown at the top of every video
MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"]  # tried in order
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FORMATS = [  # a random one is picked for the evening slot so videos don't feel repetitive
    "a mind-blowing scale comparison (how big, fast, old, hot or far something is)",
    "myth vs truth: a common belief that is actually wrong",
    "what would actually happen if... (a strange hypothetical, answered with real science)",
    "a weird-but-true fact that sounds fake",
    "a mystery scientists still cannot fully explain",
]
# Each scheduled run uses a different format, so the channel mixes video types.
# Keys are UTC hours of the cron times in post.yml: 02:30 (8 am IST), 11:30 (5 pm IST), 14:00 (7:30 pm IST).
SLOT_FORMATS = {
    2: "quick quiz: open with a surprising science question, make the viewer guess, reveal the answer in the final scene",
    11: "top 3 countdown: three surprising space facts ranked from 3 to 1, the number 1 fact is the payoff",
    14: None,  # evening slot uses the rotating FORMATS above
}
MAX_BYTES = 4_700_000  # Make.com's free plan accepts files up to 5 MB
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


def pick_format() -> str:
    slot = SLOT_FORMATS.get(datetime.now(timezone.utc).hour)
    return slot or random.choice(FORMATS)


def generate_script(history: list) -> dict:
    used = [h["topic"] for h in history[-60:]]
    fmt = pick_format()
    raw = ask_ai(
        f"You are the writer of cinematic, trailer-style YouTube Shorts for a channel about {NICHE}.\n"
        f"Format for this video: {fmt}\n"
        f"Pick a fresh, specific topic. Do NOT reuse these topics: {used}\n\n"
        "Length: 18-24 seconds when read at a fast pace, about 45-60 spoken words in total.\n"
        "Style: a deep documentary narrator. Vivid, sensory, dramatic. Short punchy sentences "
        "and fragments. Speak directly to the viewer using 'you'.\n"
        "Structure:\n"
        "1. COLD OPEN (first scene, max 9 words): the most shocking claim in the whole video, "
        "stated as a bold fact or a question that creates an open loop the viewer must resolve. "
        "Never start with 'Did you know', 'Imagine' or 'Welcome'.\n"
        "2. TENSION: 3-5 scenes. Each reveals a new surprising detail with a real number or a "
        "comparison people can picture. Add one twist that makes them rethink.\n"
        "3. PAYOFF: the most mind-blowing fact, near the end.\n"
        "4. LOOP: the final line should flow naturally back into the opening line so viewers "
        "rewatch. Do not say follow or subscribe.\n"
        "Rules: only facts you are confident are true (never invent statistics). Simple words a "
        "12-year-old understands. Each scene is 4-9 words, written the way a narrator speaks.\n\n"
        "Return ONLY JSON with keys:\n"
        '"topic" (short), "title" (curiosity-driven, under 55 chars, truthful), '
        '"description" (2 sentences with the main keywords), '
        '"tags" (6-8 keywords, no # symbol),\n'
        '"scenes": list of 5-8 objects {"text": words spoken in this scene, '
        '"visual": 2-3 word stock-video search that literally shows it, cinematic and concrete, '
        'such as "nebula timelapse", "aerial mountains", "ocean storm", "rocket launch", '
        '"visual_alt": one simple backup word such as "galaxy"}.'
    )
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    assert 4 <= len(data["scenes"]) <= 10, "unexpected scene count"
    return data


def duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)]
    )
    return float(out)


_used_clips: set = set()


def _pick_url(hit: dict):
    for size in ("large", "medium", "small"):  # biggest size that is not 4K
        v = hit["videos"].get(size, {})
        if v.get("url") and v.get("width", 1920) <= 2100:
            return v["url"]
    return None


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
        url = _pick_url(h)
        if url:
            dest.write_bytes(requests.get(url, timeout=120).content)
            _used_clips.add(h["id"])
            return True
    return False


def caption_filters(i: int, text: str, length: float) -> str:
    """Big 2-3 word captions timed to the voice. Numbers pop in gold."""
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
        color = "0xFFD54A" if any(ch.isdigit() for ch in chunk) else "white"
        parts.append(
            f"drawtext=fontfile={FONT}:textfile={f}:expansion=none:fontsize=54:"
            f"fontcolor={color}:borderw=5:bordercolor=black:shadowx=3:shadowy=3:"
            "shadowcolor=black@0.6:x=(w-text_w)/2:y=(h-text_h)/2+170:"
            f"enable='between(t,{t:.2f},{end:.2f})'"
        )
        t += dur
    return ",".join(parts)


def render_scene(i: int, scene: dict) -> Path:
    audio, clip, out = WORK / f"a{i}.mp3", WORK / f"c{i}.mp4", WORK / f"s{i}.mp4"

    make_voice(scene["text"], audio)
    length = duration(audio) + 0.2

    queries = [scene["visual"], scene.get("visual_alt") or scene["visual"].split()[-1],
               scene["visual"].split()[-1], "abstract background"]
    if not any(get_clip(q, clip, length) for q in queries):
        raise RuntimeError(f"No footage found for scene {i}")

    start = random.uniform(0, max(0.0, duration(clip) - length))
    a, b = (0.30, 0.70) if i % 2 == 0 else (0.70, 0.30)  # slow cinematic pan, alternating
    mark = WORK / "mark.txt"
    mark.write_text(CHANNEL)
    vf = (
        "scale=-2:1510,scale=w='if(lt(iw,720),720,iw)':h=-2,"
        f"crop=720:1280:x='(iw-ow)*({a}+({b}-{a})*t/{length:.2f})':y='(ih-oh)/2',"
        "eq=contrast=1.10:saturation=1.15:gamma=0.97:brightness=-0.02,"
        "colorbalance=rs=-0.05:bs=0.07:rh=0.07:bh=-0.05,"
        "vignette=angle=PI/4.5,setsar=1,"
        f"drawtext=fontfile={FONT}:textfile={mark}:expansion=none:fontsize=24:"
        "fontcolor=white@0.6:x=(w-text_w)/2:y=100,"
        + caption_filters(i, scene["text"], length)
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-ss", f"{start:.2f}",
         "-i", str(clip), "-i", str(audio), "-t", f"{length:.2f}", "-vf", vf,
         "-af", "apad", "-map", "0:v", "-map", "1:a", "-r", "30", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)],
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


# Original cinematic score made from scratch: a slow dark space pad plus an opening boom.
PAD = ("0.16*sin(2*PI*55*t)+0.12*sin(2*PI*55.35*t)"
       "+0.10*sin(2*PI*82.41*t)*(0.75+0.25*sin(2*PI*0.11*t))"
       "+0.07*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*0.07*t))"
       "+0.05*sin(2*PI*130.81*t)*(0.5+0.5*sin(2*PI*0.05*t+1))"
       "+0.04*sin(2*PI*164.81*t)*(0.5+0.5*sin(2*PI*0.09*t+2))")
BOOM = "0.9*sin(2*PI*(28+110*exp(-5*t))*t)*exp(-2.5*t)"


def finish(src: Path, dst: Path) -> None:
    """Final pass: voice polish, cinematic music that ducks under the voice, 720p, small file."""
    dur = duration(src)
    audio_graph = (
        "[0:a]highpass=f=60,equalizer=f=130:t=q:w=1.2:g=2.5,equalizer=f=3200:t=q:w=1:g=1.5,"
        "acompressor=threshold=0.08:ratio=3:attack=5:release=150:makeup=2,"
        "aecho=0.85:0.5:40|85:0.22|0.10,asplit=2[vo][vsc];"
        f"[1:a]lowpass=f=500,aecho=0.8:0.88:600|1000:0.35|0.22,volume=0.18,"
        f"afade=t=in:d=2,afade=t=out:st={max(dur - 2.5, 0):.2f}:d=2.5,"
        "aformat=channel_layouts=stereo[pad];"
        "[2:a]lowpass=f=220,volume=0.7,aformat=channel_layouts=stereo[boom];"
        "[pad][vsc]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=450[duck];"
        "[vo]aformat=channel_layouts=stereo[vos];"
        "[vos][duck][boom]amix=inputs=3:duration=first:normalize=0,"
        "loudnorm=I=-14:TP=-1.5:LRA=9[aout];"
        "[0:v]null[vout]"
    )
    total_kbps = int(MAX_BYTES * 8 / dur / 1000)
    for share in (1.0, 0.85, 0.7, 0.55):
        vk = max(int(total_kbps * share) - 80, 150)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-f", "lavfi", "-i", f"aevalsrc={PAD}:s=44100:d={dur:.2f}",
             "-f", "lavfi", "-i", f"aevalsrc={BOOM}:s=44100:d=2",
             "-filter_complex", audio_graph, "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "medium", "-b:v", f"{vk}k",
             "-maxrate", f"{int(vk * 1.25)}k", "-bufsize", f"{vk * 2}k",
             "-pix_fmt", "yuv420p", "-profile:v", "high", "-r", "30",
             "-c:a", "aac", "-b:a", "80k", "-ar", "44100",
             "-movflags", "+faststart", "-use_editlist", "0", "-t", f"{dur:.2f}", str(dst)],
            check=True,
        )
        if dst.stat().st_size < MAX_BYTES:
            return


def main() -> None:
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    script = generate_script(history)
    print("Topic:", script["topic"])

    parts = [render_scene(i, s) for i, s in enumerate(script["scenes"])]
    final = concat(parts)

    out = Path("out")
    out.mkdir(exist_ok=True)
    finish(final, out / "short.mp4")
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
