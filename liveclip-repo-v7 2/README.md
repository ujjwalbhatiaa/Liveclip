> **New: liveclipper v7** lives in [`liveclipper/`](liveclipper/) - autopilot channel watch, Katto-style scored VOD clipping,
> a local control panel and a YouTube trim box. Site: https://ujjwalbhatiaa.github.io/Liveclip/ . It runs on your own computer
> only. The original `liveclip.py` below is unchanged. MIT licensed, see [LICENSE](LICENSE). Only clip content you have the
> rights to use.

# liveclip — clip a live Twitch or YouTube stream the moment chat explodes

The clipping system from [that reel](https://www.instagram.com/reel/DfzXVKJIKou/)
by [@aiverse.pranav](https://www.instagram.com/aiverse.pranav): a free tool that
records a live stream and cuts 30-second clips the second chat explodes, so
your clip is up before the stream ends — instead of downloading the VOD
overnight and posting the same 30 seconds as everyone else.

Now works on **Twitch and YouTube Live**.

**Website:** a landing page with a working live simulation of the system lives in `docs/index.html` — enable GitHub Pages on the repo (Settings → Pages → Deploy from branch → `/docs`) and it serves at `https://ujjwalbhatiaa.github.io/liveclip/`.

## How it works (exactly like the reel)

1. **Streamlink records from second one.** The stream is recorded live to
   disk from the moment you start — no waiting for the VOD.
2. **It counts chat every 10 seconds.** Twitch via anonymous IRC (no login);
   YouTube via chat-downloader (no login, no API key).
3. **Chat explodes → FFmpeg cuts 30 seconds.** A window counts as a spike
   when it beats both an absolute floor (default 25 messages) and a multiple
   of the recent baseline (default 3× the median of the last 12 windows).
   The clip covers 25s before the spike and 5s after it, cut with FFmpeg
   stream copy — ready in seconds.
4. **Whisper adds captions** when openai-whisper is installed (optional).
5. **Clips are ready before the stream ends.** All free. Only works live.

## Install

```bash
pip install streamlink         # recorder (Twitch + YouTube)
pip install chat-downloader    # YouTube live chat only
# ffmpeg must be on PATH (already installed on most systems)
pip install openai-whisper     # optional, for captions only
```

If `pip install --user` puts tools in `~/.local/bin`, add that to your
`PATH` or the tool will tell you it can't find them.

## Usage

```bash
# Twitch
python3 liveclip.py shroud --out gta6-launch --min-spike 40

# YouTube Live — video URL, video ID, or channel live URL
python3 liveclip.py https://www.youtube.com/watch?v=VIDEO_ID --vertical
python3 liveclip.py dQw4w9WgXcQ --vertical --thumbnails
python3 liveclip.py @SkyNews/live --wait-for-live

# Test your thresholds offline before going live
python3 liveclip.py --simulate-chat chat-sample.txt
```

GTA 6 launches **November 19** on PlayStation and Xbox — launch night is
the exact use case this was built for: start it on a big GTA 6 stream
(Twitch or YouTube) and it clips every chat explosion live.

Useful flags:

| Flag | Default | What it does |
| --- | --- | --- |
| `--platform` | auto | `twitch`, `youtube`, or auto-detect |
| `--window` | 10 | chat-count window, seconds |
| `--clip-length` | 30 | clip length, seconds |
| `--pre-roll` | 25 | seconds before the spike in the clip |
| `--min-spike` | 25 | absolute minimum messages/window |
| `--multiplier` | 3.0 | spike must also beat baseline × this |
| `--cooldown` | 60 | minimum seconds between clips |
| `--quality` | best | streamlink quality (`720p`, `1080p`, …) |
| `--duration` | 0 | stop after N seconds (0 = until Ctrl-C) |
| `--no-captions` | off | skip Whisper captions |
| `--vertical` | off | also export each clip as 9:16 vertical video for Shorts/Reels/TikTok |
| `--thumbnails` | off | also save a thumbnail still for each clip |
| `--wait-for-live` | off | wait until the stream goes live instead of exiting |
| `--max-wait` | 600 | with `--wait-for-live`, give up after N seconds |
| `--simulate-chat` | — | offline test: read `offset_seconds count` lines and print which windows would spike |

Clips land in `./clips/` as `clip_<source>_<time>_chat<N>.mp4`, with a
`clips.jsonl` manifest (platform, spike time, chat count, files),
`windows.csv` (every 10s chat window with its count, baseline, and whether
it spiked — use it to tune `--min-spike` / `--multiplier`), and the full
`recording.ts` alongside them. Vertical exports are
`clip_..._vertical.mp4` (1080×1920, blurred-background style) and
thumbnails are `clip_....jpg`.

If a stream refuses anonymous access, set `TWITCH_OAUTH_TOKEN` and the
recorder will use it.

## Tuning

- **Big launch night (GTA 6):** chat is fast — raise `--min-spike` (e.g. 100)
  so only real explosions clip, and raise `--multiplier` if small hype
  moments keep triggering.
- **Small stream:** lower `--min-spike` (e.g. 10) or the floor will never
  trigger.
- **Too many clips:** raise `--cooldown` (e.g. 180).
- **Not sure?** Record a stream's chat counts with `--simulate-chat` on a
  sample log first, or check `windows.csv` after a run and adjust.

## Tests

```bash
python3 -m pytest test_liveclip.py -q
```

31 tests, no network needed: IRC parsing, YouTube chat-line parsing,
spike detection (floor, multiplier, cooldown, window log), clip-window
math, platform detection, a real FFmpeg cut from a generated video (30s
verified), a real vertical export (1080×1920 verified), a real thumbnail,
the offline simulator, and both chat monitors end-to-end over injected
fakes.

## Honest limits

- **Run it on your own machine.** The chat connections need direct network
  access — some sandboxed environments block raw TCP (Twitch IRC) and
  proxy YouTube oddly. Verified here: chat-downloader reads live YouTube
  chat; streamlink + YouTube could not be verified from this sandbox
  (its requests get refused by the sandbox egress proxy) — on a normal
  machine streamlink's YouTube plugin is standard and works.
- A chat spike is a *proxy* for a good moment, not a guarantee — check
  clips before posting.
- Stream-copy cuts start at the nearest keyframe, so a clip can start a
  second or two early. Re-encode if you need frame-exact cuts.
- Captions need Whisper installed; without it, clips ship uncaptioned.
- It only works live — once the stream ends, so does the advantage.
