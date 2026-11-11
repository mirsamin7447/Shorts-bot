"""
Fully automatic eerie mystery Shorts bot (episodes, cliffhangers, viewer choices).
One run = one Short: script -> voiceover -> stock footage -> bold captions -> music mix.
The finished video, title, description and hashtags are saved in out/. The workflow then
publishes them as a GitHub Release and pings a Make.com webhook that uploads them.
Needs ffmpeg. Secrets come from environment variables (see SETUP.md).
"""
import json
import os
import random
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import requests

from voice import make_voice

# ---- Settings you can change ----
NICHE = "short fictional stories about the universe: stars, black holes, planets and the unknown"
CHANNEL = "COSMIC BITES"  # small watermark shown at the top of every video
MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"]  # tried in order
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FORMATS = [  # a random one is picked for the evening slot so videos don't feel repetitive
    "a story told from a spaceship crew's point of view that ends on a cliffhanger",
    "a lone astronaut or space probe discovers something strange in deep space, with a twist ending",
    "a cosmic story told by a star or planet as it lives out billions of years",
    "a calm narrator describes the far future of the universe and ends on an awe-inspiring twist",
    "episode 1 of a 3-part space mystery that ends with 'Part 2 next'",
]
# Each scheduled run uses a different format, so the channel mixes video types.
# Keys are UTC hours of the cron times in post.yml: 02:30 (8 am IST), 11:30 (5 pm IST), 14:00 (7:30 pm IST).
SLOT_FORMATS = {
    2: "cold-open micro-story about a star or planet: a one-line hook, three beats, then a twist",
    11: "choose-your-ending space story: tell the setup, then the last scene asks viewers to comment A or B for what happens",
    14: None,  # evening slot uses the rotating FORMATS above
}
# Hashtags: every video gets 2-3 tags about its own story, plus these general ones.
GENERAL_HASHTAGS = ["space", "universe", "sciencefiction"]
MAX_HASHTAGS = 5  # #Shorts counts toward this limit
VIDEO_W, VIDEO_H = 720, 1280  # 720x1280 keeps files under Make.com's 5 MB limit. Use 1080, 1920 on a paid Make plan.
MAX_BYTES = 4_700_000  # Make.com's free plan accepts files up to 5 MB
# ----------------------------------

WORK = Path("work")
WORK.mkdir(exist_ok=True)
HISTORY = Path("history.json")
SERIES_BIBLE = Path("series_bible.json")  # the 30-episode series; episodes are written in order


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


def clean_tag(word: str) -> str:
    """Turn a phrase into a hashtag body: letters and digits only, e.g. 'dark forest' -> 'darkforest'."""
    return re.sub(r"[^A-Za-z0-9]", "", word)


def build_hashtags(script: dict) -> list:
    """2-3 story hashtags from the script, then general ones, capped at MAX_HASHTAGS, no duplicates."""
    topic = [clean_tag(t) for t in script.get("hashtags", []) if clean_tag(t)]
    if not topic:  # fallback for scripts without a hashtags list
        topic = [clean_tag(t) for t in script.get("tags", []) if clean_tag(t)][:3]
    tags = []
    for t in topic[:3] + GENERAL_HASHTAGS:
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    return [f"#{t}" for t in tags[: MAX_HASHTAGS - 1]] + ["#Shorts"]


def series_position(history: list):
    """Returns (episode number, last episode summary, bible) while the series runs, else None."""
    if not SERIES_BIBLE.exists():
        return None
    bible = json.loads(SERIES_BIBLE.read_text(encoding="utf-8"))
    done = [h for h in history if "episode" in h]
    n = len(done) + 1
    if n > bible["episodes"]:
        return None  # series finished: fall back to normal stories
    last = done[-1].get("summary", "") if done else "This is the first episode."
    return n, last, bible


