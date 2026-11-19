#!/usr/bin/env python3
"""
vodclip - Katto-style VOD clipping for liveclipper v6 (separate module, v6 runtime untouched).

  python3 vodclip.py SOURCE [--top 10] [--layout stack|blur|crop] [--cam x,y,w,h] [--analyze-only]
    SOURCE = local video file | YouTube video / past stream URL | YouTube channel (@handle, newest past broadcasts, --latest N) | Twitch VOD URL

Pipeline: audio profile -> candidate moments -> deterministic 4-axis score (Hook/Flow/Value/Trend, 0-100)
          -> ranked shortlist (JSON) -> render top N (stacked gameplay+cam layout, captions).
Scoring is plain arithmetic on transcript, audio energy and scene cuts. No language model.
Works with no speech: Flow and Trend then rest on audio energy and scene cuts.
Renders go to clips_vod/ for review. Nothing is uploaded.
"""
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

SR = 8000
STEP = 0.5  # seconds per energy bin

HOOK_Q = re.compile(r"^(why|how|what|who|when|where|did|do|is|are|can|could|would|have|has)\b", re.I)
IMPACT = re.compile(r"\b(insane|crazy|unreal|wtf|omg|no way|never|worst|best|clutch|dead|died|won|win|lost|"
                    r"secret|shocking|impossible|biggest|first|finally|holy|what the)\b", re.I)
NUM = re.compile(r"\d")
SIGNOFF = re.compile(r"\b(subscribe|follow me|like and|see you (next|in)|thanks for watching|link in|sponsor|"
                     r"use code|discount)\b", re.I)
AROUSAL = re.compile(r"[!?]|\b(omg|wow|insane|crazy|no way|let'?s go|yes+|bro|dude|scream|help|stop)\b", re.I)
CONJ = {"and", "but", "so", "or", "because", "then", "also", "um", "uh"}
STOP = set("the a an is are was were to of in on it that this i you we he she they for with at as be".split())


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(path)])
    j = json.loads(r.stdout or "{}")
    s = (j.get("streams") or [{}])[0]
    return int(s.get("width", 0)), int(s.get("height", 0)), float(j.get("format", {}).get("duration", 0) or 0)


# ---------- audio ----------
def audio_profile(path, start=0.0, dur=None):
    """RMS in dB per STEP-second bin; empty array when there is no audio track."""
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start)] + (["-t", str(dur)] if dur else []) + \
          ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(SR * STEP)
    if len(x) < n:
        return np.array([])
    x = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((x ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms)


def scene_cuts(path, start, end, thresh=0.35):
    r = sh(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(end - start), "-i", str(path), "-an",
            "-vf", f"select='gt(scene,{thresh})',showinfo", "-f", "null", "-"])
    return [float(m) + start for m in re.findall(r"pts_time:([\d.]+)", r.stderr)]


# ---------- candidates ----------
def find_candidates(db, min_len=15, max_len=45, top=40, silence_db=-55.0):
    """Peaks in smoothed loudness above the video's own baseline, padded into windows."""
    if len(db) < 8:
        return []
    k = max(1, int(3 / STEP))
    sm = np.convolve(np.pad(db, k, mode="edge"), np.ones(k) / k, mode="same")[k:-k]
    base, spread = np.median(sm), max(np.std(sm), 1.0)
    order = np.argsort(sm)[::-1]
    picked = []
    for i in order:
        if sm[i] < silence_db or sm[i] < base + 0.5 * spread:
            break
        t = i * STEP
        if any(abs(t - p) < min_len for p in picked):
            continue
        picked.append(t)
        if len(picked) >= top:
            break
    total = len(db) * STEP
    out = []
    for t in sorted(picked):
        s = max(0.0, t - 12)
        e = min(total, t + 6)
        if e - s < min_len:
            e = min(total, s + min_len)
        out.append((round(s, 1), round(min(e, s + max_len), 1)))
    return out


# ---------- scoring ----------
def words_in(words, s, e):
    return [w for w in words if w[0] >= s and w[1] <= e]


