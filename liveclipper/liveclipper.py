#!/usr/bin/env python3
"""
liveclipper v6 (v5 + reliable YouTube capture, --check diagnostics, --cookies) - clips Twitch AND YouTube live streams WHILE they are live.

  Streamlink (or yt-dlp) records -> rolling 2s segments on disk
  Twitch IRC / YouTube live chat -> counted every 10s (no login, no API key)
  Chat explodes -> FFmpeg cuts the moment (pre-roll included)
  Whisper -> word-by-word burned-in captions
  Output -> 9:16 vertical clip ready to post, before the stream ends

Usage:
  python3 liveclipper.py TARGET [TARGET2 ...]
    TARGET = twitch channel name | @youtubehandle | any YouTube live URL
  python3 liveclipper.py --selftest          # verify install without Twitch
  python3 liveclipper.py --check @handle     # diagnose a YouTube/Twitch source (video + chat)
While running:  Enter = clip the last 30s now,  q + Enter = quit
"""
import argparse, collections, html, json, os, queue, random, re, shutil, socket, ssl
import statistics, subprocess, sys, sysconfig, threading, time, urllib.request
from pathlib import Path

SEG = 2  # seconds per buffer segment
PRIV = re.compile(r"^:(\w+)!\S+ PRIVMSG #\w+ :(.*)$")
HYPE = (r"\b(clip( it)?|omg|wtf|no way|lmao+|lol|holy|insane|w+|l+|pog\w*|kekw|omegalul|lul"
        r"|gg|rip|let'?s go+|sheesh|ha(ha)+|ayo+|bruh%s)\b|[!?]{2,}")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+1; SOCS=CAI"}

stop = threading.Event()
_print_lock = threading.Lock()


def cfg_verbose(cfg):
    return getattr(cfg, "verbose", False)


def log(tag, msg):
    with _print_lock:
        print(f"{time.strftime('%H:%M:%S')} [{tag}] {msg}", flush=True)


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.strip()[-400:]}")


