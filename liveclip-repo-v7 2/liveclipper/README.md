# liveclipper (v7)

> **Rights and responsibility.** This tool makes clips of streams and videos. It does not give you the right to publish them.
> Only clip and post content you own or have the creator's permission to use, and follow each platform's terms. Uploads stay
> a manual, per-clip human step (review_required and rights_cleared gates). The authors take no responsibility for how it is used.
> Runs on your own computer only. There is no hosted version.

## New in v7: autopilot channel watch, VOD clipping with scores, local control panel

Your v6 live-capture code is unchanged except for one additive block in main() (the --vod flags). Everything new lives in
watch.py, vodclip.py and webui.py.

### 1. Add a channel once, it clips whenever the channel goes live (watch.py)

    python3 watch.py add twitch:NAME          # or @youtubehandle, or a YouTube /live URL
    python3 watch.py run -- --audio-trigger --thumbnails     # everything after -- goes to liveclipper.py
    python3 watch.py status

- Checks every 60 s whether each channel is live. Twitch: streamlink if installed, else yt-dlp. YouTube: reads the public
  /live page. Detection is separate from capture, so you still get "live, capture blocked" if YouTube refuses the video.
- Live -> starts the v6 clipper for that channel. Offline -> stops it. Max 3 clippers at once (--max).
- Clips go to watch_data/channels/NAME/DATE/. Logs: watch_data/watch.log and channels/NAME/clipper.log.
- A clipper that keeps dying while live is retried with backoff (30 s doubling to 10 min). After 3 fast failures the status
  becomes capture_blocked and you get one notice (desktop on Mac; Discord webhook if you pass --webhook URL).
- When a Twitch stream ends it waits 2 minutes, then runs the VOD highlights pass on that VOD (--no-vod to skip).
  NOT TESTED end to end: the step that finds the latest VOD. The VOD pass itself was tested.
- Keep it running with the Mac/Windows start-on-login files in autostart/ (you run them yourself, nothing installs itself).

### 2. Clip a finished video or Twitch VOD with Katto-style scoring (vodclip.py)

    python3 liveclipper.py --vod https://www.twitch.tv/videos/123456789 --top 10
    python3 liveclipper.py --vod my_stream.mp4 --vod-layout stack --cam 1400,700,500,300

- Twitch URL: downloads audio only, finds loud moments, transcribes just those windows, scores them, then downloads video
  only around the kept moments. Local file: same scoring on the file.
- Score 0-100 = Hook 30 + Flow 30 + Value 25 + Trend 15, plain arithmetic on the transcript, loudness and scene cuts. No AI
  model. With no speech it uses loudness and scene cuts. It ranks clips within one video; it does not predict views.
- Layout "stack": gameplay on top, webcam in the bottom band (split 75/25, 60/40, 50/50 or 40/60 with vodclip.py --split).
  Webcam detection is a skin-tone heuristic, NOT a face detector. It returned "no webcam" on a real full-frame stream, and was
  only proven finding a webcam on synthetic video. Always check the first clip, and use --cam x,y,w,h if it is wrong.
  If no webcam is found it uses the blurred vertical layout.
- Writes clips_vod/ with shortlist.json. Nothing is uploaded.

### 2b. Clip YouTube videos and past streams (vodclip.py, also the "Clip a video" tab)

    python3 vodclip.py https://youtu.be/VIDEO_ID --top 8 --transcribe
    python3 vodclip.py @KaiCenat --latest 2 --top 5 --transcribe      # newest 2 past broadcasts of a channel
    python3 vodclip.py https://youtu.be/VIDEO_ID --range 3600-7200      # only analyse that part (seconds)
    python3 vodclip.py https://youtu.be/VIDEO_ID --cookies chrome       # if YouTube says "sign in to confirm you're not a bot"

- The video is downloaded WHOLE with yt-dlp's own downloader into ~/.cache/liveclipper/dl (720p over 30 min, 1080p under; the
  3 newest are kept). All cutting, audio analysis and rendering then read that local file. ffmpeg never opens an https URL,
  which is what failed with "ERROR: ffmpeg exited with code 251" (yt-dlp --download-sections hands the link to ffmpeg).
