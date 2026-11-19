#!/usr/bin/env python3
"""
webui.py - local control panel for liveclipper. Standard library only, bound to 127.0.0.1 (this computer only).

  python3 webui.py [--port 8765] [--home watch_data]      then open http://127.0.0.1:8765

Channels + live status, start/stop the watcher, clip shortlist with scores, review queue, logs.
Review records Approve/Reject. Uploading to YouTube (by hand or autopilot, off by default) lives in publisher.py and the Upload tab.
"""
import argparse, json, mimetypes, os, re, secrets, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import watch  # noqa: E402
import platforms  # noqa: E402
import publisher  # noqa: E402

VIDEO_EXT = {".mp4", ".webm"}


class App:
    def __init__(self, home, extra_roots=()):
        self.home = Path(home).resolve()
        self.store = watch.Store(self.home)
        self.roots = [self.home] + [Path(r).resolve() for r in extra_roots]
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.proc = None
        self.review_path = self.home / "review.json"
        self.jobs = {}
        self.pub = publisher.Publisher(self.home)
        self.autopilot_thread = None
        self.yt_connect = dict(state="idle", message="")

    def start_clip_job(self, url, start, end, layout):
        import vodclip
        if not vodclip.clip_source_ok(url):
            raise ValueError("only YouTube video links and Twitch VOD links are supported")
        if layout not in ("blur", "crop", "stack"):
            raise ValueError("bad layout")
        start, end = float(start), float(end)
        if not (0 <= start < end) or end - start > 180:
            raise ValueError("clip must be between 1 and 180 seconds")
        jid = secrets.token_hex(4)
        job = self.jobs[jid] = dict(id=jid, url=url, start=start, end=end, state="running", message="fetching the video part")

        def run():
            try:
                p = vodclip.clip_range(url, start, end, self.home / "manual_clips", layout=layout)
                job.update(state="done", message="done - it is in the Review queue", file=self.rel(p))
            except Exception as e:  # shown to the user; usually YouTube refusing the download
                job.update(state="failed", message=str(e)[:300])
        threading.Thread(target=run, daemon=True).start()
        return jid

    def start_find_job(self, url, top=5, layout="blur", cookies=None, latest=1, rng=None):
        """Whole-video mode: score the whole video / past stream (or a channel's newest past broadcasts) and render the best moments."""
        import vodclip
        url = str(url).strip()
        if not (vodclip.clip_source_ok(url) or vodclip.is_channel(url) or re.match(r"https?://(?:www\.)?twitch\.tv/videos/\d+", url)):
            raise ValueError("paste a YouTube video / past stream link, a YouTube channel (@handle) or a Twitch VOD link")
        top, latest = int(top), int(latest)
        if not (1 <= top <= 15) or not (1 <= latest <= 5) or layout not in ("blur", "crop", "stack"):
            raise ValueError("bad options")
        if cookies and cookies not in ("chrome", "safari", "firefox", "edge", "brave"):
            raise ValueError("bad browser")
        cmd = [sys.executable, str(HERE / "vodclip.py"), url, "--top", str(top), "--layout", layout, "--transcribe", "--latest", str(latest)]
        if cookies:
            cmd += ["--cookies", cookies]
        if rng:
            if not re.fullmatch(r"\d+-\d+", str(rng)):
                raise ValueError("range looks like 3600-7200 (seconds)")
            cmd += ["--range", str(rng)]
        jid = secrets.token_hex(4)
        out = self.home / "vod_clips" / jid
        cmd += ["--out", str(out)]
        job = self.jobs[jid] = dict(id=jid, url=url, start=0, end=0, state="running", message="downloading audio and scoring moments (can take several minutes)")

        def run():
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(HERE))
                last = ""
                for line in p.stdout:
                    if line.strip():
                        last = line.strip()[-300:]
                        job["message"] = last
                p.wait()
                if p.returncode:
                    job.update(state="failed", message=last or "vodclip failed")
                else:
                    n = len(list(out.rglob("vod_*.mp4")))
                    job.update(state="done", message=f"done: {n} clip(s) are in the Review queue")
            except Exception as e:
                job.update(state="failed", message=str(e)[:300])
        threading.Thread(target=run, daemon=True).start()
        return jid

    # ---- review
    def reviews(self):
        try:
            return json.loads(self.review_path.read_text())
        except (OSError, ValueError):
            return {}

    def set_review(self, cid, decision):
        if decision not in ("approved", "rejected", "pending"):
            raise ValueError("bad decision")
        with self.lock:
            r = self.reviews()
            r[cid] = dict(decision=decision, at=watch.stamp())
            tmp = self.review_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(r, indent=2))
            tmp.replace(self.review_path)

    # ---- publishing (YouTube)
    def upload_state(self):
        st = self.pub.settings.get()
        try:
            import googleapiclient  # noqa: F401
            libs = True
        except ImportError:
            libs = False
        tt = platforms.load().get("tiktok", {})
        return dict(settings=st, game=self.pub.meta.game(), libs=libs, secret=bool(publisher.secret_path()), connected=publisher.TOKEN.exists(),
                    platforms=platforms.status(), tiktok_mode=tt.get("mode", "direct"), connect=self.yt_connect, log=self.pub.log.all()[-50:][::-1])

    def upload_clip(self, cid, privacy=None):
        clips = {c["id"]: c for c in self.clips()}
        c = clips.get(cid)
        if not c:
            raise ValueError("clip not found")
        if c["review"] == "rejected":
            raise ValueError("this clip was rejected")
        if not self.pub.settings.get()["targets"]:
            raise ValueError("no upload targets selected in the Upload tab")
        return self.pub.upload_targets(cid, self.safe(cid), c["review"], c, source="manual", privacy=privacy)

    # ---- generated title / description / hashtags / thumbnail (editable before upload)
    def _clip(self, cid):
        c = {x["id"]: x for x in self.clips()}.get(cid)
        if not c:
            raise ValueError("clip not found")
        return c

    def meta_get(self, cid):
        c = self._clip(cid)
        pack = publisher.build_pack(self.safe(cid), c, self.pub.meta, cid)
        th = pack.pop("thumbnail")
        pack["thumb"] = self.rel(th) if th else None
        pack["thumb_v"] = int(Path(th).stat().st_mtime) if th else 0
        return pack

    def meta_save(self, cid, fields):
        self._clip(cid)
        self.pub.meta.save(cid, fields)
        return self.meta_get(cid)

    def meta_reset(self, cid):
        self._clip(cid)
        self.pub.meta.reset(cid)
        return self.meta_get(cid)

    def meta_thumb(self, cid, headline):
        import metadata
        c = self._clip(cid)
        headline = re.sub(r"\s+", " ", str(headline or "")).strip()[:40]
        if not headline:
            raise ValueError("type a short headline for the thumbnail")
        self.pub.meta.save(cid, dict(headline=headline))
        if not metadata.ensure_thumbnail(self.safe(cid), c, headline, force=True):
            raise ValueError("could not make a thumbnail (needs ffmpeg and Pillow)")
        return self.meta_get(cid)

    PLATFORM_FIELDS = dict(facebook=("page_id", "token"), instagram=("ig_id", "token"),
                           tiktok=("client_key", "client_secret", "mode"))

    def save_platform(self, name, fields):
        allowed = self.PLATFORM_FIELDS.get(name)
        if not allowed:
            raise ValueError("unknown platform")
        vals = {k: str(v).strip()[:600] for k, v in (fields or {}).items() if k in allowed}
        if vals.get("mode") not in (None, "direct", "inbox"):
            raise ValueError("bad TikTok mode")
        platforms.set_section(name, **vals)

    def connect_tiktok(self):
        if self.yt_connect.get("tt") == "running":
            raise ValueError("already waiting for the TikTok login window")
        self.yt_connect = dict(self.yt_connect, tt="running", message="a TikTok login page should open in your browser")

        def run():
            try:
                platforms.tiktok_connect()
                self.yt_connect = dict(self.yt_connect, tt="done", message="TikTok connected")
            except Exception as e:
                self.yt_connect = dict(self.yt_connect, tt="failed", message=str(e)[:300])
        threading.Thread(target=run, daemon=True).start()

    def connect_youtube(self):
        if self.yt_connect["state"] == "running":
            raise ValueError("already waiting for the Google login window")
        if not publisher.secret_path():
            raise ValueError("client_secret.json not found. See the setup steps in the Upload tab.")
        self.yt_connect = dict(state="running", message="a Google login page should open in your browser")

        def run():
            try:
                publisher.credentials(interactive=True)
                self.yt_connect = dict(state="done", message="connected")
            except Exception as e:
                self.yt_connect = dict(state="failed", message=str(e)[:300])
        threading.Thread(target=run, daemon=True).start()

    def start_autopilot_loop(self, every=30):
        def loop():
            while True:
                try:
                    self.pub.autopilot_pass(self.clips(), self.safe)
                except Exception:
                    pass
                time.sleep(every)
        self.autopilot_thread = threading.Thread(target=loop, daemon=True)
        self.autopilot_thread.start()

    # ---- files
    def safe(self, rel):
        p = (self.home / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        for root in self.roots:
            if p == root or root in p.parents:
                return p
        raise PermissionError("outside allowed folders")

    def rel(self, p):
        p = Path(p).resolve()
        for root in self.roots:
            if root in p.parents:
                return str(p.relative_to(root)) if root == self.home else str(p)
        return str(p)

    # ---- clips
    def clips(self):
        out, rv = [], self.reviews()
        for root in self.roots:
            for mp4 in root.rglob("*.mp4"):
                n = mp4.name
                if "work" in mp4.relative_to(root).parts or n.startswith("sec_") or n.endswith("_clean.mp4"):
                    continue
                meta, score, axes, reason = {}, None, None, ""
                if n.endswith("_final.mp4"):
                    j = mp4.with_name(n[:-len("_final.mp4")] + ".json")
                    if j.exists():
                        try:
                            meta = json.loads(j.read_text())
                        except ValueError:
                            pass
                    score, reason = meta.get("score"), meta.get("trigger", "")
                elif n.startswith("vod_"):
                    m = re.match(r"vod_(\d+)_", n)
                    sl = mp4.with_name("shortlist.json")
                    if m and sl.exists():
                        try:
                            items = json.loads(sl.read_text())
                            it = items[int(m[1]) - 1]
                            score, axes, reason = it.get("score"), it.get("axes"), it.get("reason", "")
                            meta = dict(start=it.get("start"), end=it.get("end"), channel=it.get("channel"),
                                        source=it.get("source"), video_title=it.get("video_title"))
                        except (ValueError, IndexError):
                            pass
                else:
                    continue
                cid = self.rel(mp4)
                thumb = meta.get("thumbnail")
                out.append(dict(id=cid, name=n, score=score, axes=axes, reason=reason, hook=meta.get("hook"),
                                title=meta.get("hook") or meta.get("caption"), meta=meta,
                                thumb=self.rel(mp4.with_name(thumb)) if thumb and mp4.with_name(thumb).exists() else None,
                                mtime=mp4.stat().st_mtime, review=rv.get(cid, {}).get("decision", "pending"),
                                kind="vod" if n.startswith("vod_") else "live"))
        out.sort(key=lambda c: -c["mtime"])
        return out

    # ---- watcher process
    def watcher_running(self):
        if self.proc and self.proc.poll() is None:
            return True
        pidf = self.home / "watcher.pid"
        if pidf.exists():
            try:
                os.kill(int(pidf.read_text()), 0)
                return True
            except (OSError, ValueError):
                pidf.unlink(missing_ok=True)
        return False

    def start_watcher(self, extra=()):
        if self.watcher_running():
            return
        env = dict(os.environ, LIVECLIPPER_WATCH_HOME=str(self.home))
        logf = open(self.home / "watcher.out", "a")
        self.proc = subprocess.Popen([sys.executable, str(HERE / "watch.py"), "run", "--home", str(self.home), *extra],
                                     stdout=logf, stderr=subprocess.STDOUT, env=env)
        (self.home / "watcher.pid").write_text(str(self.proc.pid))

    def stop_watcher(self):
        pidf = self.home / "watcher.pid"
        pid = None
        if self.proc and self.proc.poll() is None:
            pid = self.proc.pid
        elif pidf.exists():
            try:
                pid = int(pidf.read_text())
            except ValueError:
                pass
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        pidf.unlink(missing_ok=True)

    def state(self):
        return dict(channels=self.store.channels(), state=self.store.state(), watcher=self.watcher_running(),
                    gates=dict(review_required=True, autopilot=self.pub.settings.get()["autopilot"]))

    def log_tail(self, n=120):
        p = self.home / "watch.log"
        try:
            return "".join(p.read_text().splitlines(True)[-n:])
        except OSError:
            return ""


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "liveclipper-ui"

        def log_message(self, *a):
            pass

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj))

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "bad host", "text/plain")
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, PAGE.replace("__TOKEN__", app.token), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json(app.state())
            if u.path == "/api/clips":
                return self._json(app.clips())
            if u.path == "/api/jobs":
                return self._json(list(app.jobs.values()))
            if u.path == "/api/upload":
                return self._json(app.upload_state())
            if u.path == "/api/meta":
                try:
                    return self._json(app.meta_get(parse_qs(u.query).get("id", [""])[0]))
                except (ValueError, PermissionError) as e:
                    return self._json(dict(error=str(e)), 400)
            if u.path == "/api/log":
                return self._json(dict(log=app.log_tail()))
            if u.path == "/file":
                return self._file(unquote(parse_qs(u.query).get("p", [""])[0]))
            self._send(404, "not found", "text/plain")

        def _file(self, rel):
            try:
                p = app.safe(rel)
            except PermissionError:
                return self._send(403, "forbidden", "text/plain")
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXT | {".jpg", ".jpeg", ".png"}:
                return self._send(404, "not found", "text/plain")
            size = p.stat().st_size
            ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            start, end, code = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if m and (m[1] or m[2]):
                if m[1]:
                    start = int(m[1])
                    end = int(m[2]) if m[2] else size - 1
                else:
                    start = max(size - int(m[2]), 0)
                end = min(end, size - 1)
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-LC-Token") != app.token:
                return self._send(403, "forbidden", "text/plain")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 100000)) or b"{}")
            except ValueError:
                return self._json(dict(error="bad json"), 400)
            u = urlparse(self.path).path
            try:
                if u == "/api/channels/add":
                    ch = watch.norm_channel(str(body.get("channel", "")))
                    chans = [c for c in app.store.channels() if c["name"] != ch["name"]] + [ch]
                    app.store.save_channels(chans)
                    return self._json(dict(ok=True, channel=ch))
                if u == "/api/channels/remove":
                    app.store.save_channels([c for c in app.store.channels() if c["name"] != body.get("name")])
                    return self._json(dict(ok=True))
                if u == "/api/watcher/start":
                    app.start_watcher()
                    return self._json(dict(ok=True))
                if u == "/api/watcher/stop":
                    app.stop_watcher()
                    return self._json(dict(ok=True))
                if u == "/api/ytclip":
                    jid = app.start_clip_job(str(body.get("url", "")), body.get("start"), body.get("end"),
                                             str(body.get("layout", "blur")))
                    return self._json(dict(ok=True, id=jid))
                if u == "/api/upload/settings":
                    return self._json(dict(ok=True, settings=app.pub.settings.update(
                        autopilot=body.get("autopilot"), privacy=body.get("privacy"), rights_ack=body.get("rights_ack"),
                        targets=body.get("targets"))))
                if u == "/api/platform/save":
                    app.save_platform(str(body.get("platform", "")), body.get("fields"))
                    return self._json(dict(ok=True))
                if u == "/api/vodjob":
                    jid = app.start_find_job(body.get("url", ""), body.get("top", 5), body.get("layout", "blur"),
                                             body.get("cookies") or None, body.get("latest", 1), body.get("range") or None)
                    return self._json(dict(ok=True, id=jid))
                if u == "/api/meta/save":
                    return self._json(app.meta_save(str(body.get("id", "")), body.get("fields") or {}))
                if u == "/api/meta/reset":
                    return self._json(app.meta_reset(str(body.get("id", ""))))
                if u == "/api/meta/thumb":
                    return self._json(app.meta_thumb(str(body.get("id", "")), body.get("headline")))
                if u == "/api/meta/game":
                    app.pub.meta.set_game(str(body.get("game", "")))
                    return self._json(dict(ok=True))
                if u == "/api/upload/clip":
                    return self._json(dict(ok=True, results=app.upload_clip(str(body.get("id", "")), body.get("privacy"))))
                if u == "/api/upload/connect":
                    app.connect_tiktok() if body.get("platform") == "tiktok" else app.connect_youtube()
                    return self._json(dict(ok=True))
                if u == "/api/review":
                    app.set_review(str(body.get("id", "")), str(body.get("decision", "")))
                    return self._json(dict(ok=True))
            except SystemExit as e:
                return self._json(dict(error=str(e)), 400)
            except (ValueError, TypeError, PermissionError) as e:
                return self._json(dict(error=str(e)), 400)
            self._send(404, "not found", "text/plain")
    return H


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>liveclipper</title><style>
:root{--bg:#0e0f13;--card:#171922;--line:#262a36;--tx:#e8eaf0;--mut:#8d93a5;--acc:#ffd400;--ok:#2fd27a;--bad:#ff5d5d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:17px;margin:0}h1 b{color:var(--acc)}nav{display:flex;gap:4px;margin-left:12px}
nav button{background:none;border:0;color:var(--mut);padding:7px 12px;border-radius:7px;cursor:pointer;font:inherit}
nav button.on{background:var(--card);color:var(--tx)}main{padding:22px;max-width:1200px;margin:auto}
.pill{font-size:12px;padding:2px 9px;border-radius:99px;background:var(--card);border:1px solid var(--line)}
.live{color:var(--ok);border-color:var(--ok)}.blocked,.capture_blocked,.live_blocked{color:var(--bad);border-color:var(--bad)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}input{background:var(--card);border:1px solid var(--line);color:var(--tx);padding:8px 10px;border-radius:7px;min-width:360px;font:inherit}
button.b{background:var(--acc);color:#111;border:0;padding:8px 14px;border-radius:7px;font-weight:600;cursor:pointer}
button.g{background:var(--card);color:var(--tx);border:1px solid var(--line);padding:7px 12px;border-radius:7px;cursor:pointer}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:14px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;margin-top:16px}
table{width:100%;border-collapse:collapse;margin-top:14px}td,th{text-align:left;padding:10px;border-bottom:1px solid var(--line)}th{color:var(--mut);font-weight:500}
video{width:100%;border-radius:8px;background:#000;aspect-ratio:9/16;object-fit:cover}.sc{font-size:22px;font-weight:700;color:var(--acc)}
.bar{height:5px;background:var(--line);border-radius:3px;margin:3px 0 7px}.bar i{display:block;height:100%;background:var(--acc);border-radius:3px}
.axl{display:flex;justify-content:space-between;font-size:11px;color:var(--mut)}.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto;max-height:60vh;font-size:12px}
.gate{background:#1d1a0c;border:1px solid #4a410f;color:#e9d77a;border-radius:9px;padding:9px 13px;margin-bottom:18px}
.tabs{display:none}.tabs.on{display:block}
</style></head><body>
<header><h1>live<b>clipper</b></h1><nav id="nav"></nav><span style="flex:1"></span><span class="pill" id="wstat">watcher</span></header>
<main>
<div class="gate" id="gate">Review gate is on. Approving only records your decision. Clips upload only when you press Upload (or turn on autopilot in the Upload tab, which is off by default).</div>
<section id="t-channels" class="tabs"></section><section id="t-clips" class="tabs"></section>
<section id="t-review" class="tabs"></section><section id="t-manual" class="tabs"></section><section id="t-upload" class="tabs"></section><section id="t-logs" class="tabs"></section>
</main><script>
const T="__TOKEN__",$=s=>document.querySelector(s);let tab=(location.hash.slice(1)||"channels"),STATE={},CLIPS=[];
const api=(p,b)=>fetch(p,b?{method:"POST",headers:{"X-LC-Token":T,"Content-Type":"application/json"},body:JSON.stringify(b)}:{}).then(r=>r.json());
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const file=p=>"/file?p="+encodeURIComponent(p);
const tabs=[["channels","Channels"],["clips","Clips"],["review","Review queue"],["manual","Clip a video"],["upload","Upload"],["logs","Logs"]];
$("#nav").innerHTML=tabs.map(([k,v])=>`<button data-k="${k}">${v}</button>`).join("");
$("#nav").onclick=e=>{if(e.target.dataset.k){tab=e.target.dataset.k;draw()}};
async function load(){STATE=await api("/api/state");CLIPS=await api("/api/clips");draw()}
function draw(){document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.dataset.k==tab));
 document.querySelectorAll(".tabs").forEach(s=>s.classList.toggle("on",s.id=="t-"+tab));
 $("#wstat").textContent=STATE.watcher?"watcher running":"watcher stopped";$("#wstat").className="pill "+(STATE.watcher?"live":"");
 if(tab=="channels")chan();if(tab=="clips")clips();if(tab=="review")review();if(tab=="manual")manual();if(tab=="upload")upload();if(tab=="logs")logs()}