def http(url, body=None, headers=None):
    req = urllib.request.Request(url, json.dumps(body).encode() if body else None,
                                 {**UA, "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


DIRECT = re.compile(r"^(rtmps?|rtsps?|srt)://|^https?://\S+\.(m3u8|mpd)(\?\S*)?$", re.I)


def parse_target(t):
    """-> (platform, url, short name). 'direct' = a stream/file you were given: HLS/DASH/RTMP/RTSP/SRT URL or a file."""
    if DIRECT.search(t) or (not re.search(r"youtu|twitch", t) and Path(t).is_file()):
        name = re.sub(r"[^\w.-]+", "_", Path(re.sub(r"[?#].*", "", t).rstrip("/")).name or "direct")[:30]
        return "direct", t, "in-" + (name or "direct")
    if t.startswith("@"):
        t = f"https://www.youtube.com/{t}/live"
    if re.search(r"youtu\.?be", t):
        url = t if t.startswith("http") else "https://" + t
        m = re.search(r"@([\w.-]+)", url) or re.search(r"(?:v=|youtu\.be/|/live/)([\w-]{11})", url)
        if "@" in url and not url.rstrip("/").endswith("/live"):
            url = url.rstrip("/") + "/live"
        return "youtube", url, "yt-" + (m[1].lower() if m else "live")
    name = t.lower().lstrip("#").rstrip("/").rsplit("/", 1)[-1]
    return "twitch", f"https://twitch.tv/{name}", name


# ----------------------------------------------------------------- youtube chat (no API key)

def recorder_failure(text, returncode):
    """Classify a recorder (streamlink / yt-dlp) that exited without producing video.
    -> 'blocked' | 'offline' | 'failed'"""
    low = (text or "").lower()
    if "not a bot" in low or "sign in to confirm" in low or "login required" in low or "members-only" in low:
        return "blocked"
    if ("is offline" in low or "not currently live" in low or "will begin" in low or "not live right now" in low
            or "premieres in" in low or "has ended" in low):
        return "offline"
    return "failed"


class AccessBlocked(Exception):
    """YouTube refused automated access from this network (sign-in / bot check). NOT the same as offline."""


class StateUnknown(Exception):
    """The page did not look like a live watch page and gave no recognised reason."""


def yt_classify(page):
    """-> 'blocked' | 'offline' | 'unknown' for a watch page that has no usable live chat."""
    m = re.search(r'"playabilityStatus":\{"status":"(\w+)"(?:,"reason":"([^"]*)")?', page)
    status, reason = (m.group(1), (m.group(2) or "")) if m else ("", "")
    low = reason.lower()
    if status == "LOGIN_REQUIRED" or "not a bot" in low or "sign in to confirm" in low:
        return "blocked"
    if status in ("LIVE_STREAM_OFFLINE", "UNPLAYABLE") and ("offline" in low or "live event will begin" in low or status == "LIVE_STREAM_OFFLINE"):
        return "offline"
    if '"isLive":true' in page.replace(" ", "") or '"isLiveNow":true' in page.replace(" ", ""):
        return "unknown"       # looks live but chat could not be read
    if status == "OK" and '"isLive' not in page:
        return "offline"       # a normal, non-live video
    return "unknown"


def yt_initial(page):
    """From a watch page: (video_id, api_key, client_version, first chat continuation)."""
    vid = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', page)
    if not vid:
        kind = yt_classify(page)
        if kind == "blocked":
            raise AccessBlocked("YouTube asked for sign-in / bot check for this network")
        if kind == "offline":
            raise LookupError("not live yet")
        raise StateUnknown("watch page not recognised as live or offline")
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page)
    cont = None
    m = re.search(r"var\s+ytInitialData\s*=\s*({.*?})\s*;\s*</script>", page, re.S)
    if m:
        try:
            lc = json.loads(m[1])["contents"]["twoColumnWatchNextResults"]["conversationBar"]["liveChatRenderer"]
            try:   # last menu item = "Live chat" (all messages) rather than filtered "Top chat"
                items = lc["header"]["liveChatHeaderRenderer"]["viewSelector"]["sortFilterSubMenuRenderer"]["subMenuItems"]
                cont = items[-1]["continuation"]["reloadContinuationData"]["continuation"]
            except (KeyError, IndexError, TypeError):
                cont = next(iter(lc["continuations"][0].values()))["continuation"]
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    if not cont and key:
        cont = yt_popout_cont(vid[1])
    if not (key and cont):
        kind = yt_classify(page)
        if kind == "blocked":
            raise AccessBlocked("YouTube asked for sign-in / bot check for this network")
        raise LookupError("live chat not available (stream offline or chat disabled)")
    return vid[1], key[1], ver[1] if ver else "2.20250101.00.00", cont


def yt_popout_cont(vid):
    """Fallback: the pop-out chat page carries its own first continuation."""
    try:
        page = http(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}")
        m = re.search(r'(?:var\s+ytInitialData|window\["ytInitialData"\])\s*=\s*({.*?})\s*;\s*</script>', page, re.S)
        lc = json.loads(m[1])["contents"]["liveChatRenderer"]
        return next(iter(lc["continuations"][0].values()))["continuation"]
    except Exception:
        return None


def yt_parse(lc, now):
    """liveChatContinuation -> ([(time, user, text)], next continuation, wait ms)"""
    out = []
    for a in lc.get("actions") or []:
        for kind, r in ((a.get("addChatItemAction") or {}).get("item") or {}).items():
            if kind not in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
                continue
            text = "".join(x.get("text") or ((x.get("emoji") or {}).get("shortcuts") or [""])[0]
                           for x in (r.get("message") or {}).get("runs") or [])
            if kind == "liveChatPaidMessageRenderer":
                text = "SUPERCHAT!! " + text          # paid messages count as hype
            try:
                t = min(int(r["timestampUsec"]) / 1e6, now)
            except (KeyError, ValueError):
                t = now
            out.append((t, (r.get("authorName") or {}).get("simpleText", "?"), text))
    c = next(iter(((lc.get("continuations") or [{}])[0]).values()), {})
    return out, c.get("continuation"), c.get("timeoutMs") or 5000


# ----------------------------------------------------------------- channel

def audio_decision(chat_fired, db, ahist, cfg):
    """Combined trigger semantics (--audio-trigger). Chat and audio are independent: either can clip.
    - chat spike fired: that is the trigger, audio is not asked (the loud window is not added to the baseline)
    - no audio reading (None, e.g. no audio track): audio never blocks or triggers anything
    - near-silence and warm-up never trigger. Returns True when the audio should trigger a clip."""
    if db is None:
        return False
    if chat_fired:
        return False
    if audio_spike(ahist, db, cfg):
        return True
    ahist.append(db)
    return False


def ytdlp_cmd():
    """Run yt-dlp with THIS Python when possible, so it sees the yt-dlp-ejs + deno installed by pip
    (a brew/standalone yt-dlp can't see them, which is what breaks YouTube)."""
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return ["yt-dlp"] if shutil.which("yt-dlp") else None


def js_runtime_args():
    """yt-dlp needs a JavaScript runtime (+ the yt-dlp-ejs scripts) for YouTube. Deno is picked up by default
    (incl. the pip one); if only Node is installed, tell yt-dlp to use it. Returns (extra args, warning or None)."""
    try:
        import yt_dlp_ejs  # noqa: F401
        ejs = True
    except ImportError:
        ejs = False
    pip_deno = os.path.join(sysconfig.get_path("scripts"), "deno" + (sysconfig.get_config_var("EXE") or ""))
    if shutil.which("deno") or os.access(pip_deno, os.X_OK):
        args, have = [], True
    elif shutil.which("node"):
        args, have = ["--js-runtimes", "node"], True
    else:
        args, have = [], False
    warn = None
    if not have:
        warn = 'no JavaScript runtime found - run: pip3 install -U "yt-dlp[default,deno]"'
    elif not ejs:
        warn = 'yt-dlp-ejs missing - run: pip3 install -U "yt-dlp[default,deno]"'
    return args, warn


def resolve_stream(url, cfg):
    """Live page -> (direct HLS media URL, http headers). yt-dlp first, streamlink as fallback.
    Raises AccessBlocked (sign-in/bot check), LookupError (not live), RuntimeError (anything else)."""
    errs = []
    ytd = ytdlp_cmd()
    if ytd:
        cmd = [*ytd, "-j", "--no-warnings", "--no-playlist", *js_runtime_args()[0], "-f", "b[protocol^=m3u8]/b",
               *(["--cookies-from-browser", cfg.cookies] if cfg.cookies else []), url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
            if r.returncode == 0 and r.stdout.strip():
                info = json.loads(r.stdout.strip().splitlines()[-1])
                if info.get("live_status") not in (None, "is_live") and not info.get("is_live"):
                    raise LookupError(f"not live right now ({info.get('live_status')})")
                media = info.get("url") or ((info.get("requested_formats") or [{}])[0]).get("url")
                if media:
                    return media, info.get("http_headers") or {}
                errs.append("yt-dlp: no stream URL in result")
            else:
                errs.append(r.stderr.strip())
        except subprocess.TimeoutExpired:
            errs.append("yt-dlp timed out")
    if shutil.which("streamlink"):
        r = subprocess.run(["streamlink", "--stream-url", url, cfg.quality], capture_output=True, text=True, timeout=90)
        out = r.stdout.strip()
        if r.returncode == 0 and out.startswith("http"):
            return out.splitlines()[-1], {}
        errs.append((r.stdout + r.stderr).strip())
    text = "\n".join(e for e in errs if e) or "no recorder installed"
    last = [l for l in text.splitlines() if l.strip()][-1][:300]
    kind = recorder_failure(text, 1)
    if kind == "blocked":
        raise AccessBlocked(last)
    if kind == "offline":
        raise LookupError(last)
    raise RuntimeError(last)


YT_SOURCE = -1  # --yt-source: 0 UA-only headers (default), 1 no headers, 2 all yt-dlp headers (v6 behaviour), 3 streamlink


def hls_source(media, headers, mode=None, page_url=None, quality="best"):
    """ffmpeg (or streamlink, mode 3) reading a live HLS URL from the live edge -> MPEG-TS on stdout."""
    mode = YT_SOURCE if mode is None else mode
    if mode == -1:   # auto: streamlink worked on a real Mac where ffmpeg TLS failed, so prefer it when installed
        mode = 3 if shutil.which("streamlink") else 0
    if mode == 3:
        return ["streamlink", "--stdout", page_url, quality]
    headers = headers or {}
    if mode == 0:
        headers = {k: v for k, v in headers.items() if k.lower() == "user-agent"}
    elif mode == 1:
        headers = {}
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    try:
        import certifi
        ca = ["-ca_file", certifi.where()]
    except Exception:
        ca = []
    return ["ffmpeg", "-loglevel", "error", *ca, *(["-headers", hdr] if hdr else []),
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
            "-live_start_index", "-1", "-i", media,
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "mpegts", "pipe:1"]


def window_loudness(files):
    """Mean volume in dB over the given finished segments, or None if there is no audio."""
    vals = []
    for f in files:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(f), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True)
        m = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
        if m:
            vals.append(float(m.group(1)))
    return statistics.fmean(vals) if vals else None


def audio_spike(hist, db, cfg):
    """True when this window is cfg.audio_db louder than the baseline and not near-silent."""
    if not hist or len(hist) < cfg.warmup or db < -40:
        return False
    return db >= statistics.fmean(hist) + cfg.audio_db


class Channel:
    def __init__(self, name, cfg, jobs):
        self.platform, self.url, self.name = parse_target(name)
        self.cfg, self.jobs = cfg, jobs
        self.buf = cfg.work / "buffer" / self.name
        shutil.rmtree(self.buf, ignore_errors=True)
        self.buf.mkdir(parents=True)
        self.msgs = collections.deque()          # (time, user, text)
        self.lock = threading.Lock()
        self.hist = collections.deque(maxlen=cfg.history)
        self.pending = None
        self.ahist = collections.deque(maxlen=cfg.history)   # audio baseline (dB) for --audio-trigger
        self.last_clip = 0.0
        self.session = 0
        self.procs = []
        self.recording = False
        self.terminal = None        # set to a reason string when this source is permanently given up
        self.fail_count = 0

    def start(self):
        for fn in (self.record_loop, self.chat_loop, self.tick_loop):
            threading.Thread(target=fn, daemon=True).start()

    # --- recording: source | ffmpeg segmenter -> rolling buffer
    def give_up(self, kind, detail):
        """Stop this source for good and tell the user what to do. Never retried automatically."""
        if self.terminal:
            return
        self.terminal = kind
        if kind == "blocked":
            log(self.name, f"BLOCKED: {detail}. Stopped watching this source (not retrying). It is NOT offline: "
                           "YouTube/Twitch refused automated access from this network. Nothing is being recorded. "
                           "Fix: add --cookies chrome (uses your signed-in browser), or run --check to see details.")
        else:
            log(self.name, f"RECORDER FAILED: {detail}. Stopped watching this source (not retrying). Nothing is being recorded.")
        self.kill()

    def record_loop(self):
        cfg = self.cfg
        quiet_until = 0.0
        while not stop.is_set():
            self.session += 1
            if self.platform == "youtube" and not cfg.selftest:
                try:
                    media, hdrs = resolve_stream(self.url, cfg)
                except AccessBlocked as e:
                    return self.give_up("blocked", f"YouTube refused access ({e})")
                except LookupError as e:
                    if time.time() > quiet_until:
                        log(self.name, f"not live yet ({e}) - checking every 20s")
                        quiet_until = time.time() + 300
                    stop.wait(20)
                    continue
                except Exception as e:
                    self.fail_count += 1
                    log(self.name, f"can't open YouTube stream: {e} [attempt {self.fail_count}] - retrying")
                    stop.wait(min(10 * self.fail_count, 60))
                    continue
                quiet_until = 0.0
            if cfg.selftest:
                src = ["ffmpeg", "-loglevel", "error", "-re",
                       "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                       "-f", "lavfi", "-i", "sine=frequency=330",
                       "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-f", "mpegts", "pipe:1"]
            elif self.platform == "direct":
                net = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"] if self.url.startswith("http") else []
                src = ["ffmpeg", "-loglevel", "error", *(["-re"] if Path(self.url).is_file() else []), *net,
                       "-i", self.url, "-c", "copy", "-f", "mpegts", "pipe:1"]
            elif self.platform == "youtube":
                src = hls_source(media, hdrs, page_url=self.url, quality=cfg.quality)
            elif cfg.recorder == "streamlink":
                src = ["streamlink", "--stdout", "--retry-streams", "20", "--retry-open", "3",
                       self.url, cfg.quality]
            else:
                src = [*(ytdlp_cmd() or ["yt-dlp"]), "-q", *js_runtime_args()[0], "--no-part", "--wait-for-video", "20", "-f", "b", "-o", "-", self.url]
            seg = ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
                   "-f", "segment", "-segment_time", str(SEG), "-segment_format", "mpegts",
                   str(self.buf / f"{self.session:04d}_%06d.ts")]
            if self.platform != "youtube" or cfg.selftest:
                log(self.name, "waiting for stream..." if not cfg.selftest else "starting test stream")
            errf = self.buf / f"recorder_{self.session:04d}.err"
            errh = open(errf, "wb")
            p1 = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=errh)
            p2 = subprocess.Popen(seg, stdin=p1.stdout, stderr=subprocess.DEVNULL)
            p1.stdout.close()
            self.procs = [p1, p2]
            while p2.poll() is None and not stop.is_set() and not self.terminal:
                if not self.recording and len(list(self.buf.glob(f"{self.session:04d}_*.ts"))) > 1:
                    self.recording = True
                    log(self.name, "LIVE - recording")
                stop.wait(1)
            self.kill()
            errh.close()
            if self.terminal or stop.is_set():
                self.recording = False
                if self.terminal:
                    return
                break
            if self.recording:
                log(self.name, "stream ended / dropped, will retry")
                self.fail_count = 0
            else:
                try:
                    err = Path(errf).read_text(errors="replace")[-600:]
                except OSError:
                    err = ""
                rc = p1.poll()
                kind = recorder_failure(err, rc)
                last = (err.strip().splitlines() or [""])[-1][:200]
                if kind == "blocked":
                    return self.give_up("blocked", f"recorder was refused access ({last or 'access check'})")
                if kind == "offline":
                    log(self.name, "no live stream found yet (stream offline?), will check again")
                else:
                    self.fail_count += 1
                    log(self.name, f"recorder exited without video (exit {rc}): {last or 'no error text'} "
                                   f"[attempt {self.fail_count}] - retrying")
            self.recording = False
            stop.wait(5 if self.recording or not self.fail_count else min(10 * self.fail_count, 60))

    def kill(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()

    def segments(self):
        """Finished segments as (path, wall_start, wall_end)."""
        out = []
        for f in sorted(self.buf.glob("*.ts"))[:-1]:      # newest is still being written
            try:
                end = f.stat().st_mtime
            except FileNotFoundError:
                continue
            start = end - SEG
            if out and out[-1][0].name[:4] == f.name[:4] and 0 < end - out[-1][2] < SEG * 4:
                start = out[-1][2]
            out.append((f, start, end))
        return out

    # --- chat: anonymous IRC, no login
    def chat_loop(self):
        if self.cfg.selftest:
            return self.chat_sim()
        if self.platform == "direct":
            return          # no chat for a direct feed: clips are triggered by audio level instead
        if self.platform == "youtube":
            return self.yt_chat()
        backoff = 2
        while not stop.is_set():
            try:
                raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=20)
                s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
                s.settimeout(330)
                s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{self.name}\r\n".encode())
                log(self.name, "chat connected (anonymous)")
                backoff, buf = 2, b""
                while not stop.is_set():
                    data = s.recv(8192)
                    if not data:
                        raise ConnectionError("closed by server")
                    *lines, buf = (buf + data).split(b"\r\n")
                    for line in lines:
                        line = line.decode("utf-8", "replace")
                        if line.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                            continue
                        m = PRIV.match(line)
                        if m:
                            with self.lock:
                                self.msgs.append((time.time(), m[1], m[2]))
            except Exception as e:
                if stop.is_set():
                    break
                log(self.name, f"chat reconnecting ({e})")
                stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def yt_chat(self):
        backoff = 5
        while not stop.is_set():
            try:
                vid, key, ver, cont = yt_initial(http(self.url))
                api = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key}&prettyPrint=false"
                ctx = {"client": {"clientName": "WEB", "clientVersion": ver}}
                log(self.name, f"chat connected (YouTube video {vid}, no login)")
                backoff = 5
                while not stop.is_set():
                    lc = (json.loads(http(api, {"context": ctx, "continuation": cont}))
                          .get("continuationContents") or {}).get("liveChatContinuation")
                    if not lc:
                        raise ConnectionError("chat ended")
                    msgs, cont, wait = yt_parse(lc, time.time())
                    if not cont:
                        raise ConnectionError("chat closed")
                    with self.lock:
                        self.msgs.extend(msgs)
                    stop.wait(min(max(int(wait) / 1000, 1), 5))
            except Exception as e:
                if stop.is_set():
                    break
                if isinstance(e, AccessBlocked):
                    return self.give_up("blocked", str(e))
                elif isinstance(e, StateUnknown):
                    log(self.name, f"unknown stream state: {e}")
                elif not isinstance(e, LookupError) or cfg_verbose(self.cfg):
                    log(self.name, f"chat retrying ({e})")
                stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def chat_sim(self):
        t0 = time.time()
        while not stop.is_set():
            burst = 45 < time.time() - t0 < 53
            with self.lock:
                for _ in range(8 if burst else 1):
                    self.msgs.append((time.time(), f"user{random.randint(1, 400)}",
                                      random.choice(["OMG", "CLIP IT", "no way!!", "LUL"]) if burst else "hello"))
            stop.wait(0.2 if burst else 0.5)

    # --- spike detection + clip scheduling
    def tick_loop(self):
        cfg = self.cfg
        next_win, n = time.time() + cfg.window, 0
        while not stop.wait(1):
            try:
                now = time.time()
                if now < next_win:
                    self.flush_pending()
                    continue
                next_win, n = now + cfg.window, n + 1
                w0 = now - cfg.window
                if self.platform == "direct":
                    self.audio_tick(now, w0, n)
                    continue
                with self.lock:
                    while self.msgs and self.msgs[0][0] < now - 300:
                        self.msgs.popleft()
                    win = [m for m in self.msgs if m[0] >= w0]
                count, uniq = len(win), len({m[1] for m in win})
                hype = sum(1 for m in win if cfg.hype.search(m[2]))
                eff = min(count, uniq * 3) + cfg.hype_weight * hype   # spam-resistant, hype-boosted
                base = statistics.fmean(self.hist) if self.hist else 0.0
                fired = False
                if len(self.hist) >= cfg.warmup:
                    thresh = max(cfg.min_msgs, base * cfg.ratio, base + cfg.z * statistics.pstdev(self.hist))
                    if eff >= thresh:
                        fired = True
                        self.hist.append(thresh)   # capped, so a raid doesn't trigger forever
                        pre = cfg.pre + (cfg.yt_pre if self.platform == "youtube" else 0)
                        self.trigger(w0 - pre, now + cfg.post, eff / max(base, 1.0), "chat spike", count, base)
                if not fired:
                    self.hist.append(eff)
                if cfg.audio_trigger:     # also clip on a loudness jump, e.g. when chat is quiet or unavailable
                    recent = [f for f, st, en in self.segments() if en > w0]
                    db = window_loudness(recent) if recent else None
                    if audio_decision(fired, db, self.ahist, cfg):
                        fired = True
                        self.trigger(w0 - cfg.pre, now + cfg.post, max(db - statistics.fmean(self.ahist), 1.0),
                                     "audio spike", count, base)
                self.flush_pending()
                self.cleanup(now)
                if cfg.verbose or n % 6 == 0:
                    state = "REC" if self.recording else ("BLOCKED" if self.terminal == "blocked" else "FAILED" if self.terminal else "offline")
                    log(self.name, f"{state} | chat {count}/{cfg.window}s (baseline {base:.0f})")
            except Exception as e:
                log(self.name, f"tick error: {e}")

    def audio_tick(self, now, w0, n):
        """Direct feeds have no chat: a window much louder than the recent baseline is the 'spike'."""
        cfg = self.cfg
        recent = [f for f, st, en in self.segments() if en > w0]
        db = window_loudness(recent) if recent else None
        if db is not None:
            if audio_spike(self.hist, db, cfg):
                self.hist.append(self.hist[-1] if self.hist else db)
                self.trigger(w0 - cfg.pre, now + cfg.post, max(db - statistics.fmean(self.hist), 1.0),
                             "audio spike", 0, statistics.fmean(self.hist))
            else:
                self.hist.append(db)
        self.flush_pending()
        self.cleanup(now)
        if cfg.verbose or n % 6 == 0:
            state = "REC" if self.recording else ("FAILED" if self.terminal else "offline")
            log(self.name, f"{state} | audio {('%.1f dB' % db) if db is not None else 'n/a'} (baseline {statistics.fmean(self.hist) if self.hist else 0:.1f} dB)")

    def trigger(self, start, end, score, reason, count=0, base=0.0):
        cfg, now, p = self.cfg, time.time(), self.pending
        if not self.recording:
            return log(self.name, f"{reason} but not recording yet - skipped")
        if p:   # hype is still going: extend the clip instead of making a second one
            p["end"] = min(p["start"] + cfg.max_len, max(p["end"], end))
            p["score"] = max(p["score"], score)
            return
        if reason != "manual" and now - self.last_clip < cfg.cooldown:
            return
        self.pending = dict(start=start, end=end, score=score, reason=reason, count=count, base=base)
        log(self.name, f">>> {reason.upper()} x{score:.1f} ({count} msgs vs baseline {base:.0f}) - clipping")

    def manual(self):
        now = time.time()
        self.trigger(now - self.cfg.pre - self.cfg.window, now + self.cfg.post, 99.0, "manual")

    def flush_pending(self):
        p = self.pending
        if not p or time.time() < p["end"] + SEG * 2 + 1:
            return
        self.pending, self.last_clip = None, time.time()
        segs = [s for s in self.segments() if s[2] > p["start"] and s[1] < p["end"]]
        if not segs:
            return log(self.name, "no video in buffer for that moment - skipped")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(p["end"]))
        d = self.cfg.work / "jobs" / f"{self.name}_{stamp}"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "raw.ts", "wb") as o:      # MPEG-TS segments concatenate byte-for-byte
            for f, _, _ in segs:
                with open(f, "rb") as i:
                    shutil.copyfileobj(i, o)
        a, b = max(p["start"], segs[0][1]), min(p["end"], segs[-1][2])
        with self.lock:
            chat = [m[2] for m in self.msgs if p["start"] <= m[0] <= p["end"]]
        self.jobs.put(dict(p, dir=d, url=self.url, channel=self.name, platform=self.platform, stamp=stamp, off=a - segs[0][1], dur=b - a, chat=chat))

    def cleanup(self, now):
        for f, _, end in self.segments():
            if end < now - self.cfg.buffer:
                f.unlink(missing_ok=True)