def score_segment(s, e, words, db, cuts, video_len):
    """Deterministic 0-100: Hook 30, Flow 30, Value 25, Trend 15. Returns dict with axes and reason."""
    dur = max(e - s, 1e-6)
    seg_words = words_in(words, s, e)
    text = " ".join(w[2] for w in seg_words)
    first = " ".join(w[2] for w in words_in(words, s, s + 3))
    i0, i1 = int(s / STEP), int(e / STEP)
    seg_db = db[i0:i1] if len(db) else np.array([])
    has_speech = len(seg_words) >= 3
    # Hook (first 3 s)
    hook = 0.0
    if first:
        hook += 10 if HOOK_Q.search(first) else 0
        hook += 8 if IMPACT.search(first) else 0
        hook += 6 if NUM.search(first) else 0
        hook += 6 if seg_words and seg_words[0][2].lower().strip(",.") not in CONJ else 0
        if SIGNOFF.search(first):
            hook = 0
    elif len(seg_db) >= 6:
        # no speech: a loud, sudden opening counts as the hook
        head, rest = seg_db[:6].mean(), np.median(db) if len(db) else 0
        hook = float(np.clip((head - rest) * 1.5, 0, 20))
    # Flow
    flow = 0.0
    density = len(seg_words) / dur
    flow += min(density / 2.5, 1.0) * 12
    ncut = sum(1 for c in cuts if s <= c <= e)
    flow += 6 if 0 < ncut / dur * 10 <= 4 else (3 if ncut == 0 else 0)
    if len(seg_db) >= 4:
        sustained = float(np.mean(seg_db > np.median(db) - 3)) if len(db) else 0
        steadiness = 1.0 - min(float(np.std(seg_db)) / 12.0, 1.0)
        flow += sustained * 6 + steadiness * 6
    flow = min(flow, 30)
    # Value
    value = 0.0
    if seg_words:
        sub = [w for w in seg_words if w[2].lower().strip(",.!?") not in STOP]
        value += min(len(sub) / max(len(seg_words), 1), 1.0) * 10
        value += min(len(NUM.findall(text)), 3) * 2
        value += 4 if seg_words[0][2].lower().strip(",.") not in CONJ else 0
        value += 5 if seg_words[-1][2].endswith((".", "!", "?")) else 0
        if SIGNOFF.search(text):
            value -= 10
        value = max(0.0, min(value, 25))
    # Trend
    trend = 0.0
    if text:
        trend += 4 if "?" in text else 0
        trend += min(len(AROUSAL.findall(text)), 3) * 1.5
        trend += 2 if NUM.search(text) else 0
    if len(seg_db) and len(db):
        peak = float(seg_db.max() - np.median(db))
        trend += float(np.clip(peak / 12.0, 0, 1)) * 6
    trend = min(trend, 15)
    # penalty: sponsor-heavy opening of long videos
    pen = 10 if (video_len > 120 and s < 45) else 0
    total = max(0.0, hook + flow + value + trend - pen)
    axes = dict(hook=round(hook, 1), flow=round(flow, 1), value=round(value, 1), trend=round(trend, 1))
    best = max(axes, key=lambda k: axes[k] / {"hook": 30, "flow": 30, "value": 25, "trend": 15}[k])
    reason = f"strongest on {best}" + ("" if has_speech else "; no speech, scored on audio energy and cuts")
    if pen:
        reason += "; opening-45s penalty"
    return dict(start=s, end=e, score=round(total, 1), axes=axes, speech=has_speech, reason=reason)


def transcribe_windows(path, cands, model="small", lang=None):
    """Whisper only the candidate windows (not the whole VOD). Returns words with absolute timestamps."""
    import liveclipper as lc
    cfg = SimpleNamespace(model=model, lang=lang)
    out = []
    tmp = Path(tempfile.mkdtemp(prefix="vodtx_"))
    for i, (s, e) in enumerate(cands):
        wav = tmp / f"w{i}.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(s), "-t", str(e - s), "-i", str(path), "-vn", "-ac", "1",
                        "-ar", "16000", str(wav)], check=True)
        out += [(a + s, b + s, w) for a, b, w in lc.transcribe(wav, cfg)]
    return sorted(out)


def rank(path, words=None, top=10, max_len=45, transcribe=False, model="small", has_video=True):
    w, h, vlen = probe(path)
    db = audio_profile(path)
    if not vlen:
        vlen = len(db) * STEP
    cands = find_candidates(db, max_len=max_len)
    if transcribe and cands:
        words = (words or []) + transcribe_windows(path, cands, model)
    cuts = scene_cuts(path, 0, vlen) if has_video and w and vlen < 1800 else []
    scored = [score_segment(s, e, words or [], db, cuts, vlen) for s, e in cands]
    scored.sort(key=lambda d: -d["score"])
    keep = []
    for d in scored:  # drop windows that overlap a better one
        if all(d["end"] <= k["start"] or d["start"] >= k["end"] for k in keep):
            keep.append(d)
    return keep[:top]