function chan(){const st=STATE.state||{},ch=STATE.channels||[];
 $("#t-channels").innerHTML=`<div class="row"><input id="nc" placeholder="twitch:name or @youtubehandle or YouTube URL"><button class="b" id="add">Watch channel</button>
 <span style="flex:1"></span><button class="g" id="ws">${STATE.watcher?"Stop watcher":"Start watcher"}</button></div>
 <table><tr><th>Channel</th><th>Platform</th><th>Status</th><th>Last check</th><th></th></tr>${ch.map(c=>{const s=st[c.name]||{};
 return `<tr><td>${esc(c.name)}</td><td>${esc(c.platform)}</td><td><span class="pill ${esc(s.status)}">${esc(s.status||"not checked yet")}</span> ${s.capturing?'<span class="pill live">clipping</span>':""}</td><td class="mut">${esc(s.checked||"-")}</td><td><button class="g" data-rm="${esc(c.name)}">Remove</button></td></tr>`}).join("")||'<tr><td colspan="5" class="mut">No channels yet. Add one above; the watcher starts clipping when it goes live.</td></tr>'}</table>
 <p class="mut">"live, capture blocked" means the channel is live but video could not be recorded on this computer. Detection and capture are checked separately.</p>`;
 $("#add").onclick=async()=>{const r=await api("/api/channels/add",{channel:$("#nc").value});if(r.error)alert(r.error);load()};
 $("#ws").onclick=async()=>{await api(STATE.watcher?"/api/watcher/stop":"/api/watcher/start",{});setTimeout(load,800)};
 document.querySelectorAll("[data-rm]").forEach(b=>b.onclick=async()=>{await api("/api/channels/remove",{name:b.dataset.rm});load()})}
