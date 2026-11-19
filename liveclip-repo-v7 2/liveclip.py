#!/usr/bin/env python3
"""liveclip — clip a live Twitch or YouTube stream the moment chat explodes.

The system from the viral reel, as a real tool:

1. Streamlink records the stream live, from the very first second.
2. Chat is counted every 10 seconds — Twitch via anonymous IRC (no login),
   YouTube via chat-downloader (no login, no API key).
3. When chat spikes (a "chat explodes" moment), FFmpeg cuts a 30-second
   clip around that moment — no waiting for the VOD, no overnight download.
4. Whisper adds captions to the clip when it is installed.
5. Optional: a vertical 9:16 export for Shorts/Reels/TikTok, thumbnails,
   and a chat-activity log for tuning.
6. Clips are ready before the stream ends. Free. Only works live.

Usage:
    python3 liveclip.py <twitch-channel>
    python3 liveclip.py https://www.youtube.com/watch?v=VIDEO_ID --vertical
    python3 liveclip.py shroud --clip-length 30 --min-spike 25
    python3 liveclip.py --simulate-chat chat-sample.txt

Requirements: streamlink and ffmpeg on PATH. YouTube chat also needs
chat-downloader (pip install chat-downloader). Whisper (openai-whisper)
is optional — captions are skipped with a warning when it is missing.
No login is needed for public streams and public chat; if a stream
refuses anonymous access, set TWITCH_OAUTH_TOKEN and the recorder will
use it.
"""

import argparse
import csv
import json
import os
import queue
import random
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime

VERSION = "0.2.0"

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6667

# ---------------------------------------------------------------------------
# Platform handling
# ---------------------------------------------------------------------------

_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_WATCH_ID = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")


def detect_platform(target, platform_arg="auto"):
    """Return "twitch" or "youtube" for a target string."""
    if platform_arg in ("twitch", "youtube"):
        return platform_arg
    if ("youtube.com" in target or "youtu.be" in target
            or target.startswith("@")
            or _YOUTUBE_VIDEO_ID.match(target)):
        return "youtube"
    return "twitch"


def youtube_url(target):
    """Build a full YouTube URL from a video ID, handle, or URL."""
    if target.startswith("http"):
        return target
    if _YOUTUBE_VIDEO_ID.match(target):
        return "https://www.youtube.com/watch?v=" + target
    return "https://www.youtube.com/" + target.lstrip("/")


def youtube_label(target):
    """Short label for file names: the video ID when known, else youtube."""
    m = _YOUTUBE_WATCH_ID.search(target)
    if m:
        return m.group(1)
    if _YOUTUBE_VIDEO_ID.match(target):
        return target
    return "youtube"


def stream_url(platform, target):
    """The URL streamlink records from."""
    if platform == "youtube":
        return youtube_url(target)
    return "twitch.tv/" + target


# ---------------------------------------------------------------------------
# Pure logic (unit-tested in test_liveclip.py)
# ---------------------------------------------------------------------------

_IRC_PRIVMSG = re.compile(r"^:([^!]+)!.* PRIVMSG #(\S+) :(.*)$")
_IRC_PING = re.compile(r"^PING :(.*)$")


def parse_irc_line(line):
    """Parse one raw IRC line.

    Returns ("msg", user, channel, text) for chat messages,
    ("ping", token) for server PINGs, or None for anything else.
    """
    line = line.rstrip("\r\n")
    m = _IRC_PING.match(line)
    if m:
        return ("ping", m.group(1))
    m = _IRC_PRIVMSG.match(line)
    if m:
        return ("msg", m.group(1), m.group(2), m.group(3))
    return None


def count_youtube_chat_line(line):
    """Count chat messages in one chat-downloader output line.

    chat-downloader prints one JSON object per chat message (with an
    "author" field). Returns 0 for log lines, blanks, and non-chat JSON.
    Handles a JSON list as well, just in case.
    """
    line = line.strip()
    if not line:
        return 0
    try:
        obj = json.loads(line)
    except ValueError:
        return 0
    if isinstance(obj, list):
        return sum(1 for item in obj
                   if isinstance(item, dict) and "author" in item)
    if isinstance(obj, dict) and "author" in obj:
        return 1
    return 0


