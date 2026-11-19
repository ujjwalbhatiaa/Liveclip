"""webui tests: real HTTP server on localhost, temp data dir, no network."""
import json, shutil, sys, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import webui


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())
        day = cls.d / "channels" / "demo" / "2026-11-18"; day.mkdir(parents=True)
        (day / "demo_x_final.mp4").write_bytes(b"\x00" * 5000)
        (day / "demo_x.json").write_text(json.dumps(dict(score=4.2, hook="WAIT FOR IT", trigger="chat x4.2", thumbnail=None)))
        vd = cls.d / "channels" / "demo" / "vod_highlights"; vd.mkdir()
        (vd / "vod_01_63.mp4").write_bytes(b"\x01" * 100)
        (vd / "shortlist.json").write_text(json.dumps([dict(start=1, end=19, score=63.4, axes=dict(hook=16, flow=16, value=17, trend=14), reason="r")]))
        (vd / "work").mkdir(); (vd / "work" / "sec_01.mp4").write_bytes(b"x")
        import platforms; platforms.CONFIG = cls.d / 'platforms.json'
        cls.app = webui.App(cls.d)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.make_handler(cls.app))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, path, body=None, token=True, host=None, headers=None):
        h = dict(headers or {})
        if body is not None:
            h["Content-Type"] = "application/json"
            if token:
                h["X-LC-Token"] = self.app.token
        if host:
            h["Host"] = host
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", json.dumps(body).encode() if body is not None else None, h)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_page_and_clips(self):
        c, b, _ = self.req("/")
        self.assertEqual(c, 200); self.assertIn(self.app.token.encode(), b)
        clips = json.loads(self.req("/api/clips")[1])
        names = {x["name"] for x in clips}
        self.assertEqual(names, {"demo_x_final.mp4", "vod_01_63.mp4"})   # work/ section files hidden
        vod = next(x for x in clips if x["kind"] == "vod")
        self.assertEqual(vod["score"], 63.4); self.assertEqual(vod["axes"]["hook"], 16)

    def test_forbidden(self):
        self.assertEqual(self.req("/api/channels/add", {"channel": "x"}, token=False)[0], 403)
        self.assertEqual(self.req("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.req("/file?p=../../../etc/passwd")[0], 403)
        self.assertEqual(self.req("/file?p=/etc/passwd")[0], 403)
        self.assertEqual(self.req("/file?p=watchlist.json")[0], 404)   # not a media extension

    def test_range_serving(self):
        clip = json.loads(self.req("/api/clips")[1])[0]
        c, b, h = self.req("/file?p=" + urllib.request.quote(clip["id"]), headers={"Range": "bytes=10-19"})
        self.assertEqual(c, 206); self.assertEqual(len(b), 10); self.assertIn("bytes 10-19/", h["Content-Range"])

    def test_channels_and_review(self):
        c, b, _ = self.req("/api/channels/add", {"channel": "twitch:Alveussanctuary"})
        self.assertEqual(c, 200)
        self.assertEqual([x["name"] for x in json.loads(self.req("/api/state")[1])["channels"]], ["alveussanctuary"])
        self.assertEqual(self.req("/api/channels/add", {"channel": "rtmp://x/y.m3u8"})[0], 400)
        cid = json.loads(self.req("/api/clips")[1])[0]["id"]
        self.assertEqual(self.req("/api/review", {"id": cid, "decision": "approved"})[0], 200)
        self.assertEqual(self.req("/api/review", {"id": cid, "decision": "upload"})[0], 400)
        got = {x["id"]: x["review"] for x in json.loads(self.req("/api/clips")[1])}
        self.assertEqual(got[cid], "approved")
        self.assertEqual(self.req("/api/channels/remove", {"name": "alveussanctuary"})[0], 200)

    def test_ytclip_validation_and_job(self):
        self.assertEqual(self.req("/api/ytclip", {"url": "https://evil.example/x", "start": 0, "end": 10})[0], 400)
        self.assertEqual(self.req("/api/ytclip", {"url": "https://youtu.be/jNQXAC9IVRw", "start": 10, "end": 5})[0], 400)
        self.assertEqual(self.req("/api/ytclip", {"url": "https://youtu.be/jNQXAC9IVRw", "start": "x", "end": 5})[0], 400)
        self.assertEqual(self.req("/api/ytclip", {"url": "https://youtu.be/jNQXAC9IVRw", "start": 0, "end": 10}, token=False)[0], 403)
        import vodclip, time
        orig = vodclip.clip_range
        vodclip.clip_range = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Sign in to confirm you're not a bot"))
        try:
            c, b, _ = self.req("/api/ytclip", {"url": "https://youtu.be/jNQXAC9IVRw", "start": 0, "end": 10})
            self.assertEqual(c, 200)
            for _ in range(40):
                jobs = json.loads(self.req("/api/jobs")[1])
                if jobs and jobs[-1]["state"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(jobs[-1]["state"], "failed"); self.assertIn("bot", jobs[-1]["message"])
        finally:
            vodclip.clip_range = orig

    def test_upload_gates_and_flow(self):
        self.assertEqual(self.req("/api/upload/clip", {"id": "x"}, token=False)[0], 403)      # token required
        st = json.loads(self.req("/api/upload")[1])
        self.assertEqual((st["settings"]["autopilot"], st["settings"]["privacy"]), ("off", "private"))
        self.assertEqual(self.req("/api/upload/settings", {"autopilot": "all"})[0], 400)     # needs the rights tick
        self.assertEqual(self.req("/api/upload/settings", {"privacy": "everyone"})[0], 400)
        calls = []
        self.app.pub.uploaders["youtube"] = lambda v, pack, priv: (calls.append(priv), "https://youtu.be/T")[1]
        self.app.pub.connected = lambda p: True
        clips = json.loads(self.req("/api/clips")[1]); cid = clips[0]["id"]
        self.req("/api/review", {"id": cid, "decision": "rejected"})
        self.assertEqual(self.req("/api/upload/clip", {"id": cid})[0], 400)                    # rejected never uploads
        self.assertEqual(self.req("/api/upload/settings", {"targets": ["myspace"]})[0], 400)
        self.assertEqual(self.req("/api/platform/save", {"platform": "facebook", "fields": {"page_id": "1", "token": "SECRETTOK"}})[0], 200)
        self.assertNotIn(b"SECRETTOK", self.req("/api/upload")[1])                              # secrets never returned
        self.assertEqual(self.req("/api/platform/save", {"platform": "nope", "fields": {}})[0], 400)
        self.req("/api/review", {"id": cid, "decision": "approved"})
        c, b, _ = self.req("/api/upload/clip", {"id": cid})
        self.assertEqual((c, json.loads(b)["results"], calls), (200, {"youtube": "https://youtu.be/T"}, ["private"]))
        self.assertEqual(json.loads(self.req("/api/upload/clip", {"id": cid})[1])["results"], {})   # no double upload
        self.assertEqual(json.loads(self.req("/api/upload")[1])["log"][0]["status"], "uploaded")
        self.assertEqual(self.req("/api/upload/clip", {"id": "../../etc/passwd"})[0], 400)

    def j(self, path, body=None):
        return json.loads(self.req(path, body)[1])

    def test_metadata_endpoints_and_vodjob_validation(self):
        cid = "channels/demo/2026-11-18/demo_x_final.mp4"
        r = self.j("/api/meta?id=" + cid)
        self.assertNotIn("final", r["title"].lower()); self.assertIn("#GTA6", r["description"]); self.assertIn("WAIT FOR IT", r["title"].upper())
        r = self.j("/api/meta/save", dict(id=cid, fields=dict(title="Edited title", tags="x, y")))
        self.assertEqual((r["title"], r["tags"], r["edited"]), ("Edited title", ["x", "y"], True))
        self.assertTrue(r["caption"].startswith("Edited title"))
        r = self.j("/api/meta/reset", dict(id=cid)); self.assertFalse(r["edited"])
        self.assertIn("error", self.j("/api/meta/save", dict(id="nope.mp4", fields=dict(title="x"))))
        self.assertIn("error", self.j("/api/vodjob", dict(url="https://evil.example/x")))
        self.assertIn("error", self.j("/api/vodjob", dict(url="https://youtu.be/rQLEb0W7REo", cookies="; rm -rf")))


if __name__ == "__main__":
    unittest.main()
