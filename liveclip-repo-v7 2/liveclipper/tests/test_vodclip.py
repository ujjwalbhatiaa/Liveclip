"""Offline tests for vodclip: scoring, candidates, webcam detection, stacked render. Synthetic media only."""
import json, subprocess, sys, tempfile, unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vodclip as vc

_vid = {}

def make_video(cam=True, tone=True):
    key = (cam, tone)
    if key in _vid:
        return _vid[key]
    d = Path(tempfile.mkdtemp())
    out = d / "t.mp4"
    # 640x360 moving game pattern; skin-tone box bottom-right as fake facecam; quiet noise with a loud burst at 40-46s
    vf = "testsrc2=s=640x360:r=10:d=60[g]"
    chain = vf
    if cam:
        chain += ";color=c=0xD9A98C:s=150x100:r=10:d=60[c];[g][c]overlay=x=470:y=250[v]"
    else:
        chain += ";[g]null[v]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-filter_complex", chain,
           "-f", "lavfi", "-i", "anoisesrc=d=60:c=pink:a=0.01:r=8000", "-map", "[v]", "-map", "1:a"]
    if tone:
        cmd = ["ffmpeg", "-y", "-v", "error", "-filter_complex",
               chain + ";anoisesrc=d=60:c=pink:a=0.01:r=8000[q];sine=f=300:d=6:r=8000,volume=0.6,adelay=40000|40000,apad=whole_dur=60[b];[q][b]amix=inputs=2:duration=first:normalize=0[a]",
               "-map", "[v]", "-map", "[a]"]
    cmd += ["-t", "60", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)]
    subprocess.run(cmd, check=True)
    _vid[key] = out
    return out


class T(unittest.TestCase):
    def test_score_bounds_and_determinism(self):
        words = [(0.1, 0.4, "Why"), (0.5, 0.8, "did"), (0.9, 1.2, "100"), (1.3, 1.6, "people"), (1.7, 2.0, "die?"),
                 (2.2, 2.6, "insane"), (3.0, 3.5, "clutch."), (4.0, 4.5, "Wow!")]
        db = np.full(120, -40.0); db[10:16] = -20
        a = vc.score_segment(0, 20, words, db, [], 600)
        b = vc.score_segment(0, 20, words, db, [], 600)
        self.assertEqual(a, b)
        self.assertTrue(0 <= a["score"] <= 100)
        self.assertLessEqual(a["axes"]["hook"], 30); self.assertLessEqual(a["axes"]["trend"], 15)

    def test_signoff_and_opening_penalty(self):
        words = [(0.1, 0.4, "Subscribe"), (0.5, 0.8, "and"), (0.9, 1.2, "follow"), (1.3, 1.6, "me")]
        db = np.full(120, -40.0)
        self.assertEqual(vc.score_segment(100, 120, words, db, [], 600)["axes"]["hook"], 0)
        db = np.full(400, -40.0)
        a = vc.score_segment(5, 25, [], db, [], 600); b = vc.score_segment(100, 120, [], db, [], 600)
        self.assertLessEqual(a["score"], b["score"])

    def test_no_speech_scores_on_audio(self):
        db = np.full(200, -45.0); db[100:112] = -18
        quiet = vc.score_segment(10, 30, [], db, [], 600)
        loud = vc.score_segment(48, 60, [], db, [], 600)
        self.assertTrue(loud["score"] > quiet["score"])
        self.assertIn("no speech", loud["reason"])

    def test_silence_gives_no_candidates(self):
        self.assertEqual(vc.find_candidates(np.full(400, -80.0)), [])

    def test_loud_burst_ranked_first_no_speech(self):
        v = make_video(cam=False)
        r = vc.rank(v, [], top=3)
        self.assertTrue(r)
        top = r[0]
        self.assertTrue(top["start"] - 1 <= 40 <= top["end"] + 1, top)

    def test_cam_detected_and_absent(self):
        cam = vc.detect_cam(make_video(cam=True))
        self.assertIsNotNone(cam)
        x, y, w, h = cam
        self.assertTrue(x >= 400 and y >= 200, cam)  # bottom-right
        self.assertIsNone(vc.detect_cam(make_video(cam=False)))

    def test_stack_render_1080x1920(self):
        v = make_video(cam=True)
        cam = (470, 250, 150, 100)
        out = Path(tempfile.mkdtemp()) / "o.mp4"
        vc.render_clip(v, dict(start=36, end=50), out, "stack", cam, 0.6, captions=False)
        self.assertEqual(vc.probe(out)[:2], (1080, 1920))
        self.assertAlmostEqual(vc.probe(out)[2], 14, delta=1)

    def test_stack_fallback_without_cam(self):
        v = make_video(cam=False)
        out = Path(tempfile.mkdtemp()) / "o.mp4"
        vc.render_clip(v, dict(start=36, end=50), out, "stack", None, 0.6, captions=False)
        self.assertEqual(vc.probe(out)[:2], (1080, 1920))

_dl_orig = vc.download_video


