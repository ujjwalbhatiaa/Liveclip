"""Tests for liveclip — pure logic only, no network, no stream needed."""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from liveclip import (SpikeDetector, build_chat_downloader_cmd,
                      build_cut_cmd, build_streamlink_cmd,
                      build_thumbnail_cmd, build_vertical_cmd, clip_window,
                      count_youtube_chat_line, detect_platform,
                      format_clip_name, parse_irc_line, simulate_chat_lines,
                      stream_is_live, youtube_label, youtube_url)


# --- IRC parsing -----------------------------------------------------------

def test_parse_privmsg():
    line = ":shroud!shroud@shroud.tmi.twitch.tv PRIVMSG #shroud :LETS GO\r\n"
    assert parse_irc_line(line) == ("msg", "shroud", "shroud", "LETS GO")


def test_parse_ping():
    assert parse_irc_line("PING :tmi.twitch.tv\r\n") == ("ping", "tmi.twitch.tv")


def test_parse_other_lines_ignored():
    assert parse_irc_line(":tmi.twitch.tv 001 justinfan1 :Welcome\r\n") is None
    assert parse_irc_line("") is None


# --- Clip window math ------------------------------------------------------

def test_clip_window_normal():
    # Spike at 100s, 25s pre-roll, 30s clip, 200s recorded.
    assert clip_window(100, 25, 30, 200) == (75, 105)


def test_clip_window_clamps_to_zero():
    # Spike 10s in: clip starts at 0, still 30s long.
    assert clip_window(10, 25, 30, 200) == (0, 30)


def test_clip_window_waits_for_footage():
    # Clip would end at 105s but only 100s recorded: not ready yet.
    assert clip_window(100, 25, 30, 100) is None
    # Once 105s is recorded, it is ready.
    assert clip_window(100, 25, 30, 105) == (75, 105)


def test_clip_window_rejects_garbage():
    assert clip_window(-5, 25, 30, 200) is None
    assert clip_window(100, 25, 30, 0) is None


def test_clip_name_shape():
    name = format_clip_name("shroud", 100, 312)
    assert name.startswith("clip_shroud_")
    assert name.endswith("_chat312.mp4")


# --- Spike detection -------------------------------------------------------

def _feed(detector, start, windows):
    """Feed `windows` message-counts, one per window, starting at `start`."""
    now = start
    spikes = []
    for count in windows:
        for _ in range(count):
            detector.note_message(now)
        now += detector.window_seconds
        hit = detector.check(now)
        if hit is not None:
            spikes.append(hit)
    return spikes


def test_no_spike_on_flat_chat():
    d = SpikeDetector(window_seconds=10, min_spike=25, multiplier=3.0)
    assert _feed(d, 1000.0, [5, 6, 5, 7, 6, 5]) == []


def test_spike_above_absolute_floor():
    d = SpikeDetector(window_seconds=10, min_spike=25, multiplier=3.0)
    assert _feed(d, 1000.0, [5, 6, 5, 40]) == [40]


def test_spike_needs_multiplier_over_baseline():
    # Baseline ~20/window: 40 is above the 25 floor but below 20*3=60.
    d = SpikeDetector(window_seconds=10, min_spike=25, multiplier=3.0)
    assert _feed(d, 1000.0, [20, 21, 19, 20, 40]) == []


def test_cooldown_blocks_double_clip():
    d = SpikeDetector(window_seconds=10, min_spike=25, multiplier=3.0,
                      cooldown_seconds=60)
    # Two exploding windows 10s apart: only the first clips.
    assert _feed(d, 1000.0, [5, 5, 50, 55]) == [50]


def test_spike_again_after_cooldown():
    d = SpikeDetector(window_seconds=10, min_spike=25, multiplier=3.0,
                      cooldown_seconds=60)
    spikes = _feed(d, 1000.0, [5, 5, 50, 5, 5, 5, 5, 5, 5, 5, 60])
    assert spikes == [50, 60]


# --- Chat monitor over an injected socketpair ------------------------------
# (No real TCP: this sandbox blocks outbound TCP, so the monitor takes an
# injectable connector. On a normal machine it connects to Twitch IRC.)