const AX=[["hook",30],["flow",30],["value",25],["trend",15]];
function card(c,actions){const ax=c.axes?AX.map(([k,m])=>`<div class="axl"><span>${k}</span><span>${c.axes[k]}/${m}</span></div><div class="bar"><i style="width:${Math.min(100,100*c.axes[k]/m)}%"></i></div>`).join(""):"";
 return `<div class="card"><video controls preload="metadata" ${c.thumb?`poster="${file(c.thumb)}"`:""} src="${file(c.id)}#t=0.1"></video>
 <div class="row" style="justify-content:space-between;margin-top:8px"><span class="sc">${c.score??"-"}</span><span class="pill">${esc(c.kind)}</span></div>
 <div style="font-weight:600">${esc(c.title||c.name)}</div><div class="mut" style="margin-bottom:8px">${esc(c.reason)}</div>${ax}${actions?`<div class="row">${actions}<button class="g" data-m="${esc(c.id)}">Title, description, thumbnail</button></div><div class="mp" data-mp="${esc(c.id)}"></div>`:""}</div>`}
function wireMeta(){document.querySelectorAll("[data-m]").forEach(b=>b.onclick=async()=>{const id=b.dataset.m,box=document.querySelector('[data-mp="'+CSS.escape(id)+'"]');if(box.innerHTML){box.innerHTML="";return}box.innerHTML='<div class="mut">generating...</div>';const m=await api("/api/meta?id="+encodeURIComponent(id));if(m.error){box.innerHTML='<div class="mut">'+esc(m.error)+'</div>';return}drawMeta(box,id,m)})}
function drawMeta(box,id,m){box.innerHTML=`<div style="margin-top:8px"><div class="mut">Generated from the clip. Edit anything; edits are used for every platform and by autopilot.${m.edited?" (you edited this)":""}</div>
 ${m.thumb?`<img src="${file(m.thumb)}?v=${m.thumb_v}" style="width:100%;border-radius:8px;margin:6px 0">`:'<div class="mut">No thumbnail yet.</div>'}
 <div class="row"><input class="mh" value="${esc(m.headline)}" maxlength="40" style="flex:1" placeholder="thumbnail headline"><button class="g mt">Remake thumbnail</button></div>
 <label class="mut">Title (${m.title.length}/100)</label><input class="mti" value="${esc(m.title)}" maxlength="100" style="width:100%">
 <label class="mut">Description</label><textarea class="mde" rows="8" style="width:100%">${esc(m.description)}</textarea>
 <label class="mut">Keywords / tags (comma separated)</label><input class="mtg" value="${esc(m.tags.join(", "))}" style="width:100%">
 <label class="mut">Caption for TikTok, Instagram, Facebook</label><textarea class="mca" rows="3" style="width:100%">${esc(m.caption)}</textarea>
 <div class="row" style="margin-top:6px"><button class="b ms">Save</button><button class="g mr">Reset to generated</button></div></div>`;
 const f=()=>({title:box.querySelector(".mti").value,description:box.querySelector(".mde").value,tags:box.querySelector(".mtg").value,caption:box.querySelector(".mca").value,headline:box.querySelector(".mh").value});
 const done=r=>{if(r.error)alert(r.error);else drawMeta(box,id,r)};
 box.querySelector(".ms").onclick=async()=>done(await api("/api/meta/save",{id,fields:f()}));
 box.querySelector(".mr").onclick=async()=>done(await api("/api/meta/reset",{id}));
 box.querySelector(".mt").onclick=async()=>{await api("/api/meta/save",{id,fields:f()});const r=await api("/api/meta/thumb",{id,headline:box.querySelector(".mh").value});if(r.error)alert(r.error);else drawMeta(box,id,r)}}