class YouTubeLocal(unittest.TestCase):
    _dl = staticmethod(_dl_orig)

    """Regression for 'ERROR: ffmpeg exited with code 251': ffmpeg must never read an https URL for YouTube videos."""
    URL = "https://youtu.be/rQLEb0W7REo"

    def setUp(self):
        self.v = make_video(cam=False)
        self.calls, self.seen = [], []
        self._o = (vc._yt_run, vc.yt_info, subprocess.run)
        cache = Path(tempfile.mkdtemp())
        self.cache = cache

        def fake_run(args, timeout=0):
            self.calls.append(list(args))
            out = args[args.index("-o") + 1].replace("%(ext)s", "mp4")
            Path(out).write_bytes(Path(self.v).read_bytes())
            return ""
        vc._yt_run = fake_run
        vc.yt_info = lambda url: dict(channel="IShowSpeed", title="t", duration=60.0, live=False)
        real = subprocess.run

        def spy(cmd, *a, **k):
            if cmd and cmd[0] == "ffmpeg":
                self.seen.append(cmd)
            return real(cmd, *a, **k)
        vc.subprocess.run = spy

    def tearDown(self):
        vc._yt_run, vc.yt_info, vc.subprocess.run = self._o

    def no_https(self):
        for c in self.seen:
            self.assertFalse(any("http" in str(x) for x in c), c)

    def test_download_is_whole_file_no_sections_and_cached(self):
        p = vc.download_video(self.URL, self.cache)
        self.assertTrue(p.exists())
        self.assertNotIn("--download-sections", self.calls[0])
        self.assertIn("--hls-prefer-native", self.calls[0])
        vc.download_video(self.URL, self.cache)
        self.assertEqual(len(self.calls), 1)          # second call served from the cache

    def test_fetch_range_cuts_a_local_file(self):
        dest = Path(tempfile.mkdtemp()) / "src.mp4"
        vc.fetch_range(self.URL, 5, 20, dest, download=lambda u: self.v)
        self.assertAlmostEqual(vc.probe(dest)[2], 15, delta=1)
        self.no_https()

    def test_live_video_refused(self):
        vc.yt_info = lambda url: dict(channel="c", title="t", duration=0, live=True)
        with self.assertRaises(RuntimeError):
            vc.download_video(self.URL, self.cache)

    def test_whole_video_mode_end_to_end(self):
        vc.download_video = lambda url, cache_dir=None, _o=vc.download_video: _o(url, self.cache)
        out = Path(tempfile.mkdtemp())
        try:
            vc.main([self.URL, "--top", "1", "--layout", "blur", "--no-captions", "--out", str(out)])
        finally:
            vc.download_video = self._dl
        sl = json.loads((out / "shortlist.json").read_text())
        self.assertEqual((sl[0]["channel"], sl[0]["source"]), ("IShowSpeed", self.URL))
        clip = next(out.glob("vod_01_*.mp4"))
        self.assertEqual(vc.probe(clip)[:2], (1080, 1920))
        self.no_https()
        self.assertEqual(len(self.calls), 1)

    def test_channel_and_cookie_parsing(self):
        self.assertTrue(vc.is_channel("@KaiCenat")); self.assertTrue(vc.is_channel("https://www.youtube.com/@KaiCenat/streams"))
        self.assertFalse(vc.is_channel("https://youtu.be/rQLEb0W7REo")); self.assertFalse(vc.is_channel("https://evil.example/@x"))
        vc._yt_run = lambda args, timeout=0: "aaaaaaaaaaa\tPast stream\twas_live\nbbbbbbbbbbb\tLive now\tis_live\n"
        self.assertEqual(vc.resolve_channel("@KaiCenat", 2), [("https://www.youtube.com/watch?v=aaaaaaaaaaa", "Past stream")])


if __name__ == "__main__":
    unittest.main()


class ClipRange(unittest.TestCase):
    def test_source_validation(self):
        ok = vc.clip_source_ok
        self.assertTrue(ok("https://www.youtube.com/watch?v=jNQXAC9IVRw"))
        self.assertTrue(ok("https://youtu.be/jNQXAC9IVRw"))
        self.assertTrue(ok("https://www.twitch.tv/videos/123456"))
        for bad in ("file:///etc/passwd", "https://evil.example/watch?v=jNQXAC9IVRw", "https://www.youtube.com/watch?v=short",
                    "/tmp/x.mp4", "https://www.youtube.com.evil.example/watch?v=jNQXAC9IVRw"):
            self.assertFalse(ok(bad), bad)

    def test_clip_range_uses_fetch_and_renders(self):
        v = make_video(cam=False)
        got = {}
        def fake(url, s, e, dest):
            got.update(url=url, s=s, e=e)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(s), "-t", str(e - s), "-i", str(v), "-c", "copy", str(dest)], check=True)
        out = vc.clip_range("https://www.youtube.com/watch?v=jNQXAC9IVRw", 38, 46, tempfile.mkdtemp(), "blur", captions=False, fetch=fake)
        self.assertEqual((got["s"], got["e"]), (36, 48))
        self.assertEqual(vc.probe(out)[:2], (1080, 1920)); self.assertAlmostEqual(vc.probe(out)[2], 8, delta=1)
        self.assertTrue(out.with_name(out.name.replace("_final.mp4", ".json")).exists())

    def test_bad_ranges(self):
        for s, e in ((10, 5), (0, 400), (-1, 5)):
            with self.assertRaises(ValueError):
                vc.clip_range("https://youtu.be/jNQXAC9IVRw", s, e, tempfile.mkdtemp(), fetch=lambda *a: None)
        with self.assertRaises(ValueError):
            vc.clip_range("https://example.com/v", 0, 5, tempfile.mkdtemp(), fetch=lambda *a: None)