def test_chat_monitor_counts_burst_via_socketpair():
    """Quiet chat, then a 40-message burst: monitor sees it, detector spikes."""
    import socket as sock_mod
    import threading
    import time as time_mod

    from liveclip import TwitchChatMonitor

    client, server_end = sock_mod.socketpair()

    def serve():
        try:
            server_end.recv(4096)  # PASS / NICK / JOIN, ignored
            server_end.sendall(b"PING :fake\r\n")
            for i in range(5):
                server_end.sendall(
                    ":user{}!user{}@user.tmi.twitch.tv PRIVMSG #test "
                    ":hi\r\n".format(i, i).encode())
            time_mod.sleep(2.5)
            for i in range(40):
                server_end.sendall(
                    ":user{}!user{}@user.tmi.twitch.tv PRIVMSG #test "
                    ":LETS GO\r\n".format(i, i).encode())
            time_mod.sleep(4)
        except OSError:
            pass
        finally:
            server_end.close()

    threading.Thread(target=serve, daemon=True).start()
    detector = SpikeDetector(window_seconds=2, min_spike=25, multiplier=3.0)
    stop = threading.Event()
    monitor = TwitchChatMonitor(
        "test", detector, stop,
        connector=lambda addr, timeout=10: client)
    monitor.start()
    time_mod.sleep(6)
    hit = detector.check(time_mod.monotonic())
    stop.set()
    monitor.join(timeout=8)
    assert hit == 40


# --- Command builders ------------------------------------------------------

def test_streamlink_cmd_shape():
    cmd = build_streamlink_cmd("twitch.tv/shroud", "/tmp/rec.ts")
    assert cmd[0] == "streamlink"
    assert "twitch.tv/shroud" in cmd
    assert cmd[-1] == "best"
    assert "/tmp/rec.ts" in cmd


def test_streamlink_cmd_youtube_url():
    cmd = build_streamlink_cmd(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "/tmp/rec.ts")
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in cmd


def test_cut_cmd_shape():
    cmd = build_cut_cmd("/tmp/rec.ts", 75, 105, "/tmp/clip.mp4")
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-ss") + 1] == "75"
    assert cmd[cmd.index("-to") + 1] == "105"
    assert "-c" in cmd and "copy" in cmd


# --- Platform detection ----------------------------------------------------

def test_detect_platform():
    assert detect_platform("shroud") == "twitch"
    assert detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube"
    assert detect_platform("https://youtu.be/dQw4w9WgXcQ") == "youtube"
    assert detect_platform("dQw4w9WgXcQ") == "youtube"
    assert detect_platform("@SkyNews/live") == "youtube"
    assert detect_platform("shroud", "youtube") == "youtube"
    assert detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                           "twitch") == "twitch"


def test_youtube_url():
    assert youtube_url("dQw4w9WgXcQ") == \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert youtube_url("https://youtu.be/dQw4w9WgXcQ") == \
        "https://youtu.be/dQw4w9WgXcQ"
    assert youtube_url("@SkyNews/live") == \
        "https://www.youtube.com/@SkyNews/live"


