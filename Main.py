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
        f"TITLE (copy this):\n{title}\n\n"
        f"DESCRIPTION (copy this):\n{script['description']}\n\n{hashtags} #Shorts\n"
    )

    history.append({"topic": script["topic"], "title": script["title"]})
    HISTORY.write_text(json.dumps(history, indent=2))
    print("Saved out/short.mp4")


if __name__ == "__main__":
    main()