# ----------------------------------------------------------------- captions

_model = None


def transcribe(path, cfg):
    """Returns [(start, end, word)] or [] if Whisper is unavailable."""
    global _model
    try:
        if _model is None:
            from faster_whisper import WhisperModel
            log("whisper", f"loading model '{cfg.model}' (first run downloads it)")
            _model = WhisperModel(cfg.model, device="auto", compute_type="int8")
        segs, _ = _model.transcribe(str(path), word_timestamps=True, vad_filter=True, language=cfg.lang)
        return [(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or []) if w.word.strip()]
    except ImportError:
        if _model is None:
            log("whisper", "faster-whisper not installed - clips will have no captions (pip install faster-whisper)")
            _model = False
    except Exception as e:
        log("whisper", f"caption error: {e}")
    return []


def ass_time(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(words, w, h, path, font="Arial", hook=None, handle=None, hype=None):
    """Short-form captions: 1-3 words at a time, the spoken word pops in bigger and in yellow
    (hot red-orange for hype words), heavy outline + shadow, sitting in the lower third. Hook on top."""
    vertical = h > w
    size = round(w * 0.095) if vertical else round(h * 0.09)
    margin = round(h * 0.24) if vertical else round(h * 0.08)
    ol = max(size // 9, 5)
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
            f"Style: cap,{font},{size},&H00FFFFFF,&H00000000,&H90000000,-1,1,{ol},{max(ol // 2, 3)},2,50,50,{margin}\n"
            f"Style: hook,{font},{round(size * 0.78)},&H00FFFFFF,&H00D77800,&H00000000,-1,3,{size // 6},0,8,70,70,"
            f"{round(h * (0.10 if vertical else 0.05))}\n"
            f"Style: handle,{font},{round(size * 0.4)},&H70FFFFFF,&H60000000,&H00000000,-1,1,2,0,2,40,40,"
            f"{round(h * 0.03)}\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Text\n")
    maxc = 15 if vertical else 28
    chunks, cur = [], []
    for wd in words:
        if cur and (len(cur) >= 3 or wd[0] - cur[-1][1] > 0.5 or sum(len(x[2]) + 1 for x in cur) + len(wd[2]) > maxc
                    or re.search(r"[.!?]$", cur[-1][2])):
            chunks.append(cur)
            cur = []
        cur.append(wd)
    if cur:
        chunks.append(cur)
    clean = lambda t: re.sub(r"[{}\\\n]", " ", t).strip()
    lines = [f"Dialogue: 1,0:00:00.00,9:59:59.00,{st},{clean(tx)}"
             for st, tx in (("hook", hook), ("handle", handle)) if tx]
    for k, ch in enumerate(chunks):
        nxt = chunks[k + 1][0][0] if k + 1 < len(chunks) else 1e9
        for i, (s_, e, _) in enumerate(ch):
            end = ch[i + 1][0] if i + 1 < len(ch) else min(e + 0.25, nxt)
            parts = []
            for j, x in enumerate(ch):
                t = re.sub(r"[{}\\]", "", x[2]).strip(" ,;:").upper()
                if j == i:
                    hot = bool(hype and hype.search(x[2])) or x[2].endswith(("!", "?"))
                    col = "&H3C3CFF&" if hot else "&H00E6FF&"
                    parts.append("{\\c%s\\fscx82\\fscy82\\t(0,110,\\fscx112\\fscy112)}%s{\\r}" % (col, t))
                else:
                    parts.append(t)
            lines.append(f"Dialogue: 0,{ass_time(s_)},{ass_time(max(end, s_ + 0.05))},cap,{' '.join(parts)}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- render

LAYOUTS = ("blur", "crop", "none")


def layout_graph(layout, dur):
    """Filter graph ending at [pre]. blur: blurred fill + slow punch-in on the main video + progress bar."""
    D = f"{max(dur, 0.1):.2f}"
    if layout == "blur":
        return ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "boxblur=24:4,eq=brightness=-0.06[bg];"
                f"[b]scale=w='trunc(1080*(1+0.06*t/{D})/2)*2':h=-2:eval=frame[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2:eval=frame[m];"
                f"color=c=0xFFD400:s=1080x12:d={D},format=yuv420p[bar];"
                f"[m][bar]overlay=x='-w+w*t/{D}':y=H-h[pre]")
    if layout == "crop":
        return (f"[0:v]scale=-2:1920,crop=1080:1920[m];color=c=0xFFD400:s=1080x12:d={D},format=yuv420p[bar];"
                f"[m][bar]overlay=x='-w+w*t/{D}':y=H-h[pre]")
    return "[0:v]null[pre]"


def mean_volume(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else None


def probe_size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def make_title(channel, hook, caption, transcript, top_chat):
    """Best available short title (<=95 chars): hook, else caption, else transcript start, else chat."""
    for t in (hook, caption, transcript[:80] if transcript else "", ""):
        t = re.sub(r"\s+", " ", (t or "")).strip()
        if t:
            return (t[:92] + "...") if len(t) > 95 else t
    return f"{channel} live moment"


def upload_pack(job, meta, cfg, source_url):
    """<base>_upload.json: ready-to-review title/description/tags. Nothing is uploaded by this tool."""
    title = make_title(job["channel"], meta.get("hook"), meta.get("caption"), meta.get("transcript", ""), meta.get("top_chat", []))
    tags = [t.lstrip("#") for t in (meta.get("hashtags") or "").split() if t.startswith("#")]
    tags += ["shorts", "live", "clip", job["channel"]]
    credit = f"Clip from {job['channel']}'s live stream: {source_url}"
    desc = "\n\n".join(x for x in [meta.get("caption") or "", credit, " ".join("#" + t for t in tags[:5])] if x)
    return dict(title=title, description=desc, tags=list(dict.fromkeys(tags)), privacy="private",
                video=meta.get("final") or meta.get("clean"), thumbnail=meta.get("thumbnail"),
                source=source_url, source_channel=job["channel"], rights_cleared=False,
                thumbnail_headline=(meta.get("thumbnail_info") or {}).get("text"),
                thumbnail_headline_source=(meta.get("thumbnail_info") or {}).get("source"),
                review_required=True, reviewed_title_thumbnail_hook=False)


def render(job, cfg):
    d = job["dir"]
    base = f'{job["channel"]}_{job["stamp"]}_x{job["score"]:.1f}'
    clean = cfg.out / f"{base}_clean.mp4"
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", "raw.ts", "-ss", f'{job["off"]:.2f}', "-t", f'{job["dur"]:.2f}',
         *enc, "-c:a", "aac", "-b:a", "160k", str(clean)], cwd=d)
    words = transcribe(clean, cfg) if cfg.captions else []
    top = [m for m, _ in collections.Counter(c.strip() for c in job["chat"]).most_common(8)]
    transcript = " ".join(w[2] for w in words)
    copy = ai_copy(job["channel"], transcript, top, cfg)
    hook = cfg.hook or copy.get("hook")
    dur = job["dur"]
    final = cfg.out / f"{base}_final.mp4"
    w, h = (1080, 1920) if cfg.layout != "none" else probe_size(clean)
    post = f"fade=t=in:st=0:d=0.15,fade=t=out:st={max(dur - 0.3, 0):.2f}:d=0.3"
    if words or hook or cfg.handle:
        build_ass(words, w, h, d / "caps.ass", cfg.font, hook, cfg.handle, cfg.hype)
        post += ",subtitles=caps.ass"
    mv = mean_volume(clean)
    af = f"afade=t=in:d=0.08,afade=t=out:st={max(dur - 0.3, 0):.2f}:d=0.3"
    if mv is not None and mv > -45:
        af = "loudnorm=I=-14:TP=-1.5:LRA=11," + af
    else:
        log(job["channel"], "audio near-silent: loudness normalisation skipped")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clean), "-filter_complex",
         layout_graph(cfg.layout, dur) + f";[pre]{post}[v]", "-map", "[v]", "-map", "0:a?", "-af", af, *enc,
         "-c:a", "aac", "-b:a", "160k", str(final)], cwd=d)
    thumb = None
    tinfo = {}
    if cfg.thumbnails:
        thumb = cfg.out / f"{base}_thumb.jpg"
        try:
            import thumbmaker
            tinfo = thumbmaker.make_thumbnail(clean, thumb, hook=hook, transcript=transcript, top_chat=top,
                                              channel=job["channel"], handle=cfg.handle)
        except Exception as e:
            log(job["channel"], f"thumbnail failed: {e}")
            thumb = None
    if copy:
        (cfg.out / f"{base}_caption.txt").write_text(f'{copy.get("caption", "")}\n\n{copy.get("hashtags", "")}\n')
    meta = dict(channel=job["channel"], platform=job["platform"], hook=hook, caption=copy.get("caption"),
                hashtags=copy.get("hashtags"), time=job["stamp"], score=round(job["score"], 2), trigger=job["reason"],
                chat_msgs=job["count"], chat_baseline=round(job["base"], 1), seconds=round(job["dur"], 1),
                transcript=transcript, top_chat=top,
                thumbnail_info=tinfo, clean=clean.name, final=final.name, thumbnail=thumb.name if thumb else None)
    (cfg.out / f"{base}.json").write_text(json.dumps(meta, indent=2))
    src = job.get("url") or job["channel"]
    (cfg.out / f"{base}_upload.json").write_text(json.dumps(upload_pack(job, meta, cfg, src), indent=2))
    shutil.rmtree(d, ignore_errors=True)
    return meta