def test_youtube_label():
    assert youtube_label("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == \
        "dQw4w9WgXcQ"
    assert youtube_label("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_label("https://www.youtube.com/@SkyNews/live") == "youtube"


# --- YouTube chat lines ----------------------------------------------------

def test_count_youtube_chat_line():
    msg = ('{"author": {"name": "sam"}, "message": "W W W", '
           '"timestamp": 1720000000000000}')
    assert count_youtube_chat_line(msg) == 1
    assert count_youtube_chat_line("") == 0
    assert count_youtube_chat_line("not json at all") == 0
    assert count_youtube_chat_line('{"some": "other json"}') == 0
    batch = ('[{"author": {"name": "a"}, "message": "hi"}, '
             '{"author": {"name": "b"}, "message": "yo"}]')
    assert count_youtube_chat_line(batch) == 2


def test_chat_downloader_cmd_shape():
    cmd = build_chat_downloader_cmd("https://www.youtube.com/watch?v=abc")
    assert cmd[0] == "chat_downloader"
    assert "https://www.youtube.com/watch?v=abc" in cmd
    assert "--format" in cmd and "json" in cmd


def test_stream_is_live_parses_json():
    class FakeResult:
        def __init__(self, stdout):
            self.stdout = stdout

    live = lambda *a, **k: FakeResult('{"streams": {"best": {}}}')
    dead = lambda *a, **k: FakeResult('{"error": "No playable streams"}')
    assert stream_is_live("https://example.com/live", runner=live) is True
    assert stream_is_live("https://example.com/live", runner=dead) is False
    assert stream_is_live("https://example.com/live",
                           runner=lambda *a, **k: FakeResult("garbage")) \
        is False


def test_youtube_monitor_counts_fake_chat():
    """YouTube monitor reads scripted JSON chat lines and spikes."""
    import io
    import threading
    import time as time_mod

    from liveclip import YouTubeChatMonitor

    lines = []
    for i in range(5):
        lines.append('{"author": {"name": "u%d"}, "message": "hi"}\n' % i)
    lines.append("SLEEP 2.5\n")
    for i in range(40):
        lines.append('{"author": {"name": "u%d"}, "message": "LETS GO"}\n' % i)

    class FakeStdout:
        def __init__(self, lines):
            self._lines = lines

        def __iter__(self):
            for line in self._lines:
                if line.startswith("SLEEP"):
                    time_mod.sleep(float(line.split()[1]))
                    continue
                yield line

    class FakeProc:
        def __init__(self):
            self.stdout = FakeStdout(lines)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    fake = FakeProc()
    detector = SpikeDetector(window_seconds=2, min_spike=25, multiplier=3.0)
    stop = threading.Event()
    monitor = YouTubeChatMonitor(
        "https://www.youtube.com/watch?v=abc", detector, stop,
        popen=lambda *a, **k: fake)
    monitor.start()
    time_mod.sleep(6)
    hit = detector.check(time_mod.monotonic())
    stop.set()
    monitor.join(timeout=8)
    assert hit == 40


# --- Spike detector log ----------------------------------------------------

def test_detector_logs_windows():
    d = SpikeDetector(window_seconds=10, min_spike=25, multiplier=3.0)
    _feed(d, 1000.0, [5, 6, 5, 40])
    assert len(d.log) == 4
    assert d.log[-1]["count"] == 40
    assert d.log[-1]["spike"] is True
    assert d.log[-1]["clipped"] is True
    assert d.log[0]["spike"] is False


# --- Offline simulator -----------------------------------------------------

def test_simulate_chat_lines():
    lines = ["0 5", "10 6", "20 5", "30 40", "40 5"]
    assert simulate_chat_lines(lines) == [(30.0, 40)]
    assert simulate_chat_lines(["0 5", "10 5", "20 5"]) == []


def test_simulate_chat_ignores_bad_lines():
    lines = ["hello", "0 5", "", "10 notanumber", "20 40"]
    assert simulate_chat_lines(lines) == [(20.0, 40)]


# --- Real FFmpeg cut (needs only ffmpeg, no network) -----------------------

def test_real_cut_from_generated_video(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "clip.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=90:size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=90",
                    "-c:v", "libx264", "-g", "30",
                    "-c:a", "aac", "-shortest", src],
                   check=True, capture_output=True)
    from liveclip import cut_clip
    assert cut_clip(src, 35, 65, out) is True
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", out],
                           check=True, capture_output=True, text=True)
    duration = float(probe.stdout.strip())
    assert 29.0 <= duration <= 31.0


def _make_test_video(path, duration=10):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration={}:size=640x360:rate=30".format(
                        duration),
                    "-f", "lavfi", "-i",
                    "sine=frequency=440:duration={}".format(duration),
                    "-c:v", "libx264", "-g", "30",
                    "-c:a", "aac", "-shortest", path],
                   check=True, capture_output=True)


def test_vertical_cmd_shape():
    cmd = build_vertical_cmd("/tmp/clip.mp4", "/tmp/clip_vertical.mp4")
    assert cmd[0] == "ffmpeg"
    assert "-filter_complex" in cmd
    assert "1080:1920" in " ".join(cmd)


def test_real_vertical_export_is_1080x1920(tmp_path):
    from liveclip import run_cmd
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "vertical.mp4")
    _make_test_video(src)
    assert run_cmd(build_vertical_cmd(src, out)) is True
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams",
                            "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", out],
                           check=True, capture_output=True, text=True)
    width, height = probe.stdout.strip().split(",")
    assert (int(width), int(height)) == (1080, 1920)


def test_thumbnail_cmd_shape_and_real_thumbnail(tmp_path):
    from liveclip import run_cmd
    src = str(tmp_path / "src.mp4")
    thumb = str(tmp_path / "thumb.jpg")
    _make_test_video(src)
    assert run_cmd(build_thumbnail_cmd(src, 5, thumb)) is True
    assert os.path.exists(thumb)
    assert os.path.getsize(thumb) > 0