- streamlink only works for live streams, so it is not used here. Live-now videos are refused.
- Long streams: a 6 h stream is a multi-GB download. Use --range, or expect a long wait.
- NOT proven from our side: no YouTube download was possible from our environment. Tested with a stand-in downloader and
  real ffmpeg on synthetic video. Your first run is the real test.

### 2c. Titles, descriptions, hashtags, thumbnails (metadata.py)

Every clip gets a generated title, description (credit line, keywords, hashtags), tag list, short caption and a 1280x720
thumbnail, never the file name. Built only from the clip's real hook, transcript and channel, so it never invents an event.
Review queue > "Title, description, thumbnail" shows and edits them; your edits are saved in watch_data/metadata.json and used
for every platform and by autopilot. Untouched clips use the generated text. Set the game (default GTA 6) in the Upload tab.

### 3. Local control panel (webui.py)

    python3 webui.py        # then open http://127.0.0.1:8765

Add and remove channels, start/stop the watcher, see live and capture status, browse clips with their scores, and approve
or reject in a review queue (saved in watch_data/review.json). It listens on this computer only. Uploading is described in section 4 and is off until you connect YouTube.

### 4. Upload to YouTube, by hand or on autopilot (publisher.py, Upload tab)

Uploading publishes as you, so it is off until you set it up:
1. Google Cloud Console: create a project, enable "YouTube Data API v3", configure the OAuth consent screen (add yourself as a test user),
   create an OAuth client of type "Desktop app", download it as `client_secret.json` into `~/.config/liveclipper/`. Never commit or share it.
2. `pip3 install google-api-python-client google-auth-oauthlib` (already in requirements.txt), open the web UI, Upload tab, "Connect YouTube".
3. Review queue: "Approve + upload" uploads one clip. Default privacy is private.
4. Autopilot (Upload tab): off by default. Modes "approved only" or "every new clip except rejected". Turning it on needs the rights tick box.
   Rejected clips never upload, each clip uploads once (3 tries on errors), and everything is logged in watch_data/uploads.jsonl and the Upload tab.
   Autopilot runs while webui.py is running. Title, description and thumbnail come from the clip pipeline (_upload.json).
Facebook Reels, Instagram Reels and TikTok uploads are built too (platforms.py, set up in the same tab, steps and approvals in PLATFORMS.md). Pick
which platforms to upload to with "Upload to" in the Upload tab; autopilot posts to every selected platform that is connected. Facebook and
Instagram posts are public the moment they publish. Twitch has no upload API, so there is nothing to build there.
Tested with fake uploaders / fake HTTP layers only; NONE of the four is tested against the real service yet.

### v7 honesty notes
- YouTube capture is still unproven from our side. Run python3 liveclipper.py --check @channel on your machine first.
- requirements.txt: your v6 list, unchanged, plus numpy (used by vodclip.py; faster-whisper installs it anyway).
- Tests: python3 -m unittest discover -s tests  (watch, vodclip and webui tests use fake or synthetic inputs, no network).

---

Records Twitch / YouTube live streams, detects chat spikes, and cuts ready-to-review vertical clips with
captions, a hook, a watermark, a thumbnail, and a title/description file. Upload is a separate, human-gated step.
v6 adds a rebuilt YouTube capture path, a --check diagnostic, a --cookies option, and loudness triggers.

## Install (Mac)
    brew install ffmpeg streamlink
    pip3 install -r requirements.txt
    # requirements.txt: faster-whisper, streamlink, yt-dlp[default,deno], google-api-python-client,
    # google-auth-oauthlib, Pillow. The yt-dlp[default,deno] extra installs yt-dlp plus the deno JS runtime
    # and the yt-dlp-ejs solver scripts yt-dlp needs to read YouTube (https://github.com/yt-dlp/yt-dlp/wiki/EJS).
    # Install yt-dlp with pip like this, not brew: the tool runs it as `python3 -m yt_dlp` so it can see
    # the pip-installed deno and ejs packages.

## Test it first (no real stream, takes ~2 min)
    python3 liveclipper.py --selftest --thumbnails --hook "TEST" --handle "@you" --out test_clips
    python3 liveclipper.py --simulate-chat chat.txt     # chat.txt lines: "<seconds> <message_count>"
    python3 liveclipper.py -h