def generate_script(history: list) -> dict:
    used = [h["topic"] for h in history[-60:]]
    fmt = pick_format()
    series = series_position(history)
    if series:
        n, last_summary, bible = series
        beat = bible["beats"][n - 1]
        prompt = (
            f"You write episodes of a continuing YouTube Shorts series.\n"
            f"SERIES BIBLE: {json.dumps(bible, ensure_ascii=False)}\n\n"
            f"This is episode {n} of {bible['episodes']}.\n"
            f"Story beat for this episode: {beat}\n"
            f"Previous episode summary: {last_summary}\n\n"
            "Write the episode as a short scene with 2-4 characters in the same room who talk to each other. "
            "Keep the characters consistent with the bible. Length: 30-40 seconds, about 75-100 spoken words. "
            "Open with a one-line recap, then advance the story to the beat. "
            "End on a cliffhanger unless this is the final episode, which resolves the story. "
            "Finish with a choice for viewers, for example 'Comment A if we run, B if we ask it what it wants.' "
            "Do not say follow or subscribe. This is fiction.\n\n"
            "Return ONLY JSON with keys:\n"
            '"topic" (short episode name), "title" (under 50 chars, starting with "Ep N:"), '
            '"summary" (1-2 sentences of what happens, for the next episode), '
            '"description" (2 sentences, saying it is a fictional story and what happens), '
            '"tags" (6-8 keywords, no # symbol), '
            '"hashtags" (2-3 hashtags, no # symbol), '
            '"scenes": list of 6-12 objects {"speaker": narrator, female, male_brit, young, old or gruff, '
            '"text": words spoken, "visual": 2-3 word stock-video search that matches the scene, '
            '"visual_alt": one simple backup word}.'
        )
        raw = ask_ai(prompt)
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        data = json.loads(raw)
        assert 5 <= len(data["scenes"]) <= 14, "unexpected scene count"
        data["episode"] = n
        return data
    used = [h["topic"] for h in history[-60:]]
    raw = ask_ai(
        f"You write awe-inspiring, cinematic YouTube Shorts for a channel about {NICHE}.\n"
        f"Format for this video: {fmt}\n"
        f"Pick a fresh, original story. Do NOT reuse these topics: {used}\n\n"
        "Length: 30-40 seconds when read at a steady pace, about 75-100 spoken words in total.\n"
        "Style: a calm, wondrous narrator. Short sentences. Build wonder and tension, then one sharp twist. "
        "Speak directly to the viewer using 'you'.\n"
        "Structure:\n"
        "1. HOOK (first scene, max 9 words): a short, bold statement that makes the viewer "
        "need to know more. Never start with 'Did you know', 'Imagine' or 'Welcome'.\n"
        "2. BUILD: 4-7 scenes. Each adds one more astonishing detail, with a real number or scale where possible.\n"
        "CHARACTERS: the story can have up to 3 characters who speak in their own scenes. "
        "Use speaker 'narrator' for narration. Use 'female', 'male_brit', 'young', 'old' or 'gruff' "
        "for a character's spoken lines, and keep the same speaker for the same character.\n"
        "3. TWIST: the last story beat changes what the viewer thought was happening.\n"
        "4. ENDING: either a cliffhanger, or a question that asks viewers to comment their choice "
        "(for example 'Comment A if you open the door, B if you run'). Do not say follow or subscribe.\n"
        "Rules: this is fiction, so never present invented events as real facts. Keep the science in the story accurate. "
        "Simple words anyone can follow. Each scene is 4-9 words, written the way a narrator speaks.\n\n"
        "Return ONLY JSON with keys:\n"
        '"topic" (short story name), "title" (bold and curiosity-driven, under 55 chars), '
        '"description" (2 sentences describing the story, and saying it is a fictional story), '
        '"tags" (6-8 keywords, no # symbol), '
        '"hashtags" (2-3 hashtags about this exact story, single words or short phrases, '
        'no # symbol, e.g. "lighthousemystery", "nightstory"),\n'
        '"scenes": list of 6-12 objects {"speaker": one speaker role from the rules above, "text": words spoken in this scene, '
        '"visual": 2-3 word stock-video search that matches the mood, such as "milky way timelapse", '
        '"black hole", "nebula", "space station", "meteor shower", "galaxy", "earth from space", '
        '"visual_alt": one simple backup word such as "galaxy"}.'
    )
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    assert 5 <= len(data["scenes"]) <= 14, "unexpected scene count"
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
    """Bold thumbnail-style captions: 2-3 uppercase words at a time, upper part of the screen.
    The last caption of each scene is red; numbers are gold."""
    words = text.upper().split()
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or len(" ".join(cur + [w])) > 14):
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
        if any(ch.isdigit() for ch in chunk):
            color = "0xFFD54A"
        elif k == len(chunks) - 1:
            color = "0xE8332A"
        else:
            color = "white"
        parts.append(
            f"drawtext=fontfile={FONT}:textfile={f}:expansion=none:fontsize={VIDEO_W // 11}:"
            f"fontcolor={color}:borderw=7:bordercolor=black:shadowx=3:shadowy=3:"
            "shadowcolor=black@0.7:x=(w-text_w)/2:y=h*0.26:"
            f"enable='between(t,{t:.2f},{end:.2f})'"
        )
        t += dur
    return ",".join(parts)


