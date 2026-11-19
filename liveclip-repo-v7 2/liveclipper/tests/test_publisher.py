"""publisher tests: fake uploader, no network, no Google libraries."""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import publisher


class P(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.mp4 = self.d / "demo_x_final.mp4"; self.mp4.write_bytes(b"x" * 10)
        (self.d / "demo_x_upload.json").write_text(json.dumps(dict(title="Big moment", description="d", tags=["gta"], thumbnail=None)))
        self.calls = []
        self.p = publisher.Publisher(self.d, uploader=lambda v, pack, priv: (self.calls.append((pack["title"], priv)), "https://youtu.be/ID")[1], extra={})
        self.p.connected = lambda pl: True

    def clip(self, review="pending", cid="a/demo_x_final.mp4"):
        return dict(id=cid, review=review, title="t")

    def test_defaults_off_and_private(self):
        s = self.p.settings.get()
        self.assertEqual((s["autopilot"], s["privacy"], s["rights_ack"]), ("off", "private", None))

    def test_autopilot_needs_rights_ack(self):
        with self.assertRaises(ValueError):
            self.p.settings.update(autopilot="all")
        self.p.settings.update(rights_ack=True); self.p.settings.update(autopilot="approved")
        self.assertEqual(self.p.settings.get()["autopilot"], "approved")
        self.p.settings.update(rights_ack=False)          # withdrawing the confirmation switches autopilot off
        self.assertEqual(self.p.settings.get()["autopilot"], "off")

    def test_off_uploads_nothing(self):
        self.assertEqual(self.p.autopilot_pass([self.clip("approved")], lambda c: self.mp4), 0)
        self.assertEqual(self.calls, [])

    def test_approved_mode_only_approved(self):
        self.p.settings.update(rights_ack=True); self.p.settings.update(autopilot="approved")
        n = self.p.autopilot_pass([self.clip("pending", "p"), self.clip("rejected", "r"), self.clip("approved", "ok")], lambda c: self.mp4)
        self.assertEqual((n, len(self.calls)), (1, 1))
        self.assertEqual(self.calls[0][1], "private"); self.assertIn("BIG MOMENT", self.calls[0][0].upper())

    def test_all_mode_skips_rejected_and_never_repeats(self):
        self.p.settings.update(rights_ack=True); self.p.settings.update(autopilot="all")
        cl = [self.clip("pending", "p"), self.clip("rejected", "r")]
        self.assertEqual(self.p.autopilot_pass(cl, lambda c: self.mp4), 1)
        self.assertEqual(self.p.autopilot_pass(cl, lambda c: self.mp4), 0)
        rec = self.p.log.all()
        self.assertEqual((rec[0]["status"], rec[0]["by"], rec[0]["url"]), ("uploaded", "autopilot", "https://youtu.be/ID"))

    def test_failures_logged_and_capped(self):
        def boom(v, pack, priv): raise RuntimeError("quota")
        q = publisher.Publisher(self.d, uploader=boom, extra={}); q.connected = lambda pl: True
        q.settings.update(rights_ack=True); q.settings.update(autopilot="all")
        for _ in range(5):
            q.autopilot_pass([self.clip("pending", "f")], lambda c: self.mp4)
        self.assertEqual(q.log.tries("f"), publisher.MAX_TRIES)

    def test_manual_refuses_rejected_and_duplicates(self):
        with self.assertRaises(ValueError):
            self.p.upload("r", self.mp4, "rejected")
        self.p.upload("m", self.mp4, "approved")
        with self.assertRaises(ValueError):
            self.p.upload("m", self.mp4, "approved")

    def test_pack_from_upload_json_adds_shorts_tag(self):
        pk = publisher.build_pack(self.mp4)
        self.assertIn("BIG MOMENT", pk["title"].upper()); self.assertIn("#Shorts", pk["description"]); self.assertIn("#GTA6", pk["description"])

    def test_privacy_validated(self):
        with self.assertRaises(ValueError):
            self.p.upload("z", self.mp4, "approved", privacy="everyone")


    def test_multi_target_one_failure_does_not_block_others(self):
        seen = []
        def bad(v, pack, priv): raise RuntimeError("tiktok down")
        q = publisher.Publisher(self.d, uploader=lambda v, p, pr: "yt", extra=dict(facebook=lambda v, p, pr: (seen.append("fb"), "fbu")[1], tiktok=bad))
        q.connected = lambda pl: pl != "instagram"
        q.settings.update(rights_ack=True); q.settings.update(autopilot="all", targets=["youtube", "facebook", "tiktok", "instagram"])
        self.assertEqual(q.autopilot_pass([self.clip("pending", "m")], lambda c: self.mp4), 3)   # instagram not connected: skipped
        by = {(r["platform"], r["status"]) for r in q.log.all()}
        self.assertEqual(by, {("youtube", "uploaded"), ("facebook", "uploaded"), ("tiktok", "failed")})
        self.assertEqual(q.autopilot_pass([self.clip("pending", "m")], lambda c: self.mp4), 1)   # only tiktok retried
        with self.assertRaises(ValueError):
            q.settings.update(targets=["myspace"])


if __name__ == "__main__":
    unittest.main()