function clips(){setTimeout(wireMeta,0);$("#t-clips").innerHTML=`<div class="mut">${CLIPS.length} clip(s), newest first. Live-clip scores are chat-spike strength; VOD scores are 0-100 (Hook, Flow, Value, Trend).</div><div class="grid">${CLIPS.map(c=>card(c,`<span class="pill">${esc(c.review)}</span>`)).join("")||'<div class="mut">No clips yet.</div>'}</div>`}
function review(){setTimeout(wireMeta,0);const q=CLIPS.filter(c=>c.review=="pending");
 $("#t-review").innerHTML=`<div class="mut">${q.length} waiting for your decision.</div><div class="grid">${q.map(c=>card(c,`<button class="b" data-d="approved" data-id="${esc(c.id)}">Approve</button><button class="g" data-d="rejected" data-id="${esc(c.id)}">Reject</button><button class="g" data-u="${esc(c.id)}">Approve + upload</button>`)).join("")}</div>`;
 document.querySelectorAll("[data-u]").forEach(b=>b.onclick=async()=>{if(!confirm("Approve and upload this clip now to: "+((STATE.settings&&STATE.settings.targets)||["selected platforms"]).join(", ")+" ? Facebook and Instagram post publicly."))return;b.disabled=true;const mb=document.querySelector('[data-mp="'+CSS.escape(b.dataset.u)+'"]');if(mb&&mb.querySelector(".mti"))await api("/api/meta/save",{id:b.dataset.u,fields:{title:mb.querySelector(".mti").value,description:mb.querySelector(".mde").value,tags:mb.querySelector(".mtg").value,caption:mb.querySelector(".mca").value}});await api("/api/review",{id:b.dataset.u,decision:"approved"});const r=await api("/api/upload/clip",{id:b.dataset.u});alert(r.error?"Upload failed: "+r.error:Object.entries(r.results).map(([k,v])=>k+": "+v).join("\n")||"Nothing to upload (already uploaded everywhere)");load()});
 document.querySelectorAll("[data-d]").forEach(b=>b.onclick=async()=>{await api("/api/review",{id:b.dataset.id,decision:b.dataset.d});load()})}