# ---------- webcam detection + stacked layout ----------
def sample_frames(path, n=8, width=320):
    """n grayscale+color frames spread over the file as (n,H,W,3) uint8, via ffmpeg rawvideo."""
    w, h, dur = probe(path)
    if not w:
        return np.zeros((0, 1, 1, 3), np.uint8)
    sh_ = int(round(h * width / w / 2) * 2)
    fps = max(n / max(dur, 1.0), 0.01)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={fps},scale={width}:{sh_}",
                          "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True).stdout
    arr = np.frombuffer(raw, np.uint8)
    k = len(arr) // (width * sh_ * 3)
    return arr[: k * width * sh_ * 3].reshape(k, sh_, width, 3)


def skin_mask(rgb):
    f = rgb.astype(np.float32)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return (cb > 77) & (cb < 127) & (cr > 133) & (cr < 173) & (r > 60)


def detect_cam(path, min_skin=0.25):
    """Heuristic facecam finder: a corner rectangle that is skin-tone-rich in most frames.
    Returns (x, y, w, h) in source pixels, or None. Not a face detector - verify the first clip."""
    frames = sample_frames(path)
    if len(frames) < 3:
        return None
    W0, H0, _ = probe(path)
    n, fh, fw, _ = frames.shape
    best, best_score = None, 0.0
    for fracw in (0.30, 0.24, 0.18):
        cw, ch = int(fw * fracw), int(fw * fracw * 9 / 16 * 1.2)
        for cx, cy in ((0, 0), (fw - cw, 0), (0, fh - ch), (fw - cw, fh - ch)):
            reg = frames[:, cy:cy + ch, cx:cx + cw]
            frac = np.array([skin_mask(f).mean() for f in reg])
            present = float((frac > min_skin).mean())
            # a real facecam is a distinct box: the area just inside the frame beside it must be much less skin-toned
            nx = cx - cw if cx > 0 else cx + cw
            ny = cy - ch if cy > 0 else cy + ch
            nx = int(np.clip(nx, 0, fw - cw)); ny = int(np.clip(ny, 0, fh - ch))
            nb = float(np.median([skin_mask(f[ny:ny + ch, nx:nx + cw]).mean() for f in frames]))
            if nb > 0.5 * float(np.median(frac)):
                continue
            sc = present * float(np.median(frac))
            if present >= 0.75 and sc > best_score:
                best_score, best = sc, (cx, cy, cw, ch)
    if not best:
        return None
    sx = W0 / fw
    cx, cy, cw, ch = best
    return (int(cx * sx) // 2 * 2, int(cy * sx) // 2 * 2, int(cw * sx) // 2 * 2, int(ch * sx) // 2 * 2)


SPLITS = {"75/25": 0.75, "60/40": 0.60, "50/50": 0.50, "40/60": 0.40}


def stack_graph(cam, src_w, src_h, dur, split=0.65):
    """[0:v] -> [pre], 1080x1920. Gameplay top (webcam region cropped out when it sits at an edge), cam bottom."""
    D = f"{max(dur, 0.1):.2f}"
    top_h = int(round(1920 * split / 2) * 2)
    bot_h = 1920 - top_h
    cx, cy, cw, ch = cam
    return (f"[0:v]split=2[g][c];"
            f"[g]scale=1080:{top_h}:force_original_aspect_ratio=increase,crop=1080:{top_h}[gt];"
            f"[c]crop={cw}:{ch}:{cx}:{cy},scale=1080:{bot_h}:force_original_aspect_ratio=increase,crop=1080:{bot_h}[cb];"
            f"[gt][cb]vstack=inputs=2[m];color=c=0xFFD400:s=1080x12:d={D},format=yuv420p[bar];"
            f"[m][bar]overlay=x='-w+w*t/{D}':y=H-h[pre]")


# ---------- rendering ----------
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def render_clip(src, seg, out, layout="stack", cam=None, split=0.65, captions=True, model="small", lang=None):
    import liveclipper as lc
    dur = seg["end"] - seg["start"]
    w, h, _ = probe(src)
    tmp = Path(tempfile.mkdtemp(prefix="vodclip_"))
    clean = tmp / "clean.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f'{seg["start"]:.2f}', "-t", f"{dur:.2f}", "-i", str(src),
                    *ENC, "-c:a", "aac", "-b:a", "160k", str(clean)], check=True)
    words = lc.transcribe(clean, SimpleNamespace(model=model, lang=lang)) if captions else []
    if layout == "stack" and cam:
        graph = stack_graph(cam, w, h, dur, split)
    elif layout == "stack":
        print("[vodclip] no webcam found: falling back to blur layout (use --cam x,y,w,h to force stacking)")
        graph = lc.layout_graph("blur", dur)
    else:
        graph = lc.layout_graph(layout, dur)
    post = f"fade=t=in:st=0:d=0.15,fade=t=out:st={max(dur - 0.3, 0):.2f}:d=0.3"
    if words:
        lc.build_ass(words, 1080, 1920, tmp / "caps.ass", "Arial", None, None, None)
        post += ",subtitles=caps.ass"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clean), "-filter_complex", graph + f";[pre]{post}[v]",
                    "-map", "[v]", "-map", "0:a?", *ENC, "-c:a", "aac", "-b:a", "160k", str(out)], cwd=tmp, check=True)
    return dict(words=len(words))


