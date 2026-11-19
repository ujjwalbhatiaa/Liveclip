"""watch.py tests: fake live source + fake clipper process, no network."""
import sys, tempfile, time, unittest, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watch

FAKE_OK = "import sys,time\nfor l in sys.stdin:\n    if l.strip()=='q': break\n"
FAKE_FAIL = "import sys; print('BLOCKED'); sys.exit(3)\n"


def mk(script, **kw):
    d = Path(tempfile.mkdtemp())
    s = watch.Store(d)
    f = d / "fake.py"; f.write_text(script)
    s.save_channels([dict(target="chan", platform="twitch", url="https://twitch.tv/chan", name="chan")])
    res = {"v": "offline"}
    notes = []
    w = watch.Watcher(s, check=lambda ch: res["v"], clipper_cmd=lambda ch, out, work: [sys.executable, str(f)],
                      notify=notes.append, **kw)
    return s, w, res, notes


class T(unittest.TestCase):
    def test_live_starts_clipper_and_offline_stops_it(self):
        s, w, res, notes = mk(FAKE_OK)
        w.tick(); self.assertFalse(w.procs)
        res["v"] = "live"; st = w.tick()
        self.assertIn("chan", w.procs); self.assertEqual(st["chan"]["status"], "live")
        self.assertTrue(any("LIVE" in n for n in notes))
        p = w.procs["chan"]["p"]
        w.tick(); self.assertIs(w.procs["chan"]["p"], p)      # no duplicate clipper
        res["v"] = "offline"; st = w.tick()
        self.assertFalse(w.procs); self.assertIsNotNone(p.poll()); self.assertEqual(st["chan"]["status"], "offline")

    def test_unknown_never_kills_capture(self):
        s, w, res, notes = mk(FAKE_OK)
        res["v"] = "live"; w.tick(); p = w.procs["chan"]["p"]
        res["v"] = "unknown"; w.tick()
        self.assertIs(w.procs["chan"]["p"], p); self.assertIsNone(p.poll())
        w.stop_all()

    def test_repeated_fast_failure_marks_capture_blocked_once(self):
        s, w, res, notes = mk(FAKE_FAIL)
        res["v"] = "live"
        for _ in range(6):
            w.tick(); time.sleep(0.4)
            for h in list(w.procs.values()): h["p"].wait()
            s2 = s.state()["chan"]
            if s2["status"] == "capture_blocked":
                break
            s.save_state({"chan": {**s2, "last_start": 0}})
        st = s.state()["chan"]
        self.assertEqual(st["status"], "capture_blocked")
        self.assertEqual(sum("capture keeps failing" in n for n in notes), 1)
        self.assertTrue(st["live"])    # still reported live

    def test_cap_limits_clippers(self):
        d = Path(tempfile.mkdtemp()); s = watch.Store(d); f = d / "f.py"; f.write_text(FAKE_OK)
        s.save_channels([dict(target=f"c{i}", platform="twitch", url="u", name=f"c{i}") for i in range(3)])
        w = watch.Watcher(s, check=lambda ch: "live", clipper_cmd=lambda ch, o, wk: [sys.executable, str(f)],
                          max_clippers=2, notify=lambda t: None)
        w.tick(); self.assertEqual(len(w.procs), 2); w.stop_all()

    def test_removed_channel_stops_clipper(self):
        s, w, res, notes = mk(FAKE_OK)
        res["v"] = "live"; w.tick(); self.assertTrue(w.procs)
        s.save_channels([]); w.tick(); self.assertFalse(w.procs)

    def test_vod_pass_queued_after_twitch_end(self):
        s, w, res, notes = mk(FAKE_OK, vod_cmd=lambda ch: None, vod_delay=999)
        res["v"] = "live"; w.tick(); res["v"] = "offline"; w.tick()
        self.assertEqual(len(w.vods), 1)

    def test_state_persists(self):
        s, w, res, notes = mk(FAKE_OK)
        res["v"] = "live"; w.tick(); w.stop_all()
        self.assertEqual(watch.Store(s.home).state()["chan"]["status"], "live")

    def test_youtube_page_classifier(self):
        live = '<link rel="canonical" href="https://www.youtube.com/watch?v=abcdefghijk">"isLiveNow":true'
        self.assertEqual(watch.classify_youtube_page(live), "live")
        blocked = '"playabilityStatus":{"status":"LOGIN_REQUIRED","reason":"Sign in to confirm you\'re not a bot"}'
        self.assertEqual(watch.classify_youtube_page(blocked), "blocked")
        off = '"playabilityStatus":{"status":"LIVE_STREAM_OFFLINE","reason":"This live event will begin in 3 hours."}'
        self.assertEqual(watch.classify_youtube_page(off), "offline")
        self.assertEqual(watch.classify_youtube_page("<html>nothing</html>"), "unknown")

    def test_norm_channel(self):
        self.assertEqual(watch.norm_channel("twitch:Alveussanctuary")["name"], "alveussanctuary")
        self.assertEqual(watch.norm_channel("@SomeChan")["platform"], "youtube")

if __name__ == "__main__":
    unittest.main()