def render_scene(i: int, scene: dict) -> Path:
    audio, clip, out = WORK / f"a{i}.mp3", WORK / f"c{i}.mp4", WORK / f"s{i}.mp4"

    make_voice(scene["text"], audio, scene.get("speaker", "narrator"))
    length = duration(audio) + 0.2

    queries = [scene["visual"], scene.get("visual_alt") or scene["visual"].split()[-1],
               scene["visual"].split()[-1], "abstract background"]
    if not any(get_clip(q, clip, length) for q in queries):
        raise RuntimeError(f"No footage found for scene {i}")

    start = random.uniform(0, max(0.0, duration(clip) - length))
    a, b = (0.30, 0.70) if i % 2 == 0 else (0.70, 0.30)  # slow pan, alternating
    mark = WORK / "mark.txt"
    mark.write_text(CHANNEL)
    vf = (
        f"scale=-2:{VIDEO_H + 230},scale=w='if(lt(iw,{VIDEO_W}),{VIDEO_W},iw)':h=-2:flags=lanczos,"
        f"crop={VIDEO_W}:{VIDEO_H}:x='(iw-ow)*({a}+({b}-{a})*t/{length:.2f})':y='(ih-oh)/2',"
        "eq=contrast=1.14:saturation=1.12:gamma=0.97:brightness=-0.03,unsharp=5:5:0.8:5:5:0.0,"
        "colorbalance=rs=-0.03:gs=0.00:bs=0.08:rh=0.02:bh=-0.04,"
        "vignette=angle=PI/3.5,setsar=1,"
        f"drawtext=fontfile={FONT}:textfile={mark}:expansion=none:fontsize={VIDEO_W // 30}:"
        "fontcolor=white@0.6:x=(w-text_w)/2:y=100,"
        + caption_filters(i, scene["text"], length)
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-ss", f"{start:.2f}",
         "-i", str(clip), "-i", str(audio), "-t", f"{length:.2f}", "-vf", vf,
         "-af", "apad", "-map", "0:v", "-map", "1:a", "-r", "30", "-c:v", "libx264",
         "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p",
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


# Original eerie score made from scratch: a slow dark drone plus an opening low thud.
PAD = ("0.16*sin(2*PI*44*t)+0.12*sin(2*PI*44.6*t)"
       "+0.09*sin(2*PI*66*t)*(0.7+0.3*sin(2*PI*0.09*t))"
       "+0.06*sin(2*PI*93.2*t)*(0.5+0.5*sin(2*PI*0.06*t+1))"
       "+0.04*sin(2*PI*123.5*t)*(0.4+0.6*sin(2*PI*0.04*t+2))")
BOOM = "0.9*sin(2*PI*(22+90*exp(-5*t))*t)*exp(-2.5*t)"


def finish(src: Path, dst: Path) -> None:
    """Final pass: voice polish, eerie music that ducks under the voice, 720p, small file."""
    dur = duration(src)
    audio_graph = (
        "[0:a]highpass=f=60,equalizer=f=130:t=q:w=1.2:g=2.5,equalizer=f=3200:t=q:w=1:g=1.5,"
        "acompressor=threshold=0.08:ratio=3:attack=5:release=150:makeup=2,"
        "aecho=0.85:0.5:40|85:0.22|0.10,asplit=2[vo][vsc];"
        f"[1:a]lowpass=f=450,aecho=0.8:0.88:600|1000:0.35|0.22,volume=0.18,"
        f"afade=t=in:d=2,afade=t=out:st={max(dur - 2.5, 0):.2f}:d=2.5,"
        "aformat=channel_layouts=stereo[pad];"
        "[2:a]lowpass=f=200,volume=0.7,aformat=channel_layouts=stereo[boom];"
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
             "-c:v", "libx264", "-preset", "slow", "-b:v", f"{vk}k",
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
    script_file = os.environ.get("SCRIPT_FILE", "").strip()
    if script_file:  # render a story you wrote yourself
        script = json.loads(Path(script_file).read_text(encoding="utf-8"))
        print("Using script file:", script_file)
    else:
        script = generate_script(history)
    print("Topic:", script["topic"])

    parts = [render_scene(i, s) for i, s in enumerate(script["scenes"])]
    final = concat(parts)

    out = Path("out")
    out.mkdir(exist_ok=True)
    finish(final, out / "short.mp4")
    title = f"{script['title']} #Shorts"
    hashtags = build_hashtags(script)
    ending = "This is a fictional story about the universe. Comment your choice, and follow for the next one!"
    (out / "title.txt").write_text(title)
    (out / "description.txt").write_text(
        f"{script['description']}\n\n{ending}\n\n{' '.join(hashtags)}\n"
    )
    (out / "notes.txt").write_text(
        f"TITLE (copy this):\n{title}\n\n"
        f"DESCRIPTION (copy this):\n{script['description']}\n\n{ending}\n\n{' '.join(hashtags)}\n"
    )

    entry = {"topic": script["topic"], "title": script["title"]}
    if "episode" in script:
        entry["episode"] = script["episode"]
        entry["summary"] = script.get("summary", "")
    history.append(entry)
    HISTORY.write_text(json.dumps(history, indent=2))
    print("Hashtags:", " ".join(hashtags))
    print("Saved out/short.mp4")


if __name__ == "__main__":
    main()