def ytdlp(*args):
    return subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", *args])


COOKIES = None  # browser name for yt-dlp --cookies-from-browser (set by --cookies); helps when YouTube asks to sign in


def cookie_args():
    return ["--cookies-from-browser", COOKIES] if COOKIES else []


def _yt_run(args, timeout=1800):
    import liveclipper as lc
    ytd = lc.ytdlp_cmd()
    if not ytd:
        raise RuntimeError('yt-dlp not installed: pip3 install -U "yt-dlp[default,deno]"')
    r = subprocess.run([*ytd, "--no-warnings", "--no-playlist", *lc.js_runtime_args()[0], *cookie_args(), *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        lines = [l for l in (r.stderr or r.stdout).splitlines() if l.strip()]
        msg = lines[-1][:300] if lines else "yt-dlp failed"
        if "confirm you" in msg.lower() and "bot" in msg.lower():
            msg += "  -> add --cookies chrome (or safari/firefox/edge), or download the video yourself and pass the file"
        raise RuntimeError(msg)
    return r.stdout


def fetch_vod_audio(url, workdir, upto=None, start=0):
    """Audio-only pull (small). start/upto (seconds) limit it to a range (also keeps long streams and tests bounded)."""
    out = Path(workdir) / "vod_audio.mp4"
    out.unlink(missing_ok=True)
    extra = ["--download-sections", f"*{int(start)}-{int(upto)}"] if (upto or start) else []
    if YT_ID.match(url):
        for stale in Path(workdir).glob("vod_audio.*"):
            stale.unlink()
        _yt_run(["-f", "bestaudio[ext=m4a]/bestaudio/worst", *extra, "-o", str(out), url])
        if not out.exists():
            got = sorted(Path(workdir).glob("vod_audio.*"))
            if got:
                out = got[0]
    else:
        ytdlp("-f", "Audio_Only/audio_only/worstaudio/worst", *extra, "-o", str(out), url)
    if not out.exists():
        raise SystemExit("yt-dlp could not fetch the video audio (private/members-only/expired?)")
    return out


def fetch_vod_section(url, s, e, out):
    """Video only around one moment (padded 3s each side by the caller)."""
    ytdlp("-f", "720p60/720p/best[height<=720]/best", "--download-sections", f"*{s:.0f}-{e:.0f}",
          "--force-keyframes-at-cuts", "-o", str(out), url)
    if not Path(out).exists():
        raise SystemExit(f"could not download video section {s:.0f}-{e:.0f}")
    return out


YT_ID = re.compile(r"^(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|live/|shorts/)|youtu\.be/)([\w-]{11})")
TW_VOD = re.compile(r"^https?://(?:www\.)?twitch\.tv/videos/\d+")


def clip_source_ok(url):
    """Only plain YouTube video links and Twitch VOD links are accepted (no arbitrary URLs)."""
    return bool(YT_ID.match(url) or TW_VOD.match(url))


CACHE = Path.home() / ".cache" / "liveclipper" / "dl"


def download_video(url, cache_dir=None):
    """Download the WHOLE video to a local file with yt-dlp's own downloader (no ffmpeg on https), cached by video id.
    Everything after this (cutting, audio, rendering) reads a local file, so a static ffmpeg with broken TLS cannot fail.
    1080p for videos up to 30 min, 720p for longer ones (size)."""
    vid = YT_ID.match(url)[1]
    cache = Path(cache_dir or CACHE)
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{vid}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    info = yt_info(url)
    if info["live"]:
        raise RuntimeError("that video is live right now; clip it after the stream ends, or add the channel to watch.py")
    h = 1080 if info["duration"] and info["duration"] <= 1800 else 720
    for stale in cache.glob(f"{vid}.*"):
        stale.unlink()
    _yt_run(["-f", f"bv*[height<={h}]+ba/b[height<={h}]/b", "-S", "ext:mp4:m4a", "--merge-output-format", "mp4",
             "--hls-prefer-native", "--concurrent-fragments", "4", "-o", str(cache / f"{vid}.%(ext)s"), url], timeout=7200)
    if not out.exists():
        got = [p for p in cache.glob(f"{vid}.*") if p.suffix not in (".part", ".ytdl")]
        if not got:
            raise RuntimeError("yt-dlp finished but no video file appeared")
        out = got[0]
    for old in sorted(cache.glob("*.mp4"), key=lambda p: p.stat().st_mtime)[:-3]:   # keep the 3 newest
        old.unlink(missing_ok=True)
    return out


def cut_local(video, start, end, dest):
    """Cut [start,end] out of a LOCAL file (re-encoded, frame accurate). ffmpeg never touches the network here."""
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(video), "-t", f"{end - start:.2f}",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                        str(dest)], capture_output=True, text=True)
    if r.returncode or not Path(dest).exists():
        raise RuntimeError("ffmpeg could not cut the downloaded video: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])
    return dest


def extract_audio(video, dest, start=0, end=None):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(int(start)), "-i", str(video)] + (["-t", str(int(end - start))] if end else []) + \
          ["-vn", "-ac", "1", "-c:a", "aac", "-b:a", "64k", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode or not Path(dest).exists():
        raise RuntimeError("ffmpeg could not read the audio of the downloaded video: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])
    return Path(dest)


def fetch_range(url, start, end, dest, download=None):
    """[start,end] of a finished video as a local file. YouTube: download whole video first, then cut locally
    (yt-dlp --download-sections hands https to ffmpeg, which failed with 'ffmpeg exited with code 251' on a static ffmpeg)."""
    if YT_ID.match(url):
        return cut_local((download or download_video)(url), start, end, dest)
    import liveclipper as lc
    ytd = lc.ytdlp_cmd()
    if not ytd:
        raise RuntimeError('yt-dlp not installed: pip3 install -U "yt-dlp[default,deno]"')
    cmd = [*ytd, "--no-warnings", "--no-playlist", *cookie_args(), "-f", "720p60/720p/best[height<=720]/best",
           "--download-sections", f"*{start:.0f}-{end:.0f}", "--force-keyframes-at-cuts",
           "--merge-output-format", "mp4", "-o", str(dest), url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode or not Path(dest).exists():
        lines = [l for l in (r.stderr or r.stdout).splitlines() if l.strip()]
        raise RuntimeError(lines[-1][:300] if lines else "yt-dlp failed")
    return dest


def clip_range(url, start, end, out_dir, layout="blur", cam=None, split=0.65, captions=True, model="small",
               fetch=fetch_range):
    """Manual 'clip this part of a finished video': fetch the range, render a vertical clip. Review-gated like all clips."""
    if not clip_source_ok(url):
        raise ValueError("only YouTube video links and Twitch VOD links are supported")
    if not (0 <= start < end) or end - start > 180:
        raise ValueError("clip must be between 1 and 180 seconds")
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="manualclip_"))
    src = tmp / "src.mp4"
    fetch(url, max(0.0, start - 2), end + 2, src)
    off = start - max(0.0, start - 2)
    base = f"manual_{int(start)}-{int(end)}_{int(__import__('time').time())}"
    final = out_dir / f"{base}_final.mp4"
    if layout == "stack" and not cam:
        cam = detect_cam(src)
    render_clip(src, dict(start=off, end=off + (end - start)), final, layout, cam, split, captions, model)
    meta = dict(title="Manual clip", hook=None, score=None, trigger=f"manual clip {int(start)}s-{int(end)}s",
                source=url, seconds=end - start, final=final.name)
    (out_dir / f"{base}.json").write_text(json.dumps(meta, indent=2))
    return final


CHANNEL = re.compile(r"^(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/)?(@[\w.\-]+|channel/UC[\w-]{22}|c/[\w.\-]+|user/[\w.\-]+)(/(?:streams|videos|live))?/?$")


def is_channel(src):
    return bool(CHANNEL.match(src.strip()))


def resolve_channel(src, count=1, tab="streams"):
    """Newest `count` finished videos of a YouTube channel (past broadcasts live on its /streams tab). -> [(url, title)]"""
    m = CHANNEL.match(src.strip())
    if not m:
        raise ValueError("not a YouTube channel link or @handle")
    base = "https://www.youtube.com/" + m.group(1)
    tab = (m.group(2) or "/" + tab).strip("/")
    out = _yt_run(["--flat-playlist", "--playlist-end", str(max(count * 3, 5)), "--print", "%(id)s\t%(title)s\t%(live_status)s",
                   "--yes-playlist", f"{base}/{tab}"], timeout=180)
    vids = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) >= 2 and re.fullmatch(r"[\w-]{11}", p[0]) and (len(p) < 3 or p[2] not in ("is_live", "is_upcoming")):
            vids.append(("https://www.youtube.com/watch?v=" + p[0], p[1]))
    if not vids:
        raise RuntimeError(f"no finished videos found on {base}/{tab} (try the /videos tab or paste a video link)")
    return vids[:count]