let PL=null,DUR=0,CURURL="";
function ytid(u){const m=u.match(/(?:youtube\.com\/(?:watch\?(?:[^#]*&)?v=|live\/|shorts\/)|youtu\.be\/)([\w-]{11})/);return m&&m[1]}
function fmt(t){t=Math.round(t);return Math.floor(t/60)+":"+String(t%60).padStart(2,"0")}
function manual(){if($("#mu"))return;
 $("#t-manual").innerHTML=`<div class="gate">Downloads happen on this computer, so this only works where YouTube lets your computer fetch the video. Only clip videos you own or have permission to use. Clips go to the Review queue.</div>
 <div class="row"><input id="mu" placeholder="YouTube link (a finished video or past stream)"><button class="b" id="ml">Load video</button></div>
 <div id="mbox" style="display:none;margin-top:16px;max-width:760px"><div id="mp" style="aspect-ratio:16/9;background:#000;border-radius:10px;overflow:hidden"></div>
 <div class="row" style="margin-top:12px"><label>Start <input type="range" id="ms" min="0" value="0" style="min-width:220px"></label><b id="mst">0:00</b>
 <label>End <input type="range" id="me" min="1" value="30" style="min-width:220px"></label><b id="met">0:30</b></div>
 <div class="row" style="margin-top:10px"><span class="mut" id="mlen"></span><select id="mlay" class="g"><option value="blur">Vertical, blurred fill</option><option value="crop">Vertical, crop</option><option value="stack">Gameplay + webcam stacked</option></select>
 <button class="b" id="mgo">Clip it</button></div></div><div class="card" style="max-width:760px;margin-top:18px"><h3 style="margin-top:0">Find the best moments in a whole video</h3>
 <p class="mut">Paste a YouTube video, a past stream, or a channel (@handle, uses its newest past broadcasts). The tool scores the whole thing (audio energy, speech, scene cuts) and renders the top moments into the Review queue. Long streams take a while; use a range for part of one.</p>
 <div class="row"><input id="fu" placeholder="https://www.youtube.com/watch?v=...  or  @KaiCenat" style="flex:1;min-width:260px"></div>
 <div class="row" style="margin-top:8px"><label>Clips <input id="ft" type="number" value="5" min="1" max="15" style="width:70px;min-width:0"></label>
 <label>Newest videos (channel) <input id="fl" type="number" value="1" min="1" max="5" style="width:70px;min-width:0"></label>
 <label>Range s <input id="fr" placeholder="3600-7200" style="width:110px;min-width:0"></label>
 <label>Layout <select id="fy" class="g"><option value="blur">Blurred fill</option><option value="crop">Crop</option><option value="stack">Gameplay + webcam</option></select></label>
 <label>Signed-in browser <select id="fc" class="g"><option value="">none</option><option>chrome</option><option>safari</option><option>firefox</option><option>edge</option><option>brave</option></select></label>
 <button class="b" id="fgo">Find best moments</button></div></div><div id="mjobs" style="margin-top:18px"></div>`;
 $("#fgo").onclick=async()=>{const r=await api("/api/vodjob",{url:$("#fu").value,top:+$("#ft").value,latest:+$("#fl").value,range:$("#fr").value,layout:$("#fy").value,cookies:$("#fc").value});if(r.error)alert(r.error);jobs()};
 $("#ml").onclick=()=>{const id=ytid($("#mu").value.trim());if(!id){alert("Paste a normal YouTube video link");return}CURURL=$("#mu").value.trim();
  $("#mbox").style.display="block";const init=()=>{if(PL)PL.destroy();PL=new YT.Player("mp",{videoId:id,playerVars:{playsinline:1},events:{onReady:e=>{DUR=Math.floor(e.target.getDuration());
   $("#ms").max=DUR;$("#me").max=DUR;$("#ms").value=0;$("#me").value=Math.min(DUR,30);upd()}}})};
  if(window.YT&&window.YT.Player)init();else{window.onYouTubeIframeAPIReady=init;const s=document.createElement("script");s.src="https://www.youtube.com/iframe_api";document.head.appendChild(s)}};
 const upd=()=>{let a=+$("#ms").value,b=+$("#me").value;if(b<=a){b=Math.min(DUR,a+1);$("#me").value=b}$("#mst").textContent=fmt(a);$("#met").textContent=fmt(b);
  $("#mlen").textContent=(b-a)+" s"+((b-a)>180?" (max 180)":"");if(PL&&PL.seekTo&&document.activeElement.id=="ms")PL.seekTo(a,true)};
 $("#ms").oninput=upd;$("#me").oninput=upd;
 $("#mgo").onclick=async()=>{const r=await api("/api/ytclip",{url:CURURL,start:+$("#ms").value,end:+$("#me").value,layout:$("#mlay").value});if(r.error)alert(r.error);jobs()};
 jobs()}
async function jobs(){const j=await api("/api/jobs");const el=$("#mjobs");if(!el)return;el.innerHTML=j.map(x=>`<div class="card" style="margin-bottom:8px"><span class="pill ${x.state=="done"?"live":x.state=="failed"?"blocked":""}">${x.state}</span> ${esc(x.url)} ${x.end?fmt(x.start)+"-"+fmt(x.end):""}<div class="mut">${esc(x.message)}</div></div>`).join("")}
async function upload(){const u=await api("/api/upload"),s=u.settings;
 const ok=u.libs&&u.secret&&u.connected;
 $("#t-upload").innerHTML=`<div class="card" style="max-width:820px"><h3 style="margin-top:0">YouTube</h3>
 <div class="row"><span class="pill ${u.connected?"live":"blocked"}">${u.connected?"connected":"not connected"}</span>
 <span class="mut">Google libraries: ${u.libs?"installed":"missing - run pip3 install google-api-python-client google-auth-oauthlib"} | client_secret.json: ${u.secret?"found":"missing"}</span></div>
 ${u.secret&&u.libs?`<p><button class="b" id="uc">${u.connected?"Reconnect YouTube":"Connect YouTube"}</button> <span class="mut">${esc(u.connect.message||"")}</span></p>`:`<p class="mut">Setup (once, about 10 minutes): in Google Cloud Console create a project, enable "YouTube Data API v3", set up the OAuth consent screen (add yourself as a test user), create an OAuth client of type "Desktop app", download it, and save it as <code>~/.config/liveclipper/client_secret.json</code>. Never share or commit that file. Then reload this page.</p>`}
 <p class="mut">Uploads go to your own channel. Google's default quota is 100 uploads a day. Older rules locked uploads from unaudited API projects to private; Google's current docs do not mention that, so check the first upload in YouTube Studio and change its visibility there if it came out private.</p></div>
 <div class="card" style="max-width:820px;margin-top:16px"><h3 style="margin-top:0">Facebook, Instagram, TikTok</h3>
 <p class="mut">Setup steps and approvals for each are in PLATFORMS.md. Keys are saved only on this computer (~/.config/liveclipper/platforms.json, owner-only) and never shown again. Facebook and Instagram posts are PUBLIC as soon as they publish. These three are written from the platforms' docs and not yet tested against the real services.</p>
 <div class="row"><b style="min-width:90px">Facebook</b><span class="pill ${u.platforms.facebook?"live":"blocked"}">${u.platforms.facebook?"set up":"not set up"}</span><input id="fbp" placeholder="Page ID" style="min-width:140px"><input id="fbt" type="password" placeholder="Page access token" style="min-width:200px"><button class="g" id="fbs">Save</button></div>
 <div class="row" style="margin-top:8px"><b style="min-width:90px">Instagram</b><span class="pill ${u.platforms.instagram?"live":"blocked"}">${u.platforms.instagram?"set up":"not set up"}</span><input id="igi" placeholder="Instagram account ID" style="min-width:140px"><input id="igt" type="password" placeholder="Access token" style="min-width:200px"><button class="g" id="igs">Save</button></div>
 <div class="row" style="margin-top:8px"><b style="min-width:90px">TikTok</b><span class="pill ${u.platforms.tiktok?"live":"blocked"}">${u.platforms.tiktok?"connected":"not connected"}</span><input id="tkk" placeholder="Client key" style="min-width:140px"><input id="tks" type="password" placeholder="Client secret" style="min-width:200px"><select id="tkm" class="g"><option value="direct">Post to profile (private until TikTok audits your app)</option><option value="inbox">Send to my TikTok inbox, I tap Post</option></select><button class="g" id="tkv">Save</button><button class="b" id="tkc" ${u.platforms.tiktok_app?"":"disabled"}>Log in to TikTok</button></div>
 <p class="mut">TikTok app settings: register the redirect URI exactly as <b>http://127.0.0.1:8766/callback/</b> (trailing slash included). Port 8766 must be free when you click "Log in to TikTok".</p>
 <p class="mut" id="pmsg">${esc(u.connect.message||"")}</p>
 <div style="margin-top:10px"><b>Upload to:</b> ${["youtube","facebook","instagram","tiktok"].map(p=>`<label style="margin-left:12px"><input type="checkbox" class="tg" value="${p}" ${s.targets.includes(p)?"checked":""} style="min-width:0"> ${p}</label>`).join("")} <button class="g" id="tgs">Save targets</button></div></div>
 <div class="card" style="max-width:820px;margin-top:16px"><h3 style="margin-top:0">Titles and hashtags</h3>
 <p class="mut">Every clip gets a generated title, description, keywords, hashtags and thumbnail (never the file name). Edit them per clip in the Review queue; autopilot uses the generated text unless you edited it. Game used in titles and hashtags:</p>
 <div class="row"><input id="gm" value="${esc(u.game)}" style="min-width:160px"><button class="g" id="gms">Save game</button></div></div>
 <div class="card" style="max-width:820px;margin-top:16px"><h3 style="margin-top:0">Autopilot</h3>
 <p class="mut">Off by default. When on, clips upload by themselves (checked every 30 s) while this panel is running. Each upload is a publish as you, so only turn it on for channels whose clips you have the right to use.</p>
 <label><input type="checkbox" id="ra" ${s.rights_ack?"checked":""} style="min-width:0"> I own the content I clip or have permission to use and post it (confirmed ${esc(s.rights_ack||"not yet")})</label>
 <div class="row" style="margin-top:12px"><label>Mode <select id="am" class="g"><option value="off">Off - upload only when I press the button</option><option value="approved">Upload clips I approved</option><option value="all">Upload every new clip except rejected</option></select></label>
 <label>Privacy <select id="ap" class="g"><option value="private">private</option><option value="unlisted">unlisted</option><option value="public">public</option></select></label>
 <button class="b" id="as">Save</button></div></div>
 <h3 style="margin-top:22px">Upload log</h3><table><tr><th>When</th><th>Where</th><th>Status</th><th>Title</th><th>Result</th></tr>${u.log.map(r=>`<tr><td>${esc(r.at)}</td><td>${esc(r.platform)} (${esc(r.privacy)}, ${esc(r.by)})</td><td class="${r.status=="uploaded"?"ok":"bad"}">${esc(r.status)}</td><td>${esc(r.title)}</td><td>${esc(r.url||r.error||"")}</td></tr>`).join("")||'<tr><td colspan="5" class="mut">Nothing uploaded yet.</td></tr>'}</table>`;
 $("#tkm").value=u.tiktok_mode;
 const sv=async(p,f)=>{const r=await api("/api/platform/save",{platform:p,fields:f});if(r.error)alert(r.error);upload()};
 $("#fbs").onclick=()=>sv("facebook",{page_id:$("#fbp").value,token:$("#fbt").value});
 $("#igs").onclick=()=>sv("instagram",{ig_id:$("#igi").value,token:$("#igt").value});
 $("#tkv").onclick=()=>sv("tiktok",{client_key:$("#tkk").value,client_secret:$("#tks").value,mode:$("#tkm").value});
 $("#tkc").onclick=async()=>{const r=await api("/api/upload/connect",{platform:"tiktok"});if(r.error)alert(r.error);setTimeout(upload,5000)};
 $("#tgs").onclick=async()=>{const r=await api("/api/upload/settings",{targets:[...document.querySelectorAll(".tg:checked")].map(x=>x.value)});if(r.error)alert(r.error);upload()};
 $("#am").value=s.autopilot;$("#ap").value=s.privacy;
 if($("#uc"))$("#uc").onclick=async()=>{const r=await api("/api/upload/connect",{});if(r.error)alert(r.error);setTimeout(upload,4000)};
 $("#gms").onclick=async()=>{const r=await api("/api/meta/game",{game:$("#gm").value});if(r.error)alert(r.error);upload()};
 $("#as").onclick=async()=>{let r=await api("/api/upload/settings",{rights_ack:$("#ra").checked});if(!r.error)r=await api("/api/upload/settings",{privacy:$("#ap").value,autopilot:$("#am").value});if(r.error)alert(r.error);upload()}}
async function logs(){const r=await api("/api/log");$("#t-logs").innerHTML=`<pre>${esc(r.log||"No log yet.")}</pre>`}
load();setInterval(()=>{if(tab=="manual")jobs();else if(tab=="upload"){}else if(tab!="clips"&&tab!="review")load();else if(!document.querySelector(".mp:not(:empty)"))api("/api/state").then(s=>{STATE=s;draw()})},10000);
</script></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--home", default=str(watch.HOME))
    ap.add_argument("--also", action="append", default=[], help="extra folder to scan for clips (e.g. clips, clips_vod)")
    a = ap.parse_args(argv)
    Path(a.home).mkdir(parents=True, exist_ok=True)
    extra = [p for p in ["clips", "clips_vod"] + a.also if Path(p).is_dir()]
    app = App(a.home, extra)
    app.start_autopilot_loop()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
    print(f"liveclipper UI: http://127.0.0.1:{a.port}   (this computer only; Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