## Check a source before a big stream (new in v6)
    python3 liveclipper.py --check @somehandle
    python3 liveclipper.py --check https://www.youtube.com/live/VIDEO_ID
Run this BEFORE launch night. --check prints your yt-dlp / streamlink / ffmpeg / YouTube-JS-setup status,
opens each source the way a real run would, test-records ~12 seconds, ffprobes what it captured, checks
chat access, and says exactly what is wrong and what to fix. Do this early enough that you can still fix
setup problems before the stream starts.

## Run the unit tests
    python3 -m pytest tests      # 16 tests (pip3 install pytest), no network: blocked / offline / retry, recorder failure

## Real use
    python3 liveclipper.py CHANNEL --thumbnails --handle "@you" --keywords "gta,heist" --duration 3600
Start before the stream. Enter = clip last 30s, q + Enter = quit. Each clip writes, in clips/:
`*_final.mp4` (9:16), `*_clean.mp4`, `*_thumb.jpg`, `*.json`, `*_upload.json`, `index.html`.
Stopping (q or --duration) waits for clips being rendered, then exits.

## Upload (yt_upload.py)
1. Review the clip and edit `*_upload.json` (title, description).
2. Only if you own the content or have the streamer's permission, set `"rights_cleared": true`.
3. `python3 yt_upload.py clips/X_upload.json` = dry run. Add `--confirm` to upload; default privacy is private.
Setup: Google Cloud project, enable YouTube Data API v3, create an OAuth "Desktop app" client, save as
client_secret.json. The login token is stored at ~/.config/liveclipper/youtube_token.json (owner-only, 0600).
Never share or commit client_secret.json or the token.
This upload step is untested against the real YouTube API; run the dry run first and check the result in YouTube Studio.

## If it says BLOCKED, or keeps retrying
`BLOCKED ... NOT offline` means YouTube/Twitch refused automated access (sign-in or bot check). The tool stops
watching that source and does not retry; nothing is being recorded. You need to act, for example use another
source or another network/account the service allows.
CHANGED IN v6: any other recorder failure (unknown error, HTTP 403/429, streamlink finding no playable stream)
is NO LONGER terminal. The tool retries it forever, waiting longer each time up to about 60 seconds between
attempts, and prints `[attempt N] - retrying`. There is no give-up-after-3 and no loud final failure, so watch
the log: a source that cannot work will now sit retrying at ~60s cadence instead of stopping and telling you.
`no live stream found yet` is the normal offline case and keeps checking. If a YouTube source is not live yet,
the tool polls every 20s until it starts. Always confirm clips actually appear in the output folder.

## Tested vs not tested
Tested with real Twitch (one public channel, private technical test): finding the live stream, recording video (1080p h264 + aac), connecting to Twitch chat anonymously, a clean 90s run with no auto-clip because that channel's chat was too quiet, and rendering a 10s vertical clip from the real capture using a MANUAL trigger (not an organic chat spike), without captions or --ai.
Tested offline on synthetic media: spike detection (--selftest and --simulate-chat), clip cutting, 9:16 render with hook/watermark, v5 captions/zoom/progress bar/loudness, designed thumbnail, upload-pack JSON, --duration stop, burned-in word captions on text-to-speech test audio (Whisper tiny), yt_upload.py dry run and its refusals, --check on a local file, plus 16 unit tests (including blocked/offline classification and retry behaviour).
NOT tested: an organic chat spike on a real stream, captions on real stream audio, --ai (needs your ANTHROPIC_API_KEY), the real YouTube OAuth/upload/thumbnail call, running on a Mac, long unattended runs. Speed: rendering took ~30-80s per clip on a single-CPU test machine; yours will differ.
YOUTUBE CAPTURE IS STILL UNPROVEN: the v6 YouTube path (resolve the HLS URL with yt-dlp, then record it with ffmpeg from the live edge) was built after YouTube's bot check refused every capture attempt from the test machine, and it has NOT been tested against a real YouTube stream. Run --check on your own machine before relying on it. Twitch capture via streamlink is unchanged and was real-tested.

