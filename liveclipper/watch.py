#!/usr/bin/env python3
"""
watch.py - add a channel once; it watches around the clock and starts the clipper the moment the channel goes live.

  python3 watch.py add twitch:NAME        # or @youtubehandle, a YouTube /live URL, or a bare Twitch name
  python3 watch.py list | status | remove NAME
  python3 watch.py run [--interval 60] [--max 3] [--webhook URL] [-- extra liveclipper.py flags]
  python3 watch.py once                    # one check pass (for cron/testing)

Live detection is separate from capture, so the watcher still reports "live, capture blocked" when YouTube
refuses the video. Clips go to watch_data/channels/<name>/<date>/ for review. Nothing is uploaded.
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("LIVECLIPPER_WATCH_HOME", "watch_data"))


def now():
    return time.time()


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- live checks (each returns 'live' | 'offline' | 'blocked' | 'unknown') ----------------
def check_twitch(name):
    if shutil.which("streamlink"):
        r = subprocess.run(["streamlink", "--json", f"twitch.tv/{name}"], capture_output=True, text=True, timeout=45)
        try:
            j = json.loads(r.stdout or "{}")
        except ValueError:
            return "unknown"
        if j.get("streams"):
            return "live"
        err = (j.get("error") or "").lower()
        return "offline" if ("no playable streams" in err or "offline" in err or not err) else "unknown"
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--simulate", "--no-warnings", f"https://www.twitch.tv/{name}"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode == 0:
        return "live"
    err = (r.stderr or "").lower()
    return "offline" if ("not currently live" in err or "offline" in err or "does not exist" in err) else "unknown"


def classify_youtube_page(page):
    """Pure function on the public /live page HTML. Never touches the video stream."""
    compact = page.replace(" ", "")
    canonical = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', page)
    if '"isLiveNow":true' in compact or ('"isLive":true' in compact and canonical):
        return "live"
    m = re.search(r'"playabilityStatus":\{"status":"(\w+)"(?:,"reason":"([^"]*)")?', page)
    status, reason = (m.group(1), (m.group(2) or "").lower()) if m else ("", "")
    if status == "LOGIN_REQUIRED" or "not a bot" in reason or "sign in to confirm" in reason:
        return "blocked"
    if status == "LIVE_STREAM_OFFLINE" or "live event will begin" in reason:
        return "offline"
    if canonical and status == "OK":
        return "offline"  # a normal, non-live video
    if "ytInitialData" in page or "ytInitialPlayerResponse" in page:
        return "offline"  # channel page with no live stream
    return "unknown"


def check_youtube(url):
    import liveclipper as lc
    try:
        return classify_youtube_page(lc.http(url))
    except Exception:
        return "unknown"


def default_check(ch):
    return check_twitch(ch["name"]) if ch["platform"] == "twitch" else check_youtube(ch["url"])


# ---------------- watchlist ----------------
def norm_channel(spec):
    import liveclipper as lc
    spec = spec.strip()
    if spec.lower().startswith("twitch:"):
        spec = spec.split(":", 1)[1]
    platform, url, name = lc.parse_target(spec)
    if platform == "direct":
        raise SystemExit("watch needs a Twitch channel or a YouTube channel/live URL, not a direct stream")
    return dict(target=spec, platform=platform, url=url, name=name)


class Store:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.wl, self.st = self.home / "watchlist.json", self.home / "state.json"

    def _load(self, p, default):
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return default

    def channels(self):
        return self._load(self.wl, [])

    def save_channels(self, chans):
        self.wl.write_text(json.dumps(chans, indent=2))

    def state(self):
        return self._load(self.st, {})

    def save_state(self, st):
        tmp = self.st.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        tmp.replace(self.st)

    def log(self, msg):
        line = f"{stamp()} {msg}"
        print(line, flush=True)
        with open(self.home / "watch.log", "a") as f:
            f.write(line + "\n")


class Watcher:
    FAST_EXIT_S = 60       # a clipper that dies this quickly while the channel is live counts as a failed start
    FAILS_BEFORE_BLOCKED = 3

    def __init__(self, store, check=default_check, clipper_cmd=None, extra=None, max_clippers=3, webhook=None,
                 vod_cmd=None, vod_delay=120, notify=None):
        self.s, self.check, self.extra = store, check, extra or []
        self.clipper_cmd = clipper_cmd or (lambda ch, out, work: [sys.executable, str(HERE / "liveclipper.py"), ch["target"],
                                                                  "--out", str(out), "--work", str(work)] + self.extra)
        self.vod_cmd = vod_cmd
        self.max, self.webhook, self.vod_delay = max_clippers, webhook, vod_delay
        self.procs, self.vods = {}, []
        self.notify = notify or self._notify

    def _notify(self, text):
        self.s.log("NOTIFY " + text)
        if self.webhook:
            try:
                import urllib.request
                req = urllib.request.Request(self.webhook, json.dumps({"content": text}).encode(),
                                             {"Content-Type": "application/json", "User-Agent": "liveclipper-watch"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                self.s.log(f"webhook failed: {e}")

    # -- process control
    def _start(self, ch, st):
        day = datetime.now().strftime("%Y-%m-%d")
        out = self.s.home / "channels" / ch["name"] / day
        work = self.s.home / "work" / ch["name"]
        out.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        logf = open(self.s.home / "channels" / ch["name"] / "clipper.log", "a")
        p = subprocess.Popen(self.clipper_cmd(ch, out, work), stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
                             text=True)
        self.procs[ch["name"]] = dict(p=p, started=now(), log=logf)
        st.update(capturing=True, last_start=now())
        self.s.log(f"{ch['name']}: live -> clipper started (pid {p.pid}) -> {out}")

    def _stop(self, name, why):
        h = self.procs.pop(name, None)
        if not h:
            return
        p = h["p"]
        if p.poll() is None:
            try:
                p.stdin.write("q\n"); p.stdin.flush()
            except Exception:
                pass
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        try:
            h["log"].close()
        except Exception:
            pass
        self.s.log(f"{name}: clipper stopped ({why})")

    def stop_all(self):
        for n in list(self.procs):
            self._stop(n, "watcher shutdown")

    # -- one pass
    def tick(self):
        state = self.s.state()
        chans = self.s.channels()
        names = {c["name"] for c in chans}
        for n in list(self.procs):
            if n not in names:
                self._stop(n, "channel removed")
        for ch in chans:
            st = state.setdefault(ch["name"], dict(live=False, status="offline", fails=0, capturing=False))
            try:
                res = self.check(ch)
            except Exception as e:
                res = "unknown"
                self.s.log(f"{ch['name']}: check error {e}")
            st["checked"] = stamp()
            h = self.procs.get(ch["name"])
            if h and h["p"].poll() is not None:     # clipper exited by itself
                ran = now() - h["started"]
                self.procs.pop(ch["name"])
                st["capturing"] = False
                if res == "live":
                    st["fails"] = st.get("fails", 0) + 1 if ran < self.FAST_EXIT_S else 0
                    self.s.log(f"{ch['name']}: clipper exited after {ran:.0f}s (rc {h['p'].returncode}), fails={st['fails']}")
                    if st["fails"] >= self.FAILS_BEFORE_BLOCKED:
                        st["status"] = "capture_blocked"
                    if st["fails"] >= self.FAILS_BEFORE_BLOCKED and not st.get("cap_notified"):
                        st["cap_notified"] = True
                        self.notify(f"{ch['name']} is LIVE but capture keeps failing here. Check "
                                    f"channels/{ch['name']}/clipper.log or run liveclipper.py --check.")
            if res == "live":
                if not st.get("live"):
                    st["live"], st["since"] = True, stamp()
                    st["status"] = "live"
                    st["fails"] = 0
                    st["cap_notified"] = False
                    self.notify(f"{ch['name']} went LIVE")
                can = ch["name"] not in self.procs and (st.get("status") != "capture_blocked"
                                                        or now() - st.get("last_start", 0) > self._backoff(st))
                if can:
                    if len(self.procs) < self.max:
                        self._start(ch, st)
                    else:
                        self.s.log(f"{ch['name']}: live but at the {self.max}-clipper cap, waiting")
            elif res == "offline":
                if st.get("live"):
                    st["live"] = False
                    st["status"] = "offline"
                    st["ended"] = stamp()
                    self.s.log(f"{ch['name']}: stream ended")
                    self._stop(ch["name"], "stream ended")
                    st["capturing"] = False
                    if ch["platform"] == "twitch" and self.vod_cmd:
                        self.vods.append((now() + self.vod_delay, ch))
                st["fails"] = 0
            else:  # unknown: change nothing, never kill a running capture on a flaky check
                st["status"] = st.get("status", "unknown") if st.get("live") else "unknown"
            if res == "blocked":
                st["status"] = "live_blocked" if st.get("live") else "blocked"
                if not st.get("blocked_notified"):
                    st["blocked_notified"] = True
                    self.notify(f"{ch['name']}: YouTube asked for a sign-in/bot check, cannot see live state from here.")
        self._run_vods()
        self.s.save_state(state)
        return state

    @staticmethod
    def _backoff(st):
        return min(30 * (2 ** max(st.get("fails", 0) - 1, 0)), 600)

    def _run_vods(self):
        due = [v for v in self.vods if v[0] <= now()]
        self.vods = [v for v in self.vods if v[0] > now()]
        for _, ch in due:
            try:
                cmd = self.vod_cmd(ch)
                if cmd:
                    self.s.log(f"{ch['name']}: running VOD highlights pass")
                    subprocess.Popen(cmd, stdout=open(self.s.home / "channels" / ch["name"] / "vod.log", "a"),
                                     stderr=subprocess.STDOUT)
            except Exception as e:
                self.s.log(f"{ch['name']}: VOD pass failed to start: {e}")


def latest_twitch_vod(ch):
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-items", "1", "--print", "url",
                        "--no-warnings", f"https://www.twitch.tv/{ch['name']}/videos?filter=archives"],
                       capture_output=True, text=True, timeout=60)
    url = (r.stdout or "").strip().splitlines()
    return url[0] if url else None


def make_vod_cmd(store):
    def cmd(ch):
        url = latest_twitch_vod(ch)
        if not url:
            return None
        out = store.home / "channels" / ch["name"] / "vod_highlights"
        return [sys.executable, str(HERE / "vodclip.py"), url, "--top", "5", "--transcribe", "--out", str(out)]
    return cmd


# ---------------- CLI ----------------
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["add", "remove", "list", "status", "run", "once"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--home", default=str(HOME))
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--max", type=int, default=3)
    ap.add_argument("--webhook", default=None)
    ap.add_argument("--no-vod", action="store_true", help="skip the Twitch VOD highlights pass after a stream ends")
    a = ap.parse_args(argv)
    store = Store(a.home)
    if a.cmd == "add":
        if not a.arg:
            ap.error("add needs a channel")
        ch = norm_channel(a.arg)
        chans = [c for c in store.channels() if c["name"] != ch["name"]] + [ch]
        store.save_channels(chans)
        print(f"watching {ch['name']} ({ch['platform']})")
    elif a.cmd == "remove":
        store.save_channels([c for c in store.channels() if c["name"] != a.arg])
        print("removed", a.arg)
    elif a.cmd == "list":
        for c in store.channels():
            print(c["name"], c["platform"], c["url"])
    elif a.cmd == "status":
        for n, s in store.state().items():
            print(f'{n:24s} {s.get("status", "?"):16s} checked {s.get("checked", "-")}')
    else:
        w = Watcher(store, extra=extra, max_clippers=a.max, webhook=a.webhook,
                    vod_cmd=None if a.no_vod else make_vod_cmd(store))
        if a.cmd == "once":
            w.tick(); w.stop_all(); return
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        store.log(f"watching {len(store.channels())} channel(s) every {a.interval}s (Ctrl-C to stop)")
        try:
            while True:
                w.tick()
                time.sleep(a.interval)
        except KeyboardInterrupt:
            store.log("stopping")
            w.stop_all()


if __name__ == "__main__":
    main()