def clip_window(spike_t, pre_roll, clip_len, recorded_until):
    """Return (start, end) stream-seconds for a clip around a spike.

    The clip covers pre_roll seconds before the spike and the rest after
    it. Returns None when the recording has not yet covered the end of
    the clip (caller should retry later), or when the spike is entirely
    outside what has been recorded.
    """
    if spike_t < 0 or recorded_until <= 0:
        return None
    start = max(0.0, spike_t - pre_roll)
    end = start + clip_len
    if end > recorded_until:
        return None
    return (start, end)


def format_clip_name(label, spike_t, chat_count):
    """Human-readable clip file name, e.g. clip_shroud_014235_chat312.mp4."""
    stamp = datetime.now().strftime("%H%M%S")
    return "clip_{}_{}_chat{}.mp4".format(label, stamp, chat_count)


def simulate_chat_lines(lines, window_seconds=10, min_spike=25,
                        multiplier=3.0, cooldown_seconds=60):
    """Replay a chat log offline and report which windows would spike.

    `lines` are "offset_seconds count" pairs (one window's message count
    per line, e.g. "120 312"). Returns [(spike_time, count)] for every
    window the detector would have clipped. Used by --simulate-chat and
    for tuning thresholds before going live.
    """
    detector = SpikeDetector(window_seconds=window_seconds,
                             min_spike=min_spike,
                             multiplier=multiplier,
                             cooldown_seconds=cooldown_seconds)
    events = []
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            events.append((float(parts[0]), int(parts[1])))
        except ValueError:
            continue
    events.sort()
    spikes = []
    last_t = 0.0
    for t, count in events:
        for _ in range(count):
            detector.note_message(t)
        last_t = t
        for hit in detector.poll_spikes(t + window_seconds):
            spikes.append((round(t, 1), hit))
    for hit in detector.poll_spikes(last_t + window_seconds):
        spikes.append((round(last_t, 1), hit))
    return spikes


class SpikeDetector:
    """Detects chat explosions over fixed windows.

    A window is a spike when its message count beats both an absolute
    floor (min_spike) and a multiple of the recent baseline (the median
    of the previous `baseline_windows` windows — the window being judged
    never raises its own bar). A cooldown prevents one moment from
    producing a burst of overlapping clips. Every finished window is
    recorded in `log` for tuning (windows.csv).
    """

    def __init__(self, window_seconds=10, min_spike=25, multiplier=3.0,
                 baseline_windows=12, cooldown_seconds=60):
        self.window_seconds = window_seconds
        self.min_spike = min_spike
        self.multiplier = multiplier
        self.baseline_windows = baseline_windows
        self.cooldown_seconds = cooldown_seconds
        self.counts = []          # finished window counts, oldest first
        self.log = []             # {"count","baseline","spike","clipped"}
        self._current = 0         # messages in the open window
        self._window_start = None
        self._last_spike_at = None
        self._spike_queue = queue.SimpleQueue()

    def _baseline(self):
        if not self.counts:
            return 0.0
        return statistics.median(self.counts)

    def _finish_window(self, now):
        count = self._current
        baseline = self._baseline()
        self.counts.append(count)
        if len(self.counts) > self.baseline_windows:
            self.counts.pop(0)
        threshold = max(self.min_spike, baseline * self.multiplier)
        is_spike = count >= threshold
        clipped = False
        if is_spike:
            if (self._last_spike_at is None
                    or now - self._last_spike_at >= self.cooldown_seconds):
                self._last_spike_at = now
                self._spike_queue.put(count)
                clipped = True
        self.log.append({"count": count,
                         "baseline": round(baseline, 1),
                         "spike": is_spike,
                         "clipped": clipped})
        self._current = 0
        self._window_start = now
        return count

    def note_message(self, now):
        """Record one chat message seen at monotonic time `now`."""
        if self._window_start is None:
            self._window_start = now
        while now - self._window_start >= self.window_seconds:
            self._finish_window(self._window_start + self.window_seconds)
        self._current += 1

    def _close_due_window(self, now):
        if (self._window_start is not None
                and now - self._window_start >= self.window_seconds):
            self._finish_window(now)

    def _drain_spikes(self):
        out = []
        while True:
            try:
                out.append(self._spike_queue.get_nowait())
            except queue.Empty:
                break
        return out

    def check(self, now):
        """Close the current window if due; return one spike count or None."""
        self._close_due_window(now)
        found = self._drain_spikes()
        return found[0] if found else None

    def poll_spikes(self, now):
        """Close the current window if due; return all pending spikes."""
        self._close_due_window(now)
        return self._drain_spikes()


