#!/usr/bin/env python3
"""platforms.py - Facebook Reels, Instagram Reels and TikTok upload through each platform's official API. Standard library only.

Everything runs on this computer. Credentials live in ~/.config/liveclipper/platforms.json (mode 600), never in the repo.
Each function takes (video_path, pack, privacy) like publisher.youtube_upload and returns a result string (URL or status).
NOT tested against the real services from our side: tested with a fake HTTP layer that checks the request shapes
against the platform docs (see PLATFORMS.md for the links and the approvals you need).
"""
import base64, hashlib, http.server as _hs, json, os, secrets, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser
from pathlib import Path

CONFIG = Path.home() / ".config" / "liveclipper" / "platforms.json"
GRAPH = "https://graph.facebook.com/v26.0"
TT = "https://open.tiktokapis.com"


def load():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(CONFIG), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG, 0o600)


def set_section(name, **vals):
    cfg = load()
    cfg.setdefault(name, {}).update({k: v for k, v in vals.items() if v not in (None, "")})
    save(cfg)


def status():
    """Which platforms are connected (never returns secrets)."""
    c = load()
    return dict(facebook=bool(c.get("facebook", {}).get("page_id") and c.get("facebook", {}).get("token")),
                instagram=bool(c.get("instagram", {}).get("ig_id") and c.get("instagram", {}).get("token")),
                tiktok=bool(c.get("tiktok", {}).get("refresh_token") or c.get("tiktok", {}).get("access_token")),
                tiktok_app=bool(c.get("tiktok", {}).get("client_key") and c.get("tiktok", {}).get("client_secret")))