## Rules that matter
- Clip only streams you own or have permission for. Rights, platform terms and YouTube monetization are separate:
  YouTube's reused/inauthentic content policy can deny monetization for unchanged reposts even with permission,
  so add real commentary or edits.
- Nothing here guarantees views or income.


## New in v5: captions, editing, thumbnails

- Captions: 1-3 words at a time, the spoken word pops in bigger and yellow (hot red-orange on hype words, `!` and `?`), heavy outline, lower third.
- Editing: slow punch-in on the main video, a progress bar, short fade in/out, loudness normalisation to -14 LUFS (skipped when the audio is near-silent so noise is not boosted).
- Thumbnails (`--thumbnails`): 1280x720. Samples 12 frames, picks the sharpest, most contrasty, not-dark one, crops toward the busiest part, grades it, and sets a big headline.
  The headline comes from `--hook`, else `--ai` copy, else the clip's own transcript (first strong sentence), else "LIVE CLIP". Chat text is never used as a headline. The headline is only a short phrase from those sources, so it can be wrong or misleading out of context. Check it yourself. No clicks or accuracy are guaranteed.
  A designed thumbnail is not a click guarantee. Check the headline matches what happens in the clip.
- Needs `Pillow` (in requirements.txt). Thumbnail info is in the clip's .json (`thumbnail_info`).

### Review before posting
Auto-picked headlines (from the transcript or `--ai`) come from the clip's words but can still be wrong out of context. Each `*_upload.json` has `review_required: true` and `reviewed_title_thumbnail_hook: false`, plus the headline and where it came from. `yt_upload.py` refuses to upload until you have watched the clip, checked that the title, burned-in hook and thumbnail headline are true for it, and set both `reviewed_title_thumbnail_hook` and `rights_cleared` to true yourself.

### About the demo files
The demo clip's speech is a text-to-speech voiceover generated for testing, laid over unrelated stream footage. It is not anyone's real speech, and its headline is a demo only.

## New in v5.1: direct feeds (no YouTube/Twitch player needed)
Give the tool a stream you were given instead of a channel: an HLS (.m3u8) or DASH (.mpd) URL, an rtmp:// rtsp:// or srt:// address, or a file path.
    python3 liveclipper.py "https://your-feed.example/live.m3u8" --audio-db 8 --thumbnails --out clips
Direct feeds have no chat, so a clip triggers when the audio gets `--audio-db` dB louder than the recent baseline (not near-silent). It does NOT understand "viral". It only hears loudness, so music or ads can trigger it.
Tested: a local HLS feed served over HTTP (made with ffmpeg, with a loud section) was recorded, the loud section triggered a clip, and it rendered to a 1080x1920 video + thumbnail. Not tested: RTMP/SRT/DASH, real broadcast feeds, long runs. This does not change YouTube channel capture: YouTube links still go through yt-dlp/streamlink and may be refused by YouTube's bot check.

## New in v6
- Rebuilt YouTube capture: yt-dlp resolves the stream's direct HLS URL (re-resolved each recording session, since
  those URLs expire), then ffmpeg records it from the live edge with reconnect. Streamlink `--stream-url` is the
  fallback. If the source is not live yet, the tool polls every 20s until it starts.
- `--check TARGET...`: pre-flight diagnostic (see above). Use it before launch night.
- `--cookies BROWSER` (chrome, safari, firefox, edge, brave): passed to yt-dlp's standard
  `--cookies-from-browser` option, which makes yt-dlp use the YouTube login from that browser on your machine.
  This only reads the browser you name on the computer you run it on.
- Retry behaviour changed: non-blocked recorder failures retry forever with backoff up to ~60s (see
  "If it says BLOCKED, or keeps retrying"). A bot/sign-in refusal is still terminal and never retried.
- `--audio-trigger`: for Twitch/YouTube channels, also clip when the audio gets `--audio-db` dB louder than the
  recent baseline (in addition to chat spikes). Useful when chat is quiet. Loudness is not "viral": music, ads
  or a loud intro can trigger it.
- YouTube chat fallback: if the watch page does not expose a chat continuation, the pop-out chat page is tried.