# ---------------------------------------------------------------------------
# Recorder / cutter / captions / exports (subprocess wrappers)
# ---------------------------------------------------------------------------

def build_streamlink_cmd(url, outfile, quality="best"):
    """Streamlink command that records a stream URL live to `outfile`."""
    cmd = ["streamlink", "--twitch-disable-ads", "-o", outfile,
           url, quality]
    token = os.environ.get("TWITCH_OAUTH_TOKEN")
    if token:
        cmd[2:2] = ["--twitch-oauth-token", token]
    return cmd


def stream_is_live(url, quality="best", runner=subprocess.run):
    """True when streamlink can resolve playable streams for `url`.

    streamlink prints {"error": ...} (and exits 0) when nothing is live,
    so the JSON — not the exit code — decides.
    """
    try:
        result = runner(["streamlink", "--json", url, quality],
                        capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    try:
        info = json.loads(result.stdout or "{}")
    except ValueError:
        return False
    return "error" not in info


def build_chat_downloader_cmd(url):
    """chat-downloader command tailing a YouTube live chat as JSON lines."""
    return ["chat_downloader", url, "--format", "json",
            "--chat_type", "live", "--quiet"]


def build_cut_cmd(recording, start, end, outfile):
    """FFmpeg command cutting [start, end] out of the recording.

    Stream copy: fast, no re-encode, clip is ready in seconds.
    """
    return ["ffmpeg", "-y", "-ss", str(start), "-to", str(end),
            "-i", recording, "-c", "copy", outfile]


def build_vertical_cmd(clip, outfile):
    """FFmpeg command turning a 16:9 clip into a 9:16 vertical one.

    Blurred, upscaled copy of the frame fills the background; the real
    frame sits centred on top — the standard Shorts/Reels/TikTok look.
    """
    filt = ("[0:v]split[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=40[bg];"
            "[b]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2")
    return ["ffmpeg", "-y", "-i", clip, "-filter_complex", filt,
            "-c:a", "copy", outfile]


def build_thumbnail_cmd(clip, at_seconds, outfile):
    """FFmpeg command grabbing one still frame as the clip thumbnail."""
    return ["ffmpeg", "-y", "-ss", str(at_seconds), "-i", clip,
            "-vframes", "1", outfile]


def build_caption_cmds(clip, workdir):
    """Whisper + burn-in commands for `clip`, or None if whisper missing.

    Returns (whisper_cmd, burn_cmd, captioned_path) or None when the
    whisper CLI is not installed.
    """
    import shutil
    if shutil.which("whisper") is None:
        return None
    stem = os.path.splitext(os.path.basename(clip))[0]
    captioned = os.path.join(workdir, stem + "_captioned.mp4")
    whisper_cmd = ["whisper", clip, "--model", "tiny",
                   "--output_format", "srt", "--output_dir", workdir,
                   "--fp16", "False"]
    burn_cmd = ["ffmpeg", "-y", "-i", clip, "-vf",
                "subtitles=" + os.path.join(
                    workdir, stem + ".srt").replace(":", "\\:"),
                "-c:a", "copy", captioned]
    return (whisper_cmd, burn_cmd, captioned)


def run_cmd(cmd, runner=subprocess.run):
    """Run a command; True when it exits 0."""
    result = runner(cmd, capture_output=True, text=True)
    return result.returncode == 0


def cut_clip(recording, start, end, outfile, runner=subprocess.run):
    """Run the FFmpeg cut. Returns True when the clip file exists."""
    result = runner(build_cut_cmd(recording, start, end, outfile),
                    capture_output=True, text=True)
    if result.returncode != 0:
        print("ffmpeg cut failed: " + (result.stderr or "")[-500:],
              file=sys.stderr)
        return False
    return os.path.exists(outfile)


# ---------------------------------------------------------------------------
# Live chat monitors (both feed the same SpikeDetector)
# ---------------------------------------------------------------------------

class TwitchChatMonitor(threading.Thread):
    """Reads public Twitch chat anonymously (IRC) and feeds a detector."""

    def __init__(self, channel, detector, stop_event, connector=None):
        super().__init__(daemon=True)
        self.channel = channel.lower()
        self.detector = detector
        self.stop_event = stop_event
        # connector(address, timeout) -> socket; defaults to a real TCP
        # connection. Tests inject a socketpair instead (this sandbox
        # blocks outbound TCP, so live chat cannot be verified here —
        # run it on your own machine).
        self._connector = connector or socket.create_connection
        self.connected = False

    def run(self):
        # LIVECLIP_IRC_HOST / LIVECLIP_IRC_PORT override the endpoint
        # (used by tests with a local fake server; normally Twitch IRC).
        host = os.environ.get("LIVECLIP_IRC_HOST", IRC_HOST)
        port = int(os.environ.get("LIVECLIP_IRC_PORT", IRC_PORT))
        nick = "justinfan{}".format(random.randint(10000, 99999))
        while not self.stop_event.is_set():
            try:
                sock = self._connector((host, port), timeout=10)
                sock.sendall("PASS *\r\nNICK {}\r\n".format(nick).encode())
                sock.sendall("JOIN #{}\r\n".format(self.channel).encode())
                self.connected = True
                buf = ""
                sock.settimeout(5)
                while not self.stop_event.is_set():
                    try:
                        data = sock.recv(4096).decode("utf-8", "ignore")
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    buf += data
                    while "\r\n" in buf:
                        line, buf = buf.split("\r\n", 1)
                        parsed = parse_irc_line(line)
                        if parsed is None:
                            continue
                        if parsed[0] == "ping":
                            sock.sendall("PONG :{}\r\n".format(
                                parsed[1]).encode())
                        elif parsed[0] == "msg":
                            self.detector.note_message(time.monotonic())
            except (OSError, socket.error) as exc:
                print("chat disconnected ({}); retrying...".format(exc))
                self.connected = False
                time.sleep(5)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
        self.connected = False


class YouTubeChatMonitor(threading.Thread):
    """Tails a YouTube live chat via chat-downloader and feeds a detector."""

    def __init__(self, url, detector, stop_event, popen=None):
        super().__init__(daemon=True)
        self.url = url
        self.detector = detector
        self.stop_event = stop_event
        self._popen = popen or subprocess.Popen
        self.connected = False

    def run(self):
        while not self.stop_event.is_set():
            proc = None
            try:
                proc = self._popen(build_chat_downloader_cmd(self.url),
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,
                                   text=True, bufsize=1)
                self.connected = True
                for line in proc.stdout:
                    if self.stop_event.is_set():
                        break
                    for _ in range(count_youtube_chat_line(line)):
                        self.detector.note_message(time.monotonic())
                self.connected = False
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            except (OSError, ValueError) as exc:
                print("youtube chat error ({}); retrying...".format(exc))
                self.connected = False
                time.sleep(5)
            finally:
                try:
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                except Exception:
                    pass
            if self.stop_event.is_set():
                break
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Clip a live Twitch or YouTube stream the moment "
                    "chat explodes.")
    p.add_argument("target", nargs="?",
                   help="Twitch channel name, or a YouTube live URL, "
                        "video ID, or @handle/live")
    p.add_argument("--platform", default="auto",
                   choices=["auto", "twitch", "youtube"],
                   help="stream platform (default: auto-detect)")
    p.add_argument("--out", default="clips",
                   help="directory for finished clips (default: clips)")
    p.add_argument("--window", type=int, default=10,
                   help="chat-count window in seconds (default: 10)")
    p.add_argument("--clip-length", type=int, default=30,
                   help="clip length in seconds (default: 30)")
    p.add_argument("--pre-roll", type=int, default=25,
                   help="seconds before the spike included in the clip "
                        "(default: 25, so 25s before + 5s after)")
    p.add_argument("--min-spike", type=int, default=25,
                   help="absolute minimum messages per window to count "
                        "as a spike (default: 25)")
    p.add_argument("--multiplier", type=float, default=3.0,
                   help="spike must also beat baseline x this (default: 3)")
    p.add_argument("--cooldown", type=int, default=60,
                   help="minimum seconds between clips (default: 60)")
    p.add_argument("--quality", default="best",
                   help="streamlink quality (default: best)")
    p.add_argument("--duration", type=int, default=0,
                   help="stop after N seconds (0 = run until Ctrl-C)")
    p.add_argument("--no-captions", action="store_true",
                   help="skip Whisper captions even if whisper is installed")
    p.add_argument("--vertical", action="store_true",
                   help="also export each clip as 9:16 vertical video "
                        "for Shorts/Reels/TikTok")
    p.add_argument("--thumbnails", action="store_true",
                   help="also save a thumbnail still for each clip")
    p.add_argument("--wait-for-live", action="store_true",
                   help="wait until the stream goes live instead of "
                        "exiting when it is not live yet")
    p.add_argument("--max-wait", type=int, default=600,
                   help="with --wait-for-live, give up after N seconds "
                        "(default: 600)")
    p.add_argument("--simulate-chat", metavar="FILE",
                   help="offline test: read 'offset_seconds count' lines "
                        "from FILE and print which windows would spike, "
                        "then exit (no stream needed)")
    p.add_argument("--version", action="version",
                   version="liveclip " + VERSION)
    return p.parse_args(argv)