def http(method, url, headers=None, data=None, timeout=300):
    """-> (status, parsed json or text). Replaced by a fake in tests."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    try:
        return code, json.loads(body or b"{}")
    except ValueError:
        return code, body.decode(errors="replace")


def _need(code, body, what):
    err = body.get("error") if isinstance(body, dict) else None
    ok_marker = isinstance(err, dict) and err.get("code") in (None, "", "ok")   # TikTok sends error.code "ok" on success
    if code >= 400 or (err and not ok_marker):
        if isinstance(err, dict):
            err = err.get("message") or err.get("code")
        raise RuntimeError(f"{what} failed ({code}): {str(err or body)[:240]}")
    return body


def caption(pack, limit):
    text = pack.get("caption") or (pack["title"] + "\n\n" + pack["description"])
    return text[:limit]


def _post_form(url, fields):
    return http("POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(fields).encode())


# ---------------------------------------------------------------- Facebook Page reel
def facebook_upload(video, pack, privacy=None):
    c = load().get("facebook", {})
    if not (c.get("page_id") and c.get("token")):
        raise RuntimeError("Facebook is not set up (Page ID + Page access token)")
    pid, tok = c["page_id"], c["token"]
    size = os.path.getsize(video)
    code, b = http("POST", f"{GRAPH}/{pid}/video_reels", {"Content-Type": "application/json"},
                   json.dumps({"upload_phase": "start", "access_token": tok}).encode())
    b = _need(code, b, "Facebook start")
    vid, up = b["video_id"], b.get("upload_url") or f"https://rupload.facebook.com/video-upload/v26.0/{b['video_id']}"
    with open(video, "rb") as f:
        code, b = http("POST", up, {"Authorization": "OAuth " + tok, "offset": "0", "file_size": str(size),
                                    "Content-Type": "application/octet-stream"}, f.read())
    _need(code, b, "Facebook upload")
    code, b = _post_form(f"{GRAPH}/{pid}/video_reels", dict(access_token=tok, video_id=vid, upload_phase="finish",
                                                             video_state="PUBLISHED", description=caption(pack, 2200)))
    _need(code, b, "Facebook publish")
    return f"https://www.facebook.com/reel/{vid}"


# ---------------------------------------------------------------- Instagram Reel
def instagram_upload(video, pack, privacy=None, wait=300, poll=5):
    c = load().get("instagram", {})
    if not (c.get("ig_id") and c.get("token")):
        raise RuntimeError("Instagram is not set up (Instagram account ID + access token)")
    iid, tok = c["ig_id"], c["token"]
    size = os.path.getsize(video)
    code, b = _post_form(f"{GRAPH}/{iid}/media", dict(media_type="REELS", upload_type="resumable",
                                                      caption=caption(pack, 2200), access_token=tok))
    b = _need(code, b, "Instagram container")
    cid, up = b["id"], b.get("uri") or f"https://rupload.facebook.com/ig-api-upload/v26.0/{b['id']}"
    with open(video, "rb") as f:
        code, b = http("POST", up, {"Authorization": "OAuth " + tok, "offset": "0", "file_size": str(size)}, f.read())
    _need(code, b, "Instagram upload")
    end = time.time() + wait
    while True:
        code, b = http("GET", f"{GRAPH}/{cid}?fields=status_code&access_token={urllib.parse.quote(tok)}")
        b = _need(code, b, "Instagram status")
        st = b.get("status_code")
        if st == "FINISHED":
            break
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError("Instagram processing failed: " + st)
        if time.time() > end:
            raise RuntimeError("Instagram is still processing after %d s" % wait)
        time.sleep(poll)
    code, b = _post_form(f"{GRAPH}/{iid}/media_publish", dict(creation_id=cid, access_token=tok))
    b = _need(code, b, "Instagram publish")
    return f"instagram media id {b.get('id')}"


# ---------------------------------------------------------------- TikTok
def tt_auth_url(client_key, redirect, state, challenge):
    q = urllib.parse.urlencode(dict(client_key=client_key, scope="user.info.basic,video.publish,video.upload", response_type="code",
                                    redirect_uri=redirect, state=state, code_challenge=challenge, code_challenge_method="S256"))
    return "https://www.tiktok.com/v2/auth/authorize/?" + q


def _token_call(fields):
    code, b = _post_form(f"{TT}/v2/oauth/token/", fields)
    b = _need(code, b, "TikTok token")
    if not b.get("access_token"):
        raise RuntimeError("TikTok token failed: " + str(b.get("error_description") or b)[:200])
    return b


def _store_tokens(b):
    set_section("tiktok", access_token=b["access_token"], refresh_token=b.get("refresh_token"),
                expires_at=int(time.time()) + int(b.get("expires_in", 86000)) - 120)


TT_PORT = 8766
TT_REDIRECT = f"http://127.0.0.1:{TT_PORT}/callback/"   # register EXACTLY this as the redirect URI in the TikTok app


def tiktok_connect(timeout=300, port=None):
    """Browser login on this computer. Needs client_key + client_secret saved first and TT_REDIRECT registered in the TikTok app
    (TikTok matches redirect URIs exactly, so the port is fixed)."""
    port = port or TT_PORT
    t = load().get("tiktok", {})
    if not (t.get("client_key") and t.get("client_secret")):
        raise RuntimeError("save the TikTok client key and secret first")
    verifier = secrets.token_urlsafe(48)
    challenge = hashlib.sha256(verifier.encode()).hexdigest()   # TikTok's PKCE uses hex-encoded SHA256
    state, got = secrets.token_urlsafe(12), {}

    class H(_hs.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(b"liveclipper: you can close this tab.")
    try:
        srv = _hs.HTTPServer(("127.0.0.1", port), H)
    except OSError as e:
        raise RuntimeError(f"port {port} is busy ({e.strerror}); close whatever uses it and try again")
    srv.timeout = timeout
    redirect = f"http://127.0.0.1:{port}/callback/"
    webbrowser.open(tt_auth_url(t["client_key"], redirect, state, challenge))
    srv.handle_request()
    srv.server_close()
    if got.get("state") != state or not got.get("code"):
        raise RuntimeError("TikTok login did not complete: " + str(got.get("error_description") or got.get("error") or "no code"))
    _store_tokens(_token_call(dict(client_key=t["client_key"], client_secret=t["client_secret"], code=got["code"],
                                   grant_type="authorization_code", redirect_uri=redirect, code_verifier=verifier)))


def _tt_access():
    t = load().get("tiktok", {})
    if t.get("access_token") and t.get("expires_at", 0) > time.time():
        return t["access_token"]
    if not t.get("refresh_token"):
        raise RuntimeError("TikTok is not connected")
    b = _token_call(dict(client_key=t["client_key"], client_secret=t["client_secret"], grant_type="refresh_token",
                         refresh_token=t["refresh_token"]))
    _store_tokens(b)
    return b["access_token"]


def tiktok_upload(video, pack, privacy="private", mode=None, wait=180, poll=5):
    """mode 'direct' posts to the profile (private only until TikTok audits your app); 'inbox' sends it to your TikTok app to finish."""
    mode = mode or load().get("tiktok", {}).get("mode", "direct")
    tok = _tt_access()
    H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8"}
    size = os.path.getsize(video)
    chunk = size if size < 5_000_000 else 10_000_000
    total = max(1, size // chunk)
    src = dict(source="FILE_UPLOAD", video_size=size, chunk_size=chunk, total_chunk_count=total)
    if mode == "inbox":
        url, body = f"{TT}/v2/post/publish/inbox/video/init/", dict(source_info=src)
    else:
        code, b = http("POST", f"{TT}/v2/post/publish/creator_info/query/", H, b"{}")
        opts = (_need(code, b, "TikTok creator info").get("data") or {}).get("privacy_level_options") or ["SELF_ONLY"]
        want = {"public": "PUBLIC_TO_EVERYONE", "unlisted": "FOLLOWER_OF_CREATOR"}.get(privacy, "SELF_ONLY")
        level = want if want in opts else ("SELF_ONLY" if "SELF_ONLY" in opts else opts[0])
        url = f"{TT}/v2/post/publish/video/init/"
        body = dict(post_info=dict(title=caption(pack, 2200), privacy_level=level), source_info=src)
    code, b = http("POST", url, H, json.dumps(body).encode())
    d = _need(code, b, "TikTok init").get("data") or {}
    if not d.get("upload_url"):
        raise RuntimeError("TikTok init returned no upload_url: " + str(b)[:200])
    with open(video, "rb") as f:
        for i in range(total):
            start = i * chunk
            part = f.read(chunk if i < total - 1 else size - start)
            code, b = http("PUT", d["upload_url"], {"Content-Type": "video/mp4", "Content-Length": str(len(part)),
                                                    "Content-Range": f"bytes {start}-{start + len(part) - 1}/{size}"}, part)
            if code >= 400:
                raise RuntimeError(f"TikTok upload failed ({code}): {str(b)[:200]}")
    end, st = time.time() + wait, "PROCESSING_UPLOAD"
    while time.time() < end:
        code, b = http("POST", f"{TT}/v2/post/publish/status/fetch/", H, json.dumps(dict(publish_id=d["publish_id"])).encode())
        dd = _need(code, b, "TikTok status").get("data") or {}
        st = dd.get("status", st)
        if st == "FAILED":
            raise RuntimeError("TikTok rejected it: " + str(dd.get("fail_reason")))
        if st in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            break
        time.sleep(poll)
    note = "sent to your TikTok inbox: open the TikTok app to finish and post" if st == "SEND_TO_USER_INBOX" else \
           ("posted, visibility " + body["post_info"]["privacy_level"] if st == "PUBLISH_COMPLETE" else "TikTok status " + st)
    return f"tiktok {note} (publish_id {d['publish_id']})"


UPLOADERS = dict(facebook=facebook_upload, instagram=instagram_upload, tiktok=tiktok_upload)