def yt_info(url):
    """-> dict(channel, title, duration, live). Raises RuntimeError when the video cannot be read (private, removed, blocked)."""
    out = _yt_run(["--print", "%(channel)s\t%(title)s\t%(duration)s\t%(live_status)s", url], timeout=120)
    p = (out.strip().splitlines() or [""])[-1].split("\t") + ["", "", "", ""]
    try:
        dur = float(p[2])
    except ValueError:
        dur = 0.0
    return dict(channel=p[0], title=p[1], duration=dur, live=p[3] in ("is_live", "is_upcoming"))


def fetch_twitch_vod(url, workdir):
    """Download a Twitch VOD with yt-dlp (UNTESTED from our environment; run on your machine)."""
    out = Path(workdir) / "vod.mp4"
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "best[height<=720]/best", "-o", str(out), url]
    r = subprocess.run(cmd)
    if r.returncode or not out.exists():
        raise SystemExit("yt-dlp could not download that VOD (private/sub-only/expired?)")
    return out


def main_url(url, a):
    """Katto-style: audio first, score, then fetch video only around the kept moments."""
    work = a.out / "work"; work.mkdir(exist_ok=True)
    t0, t1 = (a.range or (0, a.audio_limit))
    info = dict(source=url)
    if YT_ID.match(url):
        i = yt_info(url)
        if i["live"]:
            raise SystemExit("that video is live right now: add the channel to watch.py instead, or clip it after the stream ends")
        info.update(channel=i["channel"], video_title=i["title"])
        if i["duration"] > 4 * 3600 and not a.range:
            print(f"[vodclip] {i['duration'] / 3600:.1f} h video: analysing all of it takes a while; use --range START-END (seconds) to do part")
    video = None
    if YT_ID.match(url):
        print("[vodclip] downloading the video (yt-dlp, no ffmpeg on the network) ...", flush=True)
        video = download_video(url)
        audio = extract_audio(video, work / "vod_audio.m4a", t0, t1)
    else:
        audio = fetch_vod_audio(url, work, t1, t0)
    ranked = rank(audio, None, a.top, a.max_len, transcribe=a.transcribe, model=a.model, has_video=False)
    for r in ranked:
        r["start"] += t0
        r["end"] += t0
        r.update(info)
    (a.out / "shortlist.json").write_text(json.dumps(ranked, indent=2))
    for i, r in enumerate(ranked, 1):
        print(f'{i:2d}. {r["score"]:5.1f}  {r["start"]:8.1f}-{r["end"]:8.1f}  {r["axes"]}  {r["reason"]}')
    if a.analyze_only:
        return ranked
    cam = tuple(int(v) for v in a.cam.split(",")) if a.cam else None
    for i, r in enumerate(ranked, 1):
        pad = 3.0
        s0 = max(0.0, r["start"] - pad)
        sec = work / f"sec_{i:02d}.mp4"
        if video:
            cut_local(video, s0, r["end"] + pad, sec)
        else:
            fetch_vod_section(url, s0, r["end"] + pad, sec)
        if a.layout == "stack" and cam is None and not a.cam:
            cam = detect_cam(sec)
            print("[vodclip] webcam:", cam or "not found")
        seg = dict(start=r["start"] - s0, end=r["end"] - s0)
        out = a.out / f"vod_{i:02d}_{r['score']:.0f}.mp4"
        render_clip(sec, seg, out, a.layout, cam, SPLITS[a.split], a.captions, a.model)
        print("[vodclip] wrote", out)
    return ranked


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--layout", choices=["stack", "blur", "crop"], default="stack")
    ap.add_argument("--cam", default=None, help="webcam rectangle x,y,w,h in source pixels (overrides detection)")
    ap.add_argument("--split", choices=list(SPLITS), default="60/40")
    ap.add_argument("--max-len", type=int, default=45)
    ap.add_argument("--no-captions", dest="captions", action="store_false")
    ap.add_argument("--model", default="small")
    ap.add_argument("--transcribe", action="store_true", help="whisper the candidate windows so text axes count")
    ap.add_argument("--audio-limit", type=int, default=None, help="URL mode: only analyse the first N seconds of audio")
    ap.add_argument("--range", default=None, metavar="START-END", help="URL mode: only analyse this part, in seconds (e.g. 3600-7200)")
    ap.add_argument("--cookies", default=None, metavar="BROWSER", help="YouTube asks you to sign in? use your browser's login: chrome, safari, firefox, edge")
    ap.add_argument("--latest", type=int, default=1, help="channel mode: how many of the newest past broadcasts to process")
    ap.add_argument("--analyze-only", action="store_true", help="write the ranked shortlist, render nothing")
    ap.add_argument("--out", type=Path, default=Path("clips_vod"))
    a = ap.parse_args(argv)
    a.out = a.out.resolve(); a.out.mkdir(parents=True, exist_ok=True)
    global COOKIES
    COOKIES = a.cookies
    if a.range:
        m = re.fullmatch(r"(\d+)-(\d+)", a.range)
        if not m or int(m[1]) >= int(m[2]):
            raise SystemExit("--range must look like 3600-7200 (seconds)")
        a.range = (int(m[1]), int(m[2]))
    src = a.source
    if is_channel(src):
        res = []
        for url, title in resolve_channel(src, a.latest):
            print(f"[vodclip] {title}  {url}")
            sub = argparse.Namespace(**vars(a)); sub.out = a.out / YT_ID.match(url)[1]; sub.out.mkdir(exist_ok=True)
            res += main_url(url, sub)
        return res
    if re.match(r"https?://", src):
        if not (YT_ID.match(src) or "twitch.tv/videos/" in src):
            raise SystemExit("supported links: YouTube videos / past streams, a YouTube channel (@handle), Twitch VODs; or pass a file")
        return main_url(src, a)
    src = Path(src)
    ranked = rank(src, None, a.top, a.max_len, transcribe=a.transcribe, model=a.model)
    (a.out / "shortlist.json").write_text(json.dumps(ranked, indent=2))
    for i, r in enumerate(ranked, 1):
        print(f'{i:2d}. {r["score"]:5.1f}  {r["start"]:8.1f}-{r["end"]:8.1f}  {r["axes"]}  {r["reason"]}')
    if a.analyze_only:
        return ranked
    cam = tuple(int(v) for v in a.cam.split(",")) if a.cam else (detect_cam(src) if a.layout == "stack" else None)
    if a.layout == "stack":
        print("[vodclip] webcam:", cam or "not found")
    for i, r in enumerate(ranked, 1):
        out = a.out / f"vod_{i:02d}_{r['score']:.0f}.mp4"
        render_clip(src, r, out, a.layout, cam, SPLITS[a.split], a.captions, a.model)
        print("[vodclip] wrote", out)
    return ranked


if __name__ == "__main__":
    main()