def require_tool(*names):
    """True when one of `names` is on PATH; else prints how to install."""
    import shutil
    for name in names:
        if shutil.which(name) is not None:
            return True
    print("error: none of {} is on PATH.".format("/".join(names)),
          file=sys.stderr)
    return False


def wait_for_live(url, quality, max_wait):
    """Poll until streamlink sees a live stream, or give up."""
    waited = 0
    while waited <= max_wait:
        if stream_is_live(url, quality):
            return True
        print("stream not live yet — checking again in 30s "
              "({}s waited)...".format(waited))
        time.sleep(30)
        waited += 30
    print("stream never went live within {}s.".format(max_wait))
    return False


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.simulate_chat:
        with open(args.simulate_chat) as fh:
            spikes = simulate_chat_lines(
                fh, window_seconds=args.window,
                min_spike=args.min_spike, multiplier=args.multiplier,
                cooldown_seconds=args.cooldown)
        if not spikes:
            print("no spikes — no clip would have been made.")
            print("Try lowering --min-spike or --multiplier.")
        for t, count in spikes:
            print("spike at {}s: {} msgs in {}s — clip!".format(
                t, count, args.window))
        return 0

    if not args.target:
        print("error: give a Twitch channel or a YouTube live URL.",
              file=sys.stderr)
        return 2
    platform = detect_platform(args.target, args.platform)
    url = stream_url(platform, args.target)
    label = (args.target if platform == "twitch"
             else youtube_label(args.target))

    if not require_tool("streamlink") or not require_tool("ffmpeg"):
        return 2
    if platform == "youtube" and not require_tool("chat_downloader",
                                                  "chat-downloader"):
        print("Install it with: pip install chat-downloader",
              file=sys.stderr)
        return 2
    captions_on = not args.no_captions
    if captions_on and not require_tool("whisper"):
        print("warning: whisper not found — clips will ship without "
              "captions.", file=sys.stderr)
        captions_on = False

    if args.wait_for_live and not wait_for_live(url, args.quality,
                                                args.max_wait):
        return 2

    os.makedirs(args.out, exist_ok=True)
    recording = os.path.join(args.out, "recording.ts")
    manifest_path = os.path.join(args.out, "clips.jsonl")
    chat_log_path = os.path.join(args.out, "windows.csv")
    detector = SpikeDetector(window_seconds=args.window,
                             min_spike=args.min_spike,
                             multiplier=args.multiplier,
                             cooldown_seconds=args.cooldown)
    stop_event = threading.Event()
    if platform == "youtube":
        chat = YouTubeChatMonitor(url, detector, stop_event)
    else:
        chat = TwitchChatMonitor(args.target, detector, stop_event)
    chat.start()

    print("liveclip {} — recording {} live ({})".format(
        VERSION, url, platform))
    print("Chat window: {}s | spike: >={} msgs or baseline x{} | "
          "clip: {}s ({}s pre-roll) | cooldown: {}s".format(
              args.window, args.min_spike, args.multiplier,
              args.clip_length, args.pre_roll, args.cooldown))
    if args.vertical:
        print("Vertical 9:16 exports: on")
    if args.thumbnails:
        print("Thumbnails: on")
    print("Clips go to ./" + args.out + "/ — stop with Ctrl-C.")

    recorder = subprocess.Popen(
        build_streamlink_cmd(url, recording, args.quality))
    record_start = time.monotonic()
    pending = []  # (spike_stream_t, chat_count)
    made = 0
    logged_windows = 0
    try:
        while recorder.poll() is None:
            now = time.monotonic()
            if args.duration and now - record_start >= args.duration:
                print("duration reached, stopping.")
                break
            for spike_count in detector.poll_spikes(now):
                spike_t = now - record_start
                print("[{:.0f}s] chat exploded: {} msgs in {}s — "
                      "clip queued".format(spike_t, spike_count,
                                           args.window))
                pending.append((spike_t, spike_count))
            # Append newly finished chat windows to the tuning log.
            if len(detector.log) > logged_windows:
                fresh = detector.log[logged_windows:]
                logged_windows = len(detector.log)
                new_file = not os.path.exists(chat_log_path)
                with open(chat_log_path, "a", newline="") as fh:
                    writer = csv.writer(fh)
                    if new_file:
                        writer.writerow(["window", "messages",
                                         "baseline", "spike", "clipped"])
                    for i, entry in enumerate(
                            fresh, start=logged_windows - len(fresh) + 1):
                        writer.writerow([i, entry["count"],
                                         entry["baseline"], entry["spike"],
                                         entry["clipped"]])
            recorded_until = now - record_start
            still_pending = []
            for spike_t, chat_count in pending:
                window = clip_window(spike_t, args.pre_roll,
                                     args.clip_length, recorded_until)
                if window is None:
                    still_pending.append((spike_t, chat_count))
                    continue
                start, end = window
                name = format_clip_name(label, spike_t, chat_count)
                outfile = os.path.join(args.out, name)
                print("cutting {:.0f}s–{:.0f}s -> {}".format(
                    start, end, name))
                if cut_clip(recording, start, end, outfile):
                    made += 1
                    final_clip = outfile
                    entry = {"platform": platform,
                             "source": url,
                             "spike_stream_seconds": round(spike_t, 1),
                             "chat_count": chat_count,
                             "clip": name,
                             "made_at": datetime.now().isoformat()}
                    if captions_on:
                        cap = build_caption_cmds(outfile, args.out)
                        if cap:
                            wcmd, bcmd, captioned = cap
                            print("captioning with Whisper...")
                            if run_cmd(wcmd) and run_cmd(bcmd):
                                final_clip = captioned
                                entry["clip_captioned"] = os.path.basename(
                                    captioned)
                                print("captioned clip ready.")
                            else:
                                print("captioning failed — keeping the "
                                      "uncaptioned clip.", file=sys.stderr)
                    if args.vertical:
                        stem = os.path.splitext(
                            os.path.basename(final_clip))[0]
                        vertical = os.path.join(
                            args.out, stem + "_vertical.mp4")
                        if run_cmd(build_vertical_cmd(final_clip,
                                                      vertical)):
                            entry["clip_vertical"] = os.path.basename(
                                vertical)
                            print("vertical export ready: {}".format(
                                vertical))
                    if args.thumbnails:
                        stem = os.path.splitext(
                            os.path.basename(final_clip))[0]
                        thumb = os.path.join(args.out, stem + ".jpg")
                        if run_cmd(build_thumbnail_cmd(
                                final_clip, (start + end) / 2, thumb)):
                            entry["thumbnail"] = os.path.basename(thumb)
                    with open(manifest_path, "a") as fh:
                        fh.write(json.dumps(entry) + "\n")
                    print("clip ready: {}".format(final_clip))
                else:
                    print("clip failed, skipping.", file=sys.stderr)
            pending = still_pending
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        stop_event.set()
        if recorder.poll() is None:
            recorder.terminate()
            try:
                recorder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                recorder.kill()
        chat.join(timeout=5)
    print("done — {} clip(s) in ./{}".format(made, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
