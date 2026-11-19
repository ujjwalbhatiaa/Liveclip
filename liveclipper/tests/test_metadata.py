"""metadata tests: generated titles never use the file name; edits override; pack goes to every platform."""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import metadata, publisher


class M(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.mp4 = self.d / "ishowspeed_20261119-003634_x2.7_final.mp4"; self.mp4.write_bytes(b"x")

    def test_no_filename_title(self):
        for hint in ({}, dict(kind="live", meta=dict(trigger="chat x2.7")), dict(meta=dict(transcript="Oh my god he just flew off the building! No way."))):
            g = metadata.generate(self.mp4, hint, {})
            self.assertNotIn("20261119", g["title"]); self.assertNotIn("final", g["title"].lower())
            self.assertLessEqual(len(g["title"]), 100)
            self.assertIn("GTA 6", g["title"]); self.assertIn("#GTA6", g["description"])
            self.assertLessEqual(len(",".join(g["tags"])), 500)
            self.assertIn("IShowSpeed", g["title"] + g["description"])

    def test_uses_real_words_only(self):
        g = metadata.generate(self.mp4, dict(meta=dict(hook="HE FLEW OFF THE ROOF")), {})
        self.assertIn("FLEW OFF THE ROOF", g["title"].upper())

    def test_filename_like_old_title_ignored(self):
        g = metadata.generate(self.mp4, {}, dict(title="ishowspeed 20261119-003634 x2.7 final"))
        self.assertNotIn("20261119", g["title"])

    def test_edits_override_and_autopilot_uses_them(self):
        calls = []
        p = publisher.Publisher(self.d, uploader=lambda v, pack, pr: (calls.append(pack), "u")[1], extra={})
        p.connected = lambda pl: True
        p.upload("c1", self.mp4, "approved", {"id": "c1"}, source="autopilot")
        self.assertNotIn("20261119", calls[-1]["title"]); self.assertTrue(calls[-1]["tags"]); self.assertIn("#GTA6", calls[-1]["caption"])
        p.meta.save("c2", dict(title="My own title", tags="a, b"))
        p.upload("c2", self.mp4, "approved", {"id": "c2"})
        self.assertEqual(calls[-1]["title"], "My own title"); self.assertEqual(calls[-1]["tags"], ["a", "b"])
        self.assertTrue(calls[-1]["caption"].startswith("My own title"))
        with self.assertRaises(ValueError):
            p.meta.save("c3", dict(title="  "))

    def test_no_links_anywhere(self):
        hint = dict(kind="live", meta=dict(channel="ishowspeed", caption="see https://twitch.tv/ishowspeed now", transcript="wow www.x.com crazy!"))
        g = metadata.generate(self.mp4, hint, dict(source="https://twitch.tv/ishowspeed"))
        for k in ("title", "description", "caption"):
            self.assertNotIn("http", g[k]); self.assertNotIn("www.", g[k]); self.assertNotIn(".tv/", g[k])
        self.assertIn("Clip from IShowSpeed's live stream", g["description"])

    def test_game_setting(self):
        s = metadata.MetaStore(self.d); s.set_game("Fortnite")
        g = metadata.generate(self.mp4, {}, {}, game=s.game())
        self.assertIn("Fortnite", g["title"]); self.assertIn("#Fortnite", g["description"]); self.assertNotIn("GTA6", g["description"])


if __name__ == "__main__":
    unittest.main()
