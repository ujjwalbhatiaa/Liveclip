"""platforms tests: fake HTTP layer; checks the request shapes against the platform docs. No network."""
import json, sys, tempfile, unittest, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import platforms


class Fake:
    def __init__(self, routes): self.calls, self.routes = [], routes
    def __call__(self, method, url, headers=None, data=None, timeout=0):
        self.calls.append((method, url, headers or {}, data))
        for key, resp in self.routes:
            if key in url:
                return resp(method, url, data) if callable(resp) else resp
        return 404, {"error": {"message": "no route " + url}}


class P(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        platforms.CONFIG = d / "platforms.json"
        self.mp4 = d / "c.mp4"; self.mp4.write_bytes(b"v" * 12_000_000)
        self.small = d / "s.mp4"; self.small.write_bytes(b"v" * 1000)
        self.pack = dict(title="Title", description="Desc #Shorts", tags=[], thumbnail=None)
        self.orig = platforms.http

    def tearDown(self): platforms.http = self.orig

    def test_facebook_three_calls(self):
        platforms.set_section("facebook", page_id="PG", token="TOK")
        f = platforms.http = Fake([("/PG/video_reels", lambda m, u, d: (200, {"video_id": "V1", "upload_url": "https://rupload.facebook.com/video-upload/V1"}) if b"start" in d else (200, {"success": True})),
                                   ("rupload.facebook.com", (200, {"success": True}))])
        out = platforms.facebook_upload(self.small, self.pack)
        self.assertEqual(out, "https://www.facebook.com/reel/V1")
        up = [c for c in f.calls if "rupload" in c[1]][0]
        self.assertEqual((up[2]["Authorization"], up[2]["offset"], up[2]["file_size"]), ("OAuth TOK", "0", "1000"))
        fin = f.calls[-1][3].decode()
        self.assertIn("upload_phase=finish", fin); self.assertIn("video_state=PUBLISHED", fin); self.assertIn("video_id=V1", fin)

    def test_facebook_error_message(self):
        platforms.set_section("facebook", page_id="PG", token="TOK")
        platforms.http = Fake([("video_reels", (400, {"error": {"message": "Invalid OAuth access token", "code": 190}}))])
        with self.assertRaisesRegex(RuntimeError, "Invalid OAuth"):
            platforms.facebook_upload(self.small, self.pack)

    def test_instagram_resumable_flow(self):
        platforms.set_section("instagram", ig_id="IG", token="T")
        f = platforms.http = Fake([("/IG/media_publish", (200, {"id": "M9"})),
                                   ("/IG/media", (200, {"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/C1"})),
                                   ("rupload", (200, {"success": True})),
                                   ("/C1?fields=status_code", (200, {"status_code": "FINISHED"}))])
        out = platforms.instagram_upload(self.small, self.pack, poll=0)
        self.assertIn("M9", out)
        body = urllib.parse.parse_qs(f.calls[0][3].decode())
        self.assertEqual((body["media_type"], body["upload_type"]), (["REELS"], ["resumable"]))
        self.assertIn("creation_id=C1", f.calls[-1][3].decode())

    def test_instagram_processing_error(self):
        platforms.set_section("instagram", ig_id="IG", token="T")
        platforms.http = Fake([("/IG/media", (200, {"id": "C1", "uri": "https://x/rupload"})), ("rupload", (200, {})),
                               ("status_code", (200, {"status_code": "ERROR"}))])
        with self.assertRaisesRegex(RuntimeError, "processing failed"):
            platforms.instagram_upload(self.small, self.pack, poll=0)

    def test_tiktok_direct_chunks_and_private_fallback(self):
        platforms.set_section("tiktok", client_key="K", client_secret="S", access_token="AT", refresh_token="RT", expires_at=9999999999)
        ok = {"error": {"code": "ok", "message": ""}}
        f = platforms.http = Fake([("creator_info/query", (200, dict(ok, data={"privacy_level_options": ["SELF_ONLY"]}))),
                                   ("video/init", (200, dict(ok, data={"publish_id": "P1", "upload_url": "https://upload.tiktokapis.com/video/?id=1"}))),
                                   ("upload.tiktokapis.com", (206, {})),
                                   ("status/fetch", (200, dict(ok, data={"status": "PUBLISH_COMPLETE"})))])
        out = platforms.tiktok_upload(self.mp4, self.pack, privacy="public", mode="direct", poll=0)
        self.assertIn("SELF_ONLY", out)                                    # public asked, app unaudited: only private offered
        init = json.loads([c for c in f.calls if c[1].endswith("/video/init/")][0][3])
        self.assertEqual(init["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertEqual((init["source_info"]["chunk_size"], init["source_info"]["total_chunk_count"]), (10_000_000, 1))
        put = [c for c in f.calls if c[0] == "PUT"]
        self.assertEqual(len(put), 1); self.assertEqual(put[0][2]["Content-Range"], "bytes 0-11999999/12000000")

    def test_tiktok_inbox_and_failed(self):
        platforms.set_section("tiktok", client_key="K", client_secret="S", access_token="AT", expires_at=9999999999)
        ok = {"error": {"code": "ok"}}
        platforms.http = Fake([("inbox/video/init", (200, dict(ok, data={"publish_id": "P", "upload_url": "https://u.tiktokapis.com/x"}))),
                               ("u.tiktokapis.com", (201, {})), ("status/fetch", (200, dict(ok, data={"status": "SEND_TO_USER_INBOX"})))])
        self.assertIn("inbox", platforms.tiktok_upload(self.small, self.pack, mode="inbox", poll=0))
        platforms.http = Fake([("inbox/video/init", (200, dict(ok, data={"publish_id": "P", "upload_url": "https://u.tiktokapis.com/x"}))),
                               ("u.tiktokapis.com", (201, {})), ("status/fetch", (200, dict(ok, data={"status": "FAILED", "fail_reason": "bad_codec"})))])
        with self.assertRaisesRegex(RuntimeError, "bad_codec"):
            platforms.tiktok_upload(self.small, self.pack, mode="inbox", poll=0)

    def test_tiktok_refresh(self):
        platforms.set_section("tiktok", client_key="K", client_secret="S", access_token="old", refresh_token="RT", expires_at=1)
        f = platforms.http = Fake([("oauth/token", (200, {"access_token": "new", "refresh_token": "RT2", "expires_in": 86400}))])
        self.assertEqual(platforms._tt_access(), "new")
        self.assertEqual(platforms.load()["tiktok"]["refresh_token"], "RT2")
        self.assertIn("grant_type=refresh_token", f.calls[0][3].decode())

    def test_config_is_private_and_status_hides_secrets(self):
        platforms.set_section("facebook", page_id="PG", token="TOK")
        self.assertEqual(oct(platforms.CONFIG.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("TOK", json.dumps(platforms.status()))

    def test_auth_url_pkce(self):
        u = platforms.tt_auth_url("K", "http://127.0.0.1:5/callback/", "st", "ch")
        self.assertIn("code_challenge_method=S256", u); self.assertIn("video.publish", u)

    def test_tiktok_login_uses_fixed_registerable_redirect(self):
        import socket, threading, urllib.request, urllib.parse
        self.assertEqual(platforms.TT_REDIRECT, "http://127.0.0.1:8766/callback/")
        platforms.set_section("tiktok", client_key="K", client_secret="S")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        seen = {}

        def browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            seen["redirect"] = q["redirect_uri"][0]
            threading.Thread(target=lambda: urllib.request.urlopen(f"{q['redirect_uri'][0]}?code=C&state={q['state'][0]}").read()).start()
        calls = []
        o = (platforms.webbrowser.open, platforms._token_call)
        platforms.webbrowser.open = browser
        platforms._token_call = lambda f: (calls.append(f), dict(access_token="A", refresh_token="R", expires_in=3600))[1]
        try:
            platforms.tiktok_connect(timeout=10, port=port)
        finally:
            platforms.webbrowser.open, platforms._token_call = o
        self.assertEqual(seen["redirect"], f"http://127.0.0.1:{port}/callback/")
        self.assertEqual(calls[0]["redirect_uri"], seen["redirect"])
        self.assertEqual(platforms.load()["tiktok"]["access_token"], "A")

    def test_tiktok_port_busy_message(self):
        import socket
        platforms.set_section("tiktok", client_key="K", client_secret="S")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
        try:
            with self.assertRaises(RuntimeError) as c:
                platforms.tiktok_connect(timeout=1, port=s.getsockname()[1])
            self.assertIn("busy", str(c.exception))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
