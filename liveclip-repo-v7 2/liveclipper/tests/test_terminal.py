"""Mocked regression tests: terminal blocked behaviour and recorder failure reporting. No network, no real recorder."""
import argparse, io, sys, tempfile, threading, unittest
from contextlib import redirect_stdout
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import liveclipper as L

BOT = "error: Sign in to confirm you're not a bot. Use --cookies"   # text only; never used to fetch anything


class FastStop(threading.Event):
    """stop event whose wait() does not sleep, so retry loops finish instantly in tests."""
    def wait(self, t=None):
        return self.is_set()


class FakeProc:
    def __init__(self, rc=0):
        self.stdout = io.BytesIO(); self.rc = rc
    def poll(self): return self.rc
    def terminate(self): pass


def make_channel(name, recorder_stderr, max_src_calls=10):
    L.stop = FastStop()
    calls = {"src": 0}
    def fake_popen(cmd, **kw):
        if cmd and cmd[0] in ("streamlink", "yt-dlp"):
            calls["src"] += 1
            if calls["src"] >= max_src_calls:
                L.stop.set()          # safety so a retry bug cannot loop forever
            kw["stderr"].write(recorder_stderr.encode()); kw["stderr"].flush()
        return FakeProc(1 if cmd and cmd[0] in ("streamlink", "yt-dlp") else 0)
    L.subprocess.Popen = fake_popen
    cfg = argparse.Namespace(work=Path(tempfile.mkdtemp()), history=30, selftest=False, recorder="streamlink",
                             quality="best", verbose=False, window=10)
    return L.Channel(name, cfg, None), calls


def run_logged(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


class Classify(unittest.TestCase):
    def test_recorder_failure_kinds(self):
        # v6 semantics: bot/sign-in checks (and members-only) are 'blocked'; a source that is simply
        # not live is 'offline'; everything else - including 403/429 and streamlink's
        # 'No playable streams' - is 'failed' and retried, never terminal.
        self.assertEqual(L.recorder_failure(BOT, 1), "blocked")
        self.assertEqual(L.recorder_failure("members-only content", 1), "blocked")
        self.assertEqual(L.recorder_failure("This stream is offline", 1), "offline")
        self.assertEqual(L.recorder_failure("not currently live", 1), "offline")
        self.assertEqual(L.recorder_failure("premieres in 3 hours", 1), "offline")
        self.assertEqual(L.recorder_failure("This live event has ended", 1), "offline")
        self.assertEqual(L.recorder_failure("HTTP Error 429: Too Many Requests", 1), "failed")
        self.assertEqual(L.recorder_failure("HTTP Error 403 Forbidden", 1), "failed")
        self.assertEqual(L.recorder_failure("error: No playable streams found on this URL", 1), "failed")
        self.assertEqual(L.recorder_failure("segfault", 139), "failed")
        self.assertEqual(L.recorder_failure("", 1), "failed")


class Terminal(unittest.TestCase):
    def setUp(self):
        self._popen, self._stop, self._http = L.subprocess.Popen, L.stop, L.http
    def tearDown(self):
        L.subprocess.Popen, L.stop, L.http = self._popen, self._stop, self._http

    def test_recorder_blocked_is_terminal_and_not_retried(self):
        ch, calls = make_channel("yt-x", BOT)
        out = run_logged(ch.record_loop)
        self.assertEqual(ch.terminal, "blocked")
        self.assertEqual(calls["src"], 1)                       # not retried
        self.assertIn("BLOCKED", out); self.assertIn("NOT offline", out); self.assertIn("Nothing is being recorded", out)
        self.assertNotIn("LIVE - recording", out)

    def test_chat_blocked_is_terminal_and_not_retried(self):
        n = {"c": 0}
        def fake_http(url, body=None, headers=None):
            n["c"] += 1
            return '<link rel="canonical" href="undefined">"playabilityStatus":{"status":"LOGIN_REQUIRED","reason":"Sign in to confirm you’re not a bot"}'
        ch, _ = make_channel("yt-x", "")
        ch.platform = "youtube"; L.http = fake_http
        out = run_logged(ch.yt_chat)
        self.assertEqual(ch.terminal, "blocked"); self.assertEqual(n["c"], 1)
        self.assertIn("NOT offline", out)

    def test_unknown_recorder_failure_retries_forever_with_backoff(self):
        # v6: unknown failures no longer stop after 3 tries - the recorder retries
        # indefinitely with capped backoff (the safety cap in make_channel ends the loop).
        ch, calls = make_channel("yt-x", "boom: weird failure")
        out = run_logged(ch.record_loop)
        self.assertIsNone(ch.terminal); self.assertEqual(calls["src"], 10)
        self.assertIn("retrying", out); self.assertIn("[attempt 1]", out)
        self.assertNotIn("RECORDER FAILED", out); self.assertNotIn("LIVE - recording", out)

    def test_offline_is_not_terminal(self):
        # v6: 'is offline' text still classifies as offline: keeps checking, never terminal.
        ch, calls = make_channel("yt-x", "error: this channel is offline", max_src_calls=4)
        out = run_logged(ch.record_loop)
        self.assertIsNone(ch.terminal); self.assertEqual(calls["src"], 4)
        self.assertIn("no live stream found yet", out); self.assertNotIn("BLOCKED", out)


if __name__ == "__main__":
    unittest.main()