_ai_warned = False


def ai_copy(channel, transcript, top_chat, cfg):
    """Optional: Claude writes the on-screen hook, post caption and hashtags. Needs ANTHROPIC_API_KEY."""
    global _ai_warned
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not cfg.ai:
        return {}
    if not key:
        if not _ai_warned:
            log("ai", "--ai needs ANTHROPIC_API_KEY set - skipping hooks/captions")
            _ai_warned = True
        return {}
    prompt = (f"A moment from the live stream '{channel}' just made chat explode.\n"
              f"What was said (auto transcript): {transcript[:1500] or '(no speech detected)'}\n"
              f"Most common chat messages: {top_chat}\n\n"
              "Write copy for a short vertical clip. Only use what the transcript and chat support; never invent events. "
              'Reply with JSON only: {"hook": "on-screen hook, max 8 words, no emojis", '
              '"caption": "1-2 sentence post caption", "hashtags": "8 hashtags in one string"}')
    try:
        r = json.loads(http("https://api.anthropic.com/v1/messages",
                            {"model": cfg.ai_model, "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
                            {"x-api-key": key, "anthropic-version": "2023-06-01"}))
        return json.loads(re.search(r"\{.*\}", r["content"][0]["text"], re.S)[0])
    except Exception as e:
        log("ai", f"hook/caption failed: {e}")
        return {}


def montage(cfg):
    """Best N clips of the session, in time order, joined into one video."""
    metas = [json.loads(p.read_text()) for p in [q for q in cfg.out.glob("*.json") if not q.name.endswith("_upload.json")]]
    top = sorted(sorted((m for m in metas if m.get("final")), key=lambda m: -m["score"])[:cfg.montage],
                 key=lambda m: m["time"])
    if len(top) < 2:
        return log("main", "montage needs at least 2 clips - skipped")
    W, H = (1920, 1080) if cfg.layout == "none" else (1080, 1920)
    n, inputs = len(top), [x for m in top for x in ("-i", str(cfg.out / m["final"]))]
    fc = "".join(f"[{i}:v]fps=30,scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                 f"setsar=1[v{i}];[{i}:a]aresample=48000[a{i}];" for i in range(n))
    fc += "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    out = cfg.out / f"montage_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-movflags", "+faststart", str(out)])
    log("main", f"MONTAGE of top {n} clips -> {out}")


def write_index(cfg):
    """clips/index.html - every clip, best first."""
    metas = sorted((json.loads(p.read_text()) for p in [q for q in cfg.out.glob("*.json") if not q.name.endswith("_upload.json")]), key=lambda m: -m["score"])
    cards = "".join(
        f'<div class="c"><video controls preload="metadata" src="{html.escape(m["final"] or m["clean"])}"></video>'
        f'<b>x{m["score"]} &middot; {html.escape(m["channel"])} &middot; {html.escape(m["time"])}</b>'
        f'<p>{html.escape(m.get("caption") or m["transcript"][:220])}</p><small>{html.escape(" | ".join(m["top_chat"][:5]))}</small></div>'
        for m in metas)
    (cfg.out / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>liveclipper</title><style>body{font:14px system-ui;background:#111;color:#eee;margin:16px}"
        ".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}"
        ".c{background:#1c1c1c;border-radius:10px;padding:10px}video{width:100%;border-radius:6px}"
        "p{margin:6px 0;color:#bbb}small{color:#888}</style>"
        f"<h2>Clips, best first ({len(metas)})</h2><div class=g>{cards}</div>")


def notify(cfg, text):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification {json.dumps(text)} with title "liveclipper"'],
                       capture_output=True)
    if cfg.webhook:   # Discord-style webhook: get pinged on your phone
        try:
            req = urllib.request.Request(cfg.webhook, json.dumps({"content": text}).encode(),
                                         {"Content-Type": "application/json", "User-Agent": "liveclipper"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log("notify", f"webhook failed: {e}")


def render_worker(jobs, cfg):
    while True:
        job = jobs.get()
        if job is None:
            return
        try:
            t = time.time()
            meta = render(job, cfg)
            write_index(cfg)
            log(job["channel"], f"CLIP READY in {time.time() - t:.0f}s -> {cfg.out / (meta['final'] or meta['clean'])}")
            notify(cfg, f"Clip ready: {job['channel']} x{meta['score']} ({meta['seconds']}s)")
        except Exception as e:
            log(job["channel"], f"render failed: {e}")
        if cfg.selftest:
            stop.set()


# ----------------------------------------------------------------- main

def simulate(cfg):
    """Offline threshold check: same baseline/ratio/z/min-msgs/warmup rules, fed from a file."""
    hist = collections.deque(maxlen=cfg.history)
    last = -1e9
    for line in Path(cfg.simulate_chat).read_text().splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("#"):
            continue
        t, count = float(parts[0]), float(parts[1])
        fired = False
        if len(hist) >= cfg.warmup:
            base = statistics.fmean(hist)
            thresh = max(cfg.min_msgs, base * cfg.ratio, base + cfg.z * statistics.pstdev(hist))
            if count >= thresh and t - last >= cfg.cooldown:
                print(f"spike at {t:.0f}s: {count:.0f} msgs (threshold {thresh:.0f}, baseline {base:.0f}) - clip")
                hist.append(thresh); last = t; fired = True
        if not fired:
            hist.append(count)
    print("done")


def check(cfg):
    """--check: open each source the way a run would, record 6s, read chat once, and say exactly what's wrong."""
    ytd = ytdlp_cmd()
    ver = subprocess.run([*ytd, "--version"], capture_output=True, text=True).stdout.strip() if ytd else "NOT INSTALLED"
    print(f"yt-dlp:     {ver} ({' '.join(ytd[:3]) if ytd else '-'})")
    print(f"streamlink: {'yes' if shutil.which('streamlink') else 'no'}   ffmpeg: {'yes' if shutil.which('ffmpeg') else 'NO'}")
    warn = js_runtime_args()[1]
    print(f"YouTube JS: {'OK' if not warn else 'PROBLEM - ' + warn}")
    print(f"cookies:    {cfg.cookies or 'none'}\n")
    for t in cfg.channels:
        plat, url, name = parse_target(t)
        print(f"== {name} ({plat}) {url}")
        try:
            if plat == "youtube":
                media, hdrs = resolve_stream(url, cfg)
                src = None
            elif plat == "direct":
                src = ["ffmpeg", "-loglevel", "error", "-i", url, "-c", "copy", "-f", "mpegts", "pipe:1"]
            else:
                src = ["streamlink", "--stdout", url, cfg.quality]
            print("   stream:  found, test-recording...")
            tmp = cfg.work / f"check_{name}.ts"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            modes = [0, 1, 2, 3] if plat == "youtube" else [None]
            good = None
            for m in modes:
                s_ = hls_source(media, hdrs, mode=m, page_url=url, quality=cfg.quality) if plat == "youtube" else src
                if plat == "youtube":
                    print(f"   try yt-source {m} ({'streamlink' if m == 3 else 'ffmpeg'}):", end=" ", flush=True)
                if s_[0] == "streamlink" and not shutil.which("streamlink"):
                    print("streamlink not installed"); continue
                p = subprocess.Popen(s_, stdout=open(tmp, "wb"), stderr=subprocess.PIPE)
                time.sleep(12)
                p.terminate()
                err = p.stderr.read().decode(errors="replace").strip()
                size = tmp.stat().st_size if tmp.exists() else 0
                info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                       "-of", "csv=p=0", str(tmp)], capture_output=True, text=True).stdout.split()
                if size > 100_000:
                    print(f"video:   OK ({size // 1024} KB recorded; {', '.join(dict.fromkeys(info))})")
                    good = m
                    break
                first = err.splitlines()[:6]
                print(f"video:   FAILED (only {size} bytes)")
                for l in first:
                    print("      ", l[:160])
            if plat == "youtube" and good is not None:
                print(f"   -> works with: --yt-source {good}")
            tmp.unlink(missing_ok=True)
        except AccessBlocked as e:
            print(f"   video:   BLOCKED - {e}\n            fix: add --cookies chrome (or safari/firefox/edge)")
        except LookupError as e:
            print(f"   video:   NOT LIVE - {e}")
        except Exception as e:
            print(f"   video:   FAILED - {e}")
        if plat == "youtube":
            try:
                vid, key, ver_, cont = yt_initial(http(url))
                print(f"   chat:    OK (video {vid})")
            except Exception as e:
                print(f"   chat:    {type(e).__name__} - {e}  (clips can still trigger with --audio-trigger)")
        elif plat == "twitch":
            print("   chat:    anonymous IRC (checked during a real run)")
        print()


def main():
    ap = argparse.ArgumentParser(description="Clip Twitch streams live, triggered by chat spikes.")
    ap.add_argument("channels", nargs="*", metavar="TARGET",
                    help="twitch channel name, @youtubehandle, or a YouTube live URL (one or many, mixed)")
    ap.add_argument("--quality", default="best", help="streamlink quality, e.g. 720p60,best")
    ap.add_argument("--recorder", choices=["auto", "streamlink", "yt-dlp"], default="auto",
                    help="yt-dlp is the fallback if streamlink can't open a YouTube stream")
    ap.add_argument("--yt-source", type=int, choices=[-1,0,1,2,3], default=-1,
                    help="YouTube capture method: -1 auto (streamlink if installed, else 0), 0 ffmpeg+UA header, 1 ffmpeg no headers, 2 ffmpeg all headers, 3 streamlink (run --check to find the one that works)")
    ap.add_argument("--yt-pre", type=int, default=5, help="extra pre-roll seconds for YouTube (higher latency)")
    ap.add_argument("--keywords", default="", help="extra hype words for your niche, comma-separated: goal,clutch,ace")
    ap.add_argument("--hook", default=None, help="text burned at the top of every clip")
    ap.add_argument("--handle", default=None, help="your @handle as a small watermark")
    ap.add_argument("--ai", action="store_true",
                    help="Claude writes the hook, caption and hashtags per clip (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--ai-model", default="claude-haiku-5-5")
    ap.add_argument("--montage", type=int, default=0, metavar="N", help="on quit, join the top N clips into one video")
    ap.add_argument("--out", type=Path, default=Path("clips"))
    ap.add_argument("--work", type=Path, default=Path(".liveclipper"))
    ap.add_argument("--window", type=int, default=10, help="chat counting window, seconds")
    ap.add_argument("--pre", type=int, default=15, help="seconds kept BEFORE the spike window")
    ap.add_argument("--post", type=int, default=5, help="seconds kept after the spike")
    ap.add_argument("--max-len", type=int, default=60, help="max clip length if hype keeps going")
    ap.add_argument("--cooldown", type=int, default=45, help="min seconds between clips per channel")
    ap.add_argument("--ratio", type=float, default=2.5, help="spike = chat at least this many times baseline")
    ap.add_argument("--z", type=float, default=3.0, help="...and this many std-devs above baseline")
    ap.add_argument("--min-msgs", type=int, default=15, help="ignore spikes smaller than this (quiet chats)")
    ap.add_argument("--hype-weight", type=float, default=0.5, help="extra weight for hype messages (CLIP, OMG, KEKW...)")
    ap.add_argument("--history", type=int, default=30, help="baseline = last N windows")
    ap.add_argument("--warmup", type=int, default=6, help="windows to observe before the first clip")
    ap.add_argument("--buffer", type=int, default=180, help="seconds of video kept on disk")
    ap.add_argument("--layout", choices=LAYOUTS, default="blur", help="blur/crop = 9:16 vertical, none = original")
    ap.add_argument("--no-captions", dest="captions", action="store_false")
    ap.add_argument("--model", default="small", help="Whisper model: tiny, base, small, medium, large-v3")
    ap.add_argument("--lang", default=None, help="force caption language, e.g. en, hi")
    ap.add_argument("--font", default="Arial")
    ap.add_argument("--webhook", default=None, help="Discord webhook URL for 'clip ready' pings")
    ap.add_argument("--verbose", action="store_true", help="print chat rate every window")
    ap.add_argument("--thumbnails", action="store_true", help="also make a designed 1280x720 thumbnail per clip (best frame + headline)")
    ap.add_argument("--duration", type=int, default=0, help="stop after N seconds (0 = run until quit)")
    ap.add_argument("--simulate-chat", metavar="FILE", default=None, help="offline: file of 'offset_seconds message_count' lines; prints which windows would spike")
    ap.add_argument("--audio-db", type=float, default=8.0, help="direct feeds: spike = this many dB louder than the baseline")
    ap.add_argument("--audio-trigger", action="store_true", help="also clip when the audio jumps --audio-db louder (chat streams)")
    ap.add_argument("--cookies", default=None, metavar="BROWSER",
                    help="YouTube asks you to sign in? use your browser's login: chrome, safari, firefox, edge, brave")
    ap.add_argument("--check", action="store_true", help="diagnose the given sources (video + chat) and exit")
    ap.add_argument("--selftest", action="store_true", help="fake stream + fake chat spike, makes one clip, exits")
    ap.add_argument("--vod", metavar="SOURCE", default=None,
                    help="clip a finished video instead of a live stream: local file or Twitch VOD URL (see vodclip.py)")
    ap.add_argument("--top", type=int, default=10, help="with --vod: how many top-scored clips to render")
    ap.add_argument("--cam", default=None, help="with --vod: webcam rectangle x,y,w,h (skips auto-detect)")
    ap.add_argument("--vod-layout", choices=["stack", "blur", "crop"], default="stack",
                    help="with --vod: stack = gameplay on top, webcam below (falls back to blur if no cam)")
    cfg = ap.parse_args()
    global YT_SOURCE
    YT_SOURCE = cfg.yt_source

    if cfg.vod:
        import vodclip
        extra = ["--cam", cfg.cam] if cfg.cam else []
        return vodclip.main([cfg.vod, "--top", str(cfg.top), "--layout", cfg.vod_layout, "--model", cfg.model,
                             "--out", "clips_vod"] + extra + ([] if cfg.captions else ["--no-captions"]))
    if cfg.simulate_chat:
        return simulate(cfg)
    if cfg.check:
        cfg.work = cfg.work.resolve()
        return check(cfg)
    if cfg.selftest:
        cfg.channels, cfg.warmup, cfg.pre, cfg.verbose = ["selftest"], 3, 10, True
    if not cfg.channels and not cfg.simulate_chat:
        ap.error("give at least one channel (or --selftest)")
    if cfg.recorder == "auto":
        cfg.recorder = "streamlink" if shutil.which("streamlink") or not shutil.which("yt-dlp") else "yt-dlp"
    kw = "".join("|" + re.escape(k.strip()) for k in cfg.keywords.split(",") if k.strip())
    cfg.hype = re.compile(HYPE % kw, re.I)
    plats = {parse_target(c)[0] for c in cfg.channels} if not cfg.selftest else set()
    need = ["ffmpeg", "ffprobe"] + (["streamlink"] if "twitch" in plats and cfg.recorder == "streamlink" else [])
    missing = [b for b in need if not shutil.which(b)]
    if "youtube" in plats and not ytdlp_cmd():
        missing.append("yt-dlp")
    if missing:
        sys.exit(f"Missing: {', '.join(missing)}. Mac: brew install ffmpeg streamlink && "
                 'pip3 install -U "yt-dlp[default,deno]"')
    cfg.out, cfg.work = cfg.out.resolve(), cfg.work.resolve()
    cfg.out.mkdir(parents=True, exist_ok=True)

    jobs = queue.Queue()
    worker = threading.Thread(target=render_worker, args=(jobs, cfg))
    worker.start()
    chans = [Channel(c, cfg, jobs) for c in cfg.channels]
    if any(c.platform == "youtube" for c in chans) or (cfg.recorder == "yt-dlp" and any(c.platform != "direct" for c in chans)):
        warn = js_runtime_args()[1]
        if warn:
            log("main", f"YouTube setup: {warn}")
    for c in chans:
        c.start()
    log("main", f"watching {', '.join(c.name for c in chans)} | Enter = clip now, q = quit | clips -> {cfg.out}")

    def keys():
        for line in sys.stdin:
            if line.strip().lower() == "q":
                return stop.set()
            for c in chans:
                c.manual()
    if sys.stdin.isatty():
        threading.Thread(target=keys, daemon=True).start()
    try:
        t_start = time.time()
        while not stop.wait(0.5):
            if cfg.duration and time.time() - t_start >= cfg.duration:
                break
            if chans and all(c.terminal for c in chans):
                log("main", "every source is blocked or failed; stopping. See the messages above.")
                break
    except KeyboardInterrupt:
        pass
    stop.set()
    log("main", "stopping, finishing any clips in progress...")
    for c in chans:
        c.kill()
    jobs.put(None)
    worker.join()
    if cfg.montage:
        try:
            montage(cfg)
        except Exception as e:
            log("main", f"montage failed: {e}")
    shutil.rmtree(cfg.work, ignore_errors=True)
    if chans and all(c.terminal for c in chans):
        sys.exit(2)


if __name__ == "__main__":
    main()
