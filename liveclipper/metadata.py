"""
metadata.py - titles, descriptions, keywords, hashtags and thumbnails for clips.

Never uses the file name as a title. Everything is built only from what the clip really has (hook, caption, transcript,
chat trigger, channel name), so it never invents an event. Style follows the usual GTA clip Shorts: short, loud, one emoji,
channel name, game tag. Every field can be edited in the panel before upload; edits win over generated text.
"""
import json, re, threading, zlib
from pathlib import Path

DEFAULT_GAME = "GTA 6"
GAME_TAGS = {"gta 6": ["GTA6", "GTAVI", "GrandTheftAutoVI"], "gta vi": ["GTA6", "GTAVI", "GrandTheftAutoVI"]}
GAME_KEYWORDS = {"gta 6": ["gta 6", "gta vi", "grand theft auto 6", "gta 6 gameplay", "gta 6 funny moments", "gta 6 clips",
                           "gta 6 reaction", "gta 6 launch", "rockstar games"]}
PRETTY = {"ishowspeed": "IShowSpeed", "kaicenat": "Kai Cenat", "xqc": "xQc", "xqcow": "xQc", "ludwig": "Ludwig",
          "adinross": "Adin Ross", "stableronaldo": "StableRonaldo", "jasontheween": "Jason The Ween", "lacy": "LaCy",
          "clix": "Clix", "michou": "Michou"}
STOP = set("the a an and or but so to of in on at is it its i you he she we they this that was were be been are am do did "
           "have has had not no yes yeah okay ok like just um uh what when with for from my your me his her them there "
           "here get got going gonna will would can could oh wait".split())
EMOJI = ["😱", "🤯", "💀", "😭", "🔥", "😂"]
TITLE_MAX = 90          # YouTube allows 100; shorter reads better in the Shorts feed


def pretty_channel(name):
    n = (name or "").strip()
    if n.lower() in PRETTY:
        return PRETTY[n.lower()]
    if n and n == n.lower():
        return n[:1].upper() + n[1:]
    return n


def channel_of(mp4, meta):
    ch = (meta or {}).get("channel")
    if ch:
        return str(ch)
    m = re.match(r"^(.+?)_\d{8}-\d{6}_x", Path(mp4).name)
    return m.group(1) if m else ""


def _no_links(t):
    """Generated text never carries a URL."""
    t = re.sub(r"(?:https?://|www\.)\S+", "", t or "")
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", t)).strip()


def _hashtag(s):
    return re.sub(r"[^A-Za-z0-9]", "", s or "")


def _clean(t):
    return re.sub(r"\s+", " ", (t or "")).strip(" \t\n-_,.;:")


def _looks_like_filename(t):
    return bool(re.search(r"\d{8}-\d{6}|_final|\bx\d\.\d\b|\.mp4", t or ""))


def phrase_from(hint, pack_title=None):
    """The real words of the moment: hook, else loudest transcript sentence, else caption. '' when there is none."""
    meta = (hint or {}).get("meta") or hint or {}
    for cand in (meta.get("hook"), pack_title if pack_title and not _looks_like_filename(pack_title) else None):
        c = _clean(cand)
        if c and len(c.split()) >= 2 and "live moment" not in c.lower():
            return " ".join(c.split()[:8])
    t = _clean(meta.get("transcript"))
    if t:
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if len(s.split()) >= 2]
        if sents:
            best = max(sents, key=lambda s: (("!" in s or "?" in s) * 3 + min(len(s.split()), 8)))
            return " ".join(_clean(best).split()[:8])
    c = _clean(meta.get("caption"))
    return " ".join(c.split()[:8]) if c else ""


def _caps(phrase):
    p = phrase.rstrip("!?.,")
    return p.upper() if len(p) <= 28 else p[:1].upper() + p[1:]


def _keywords(hint, game_key, channel):
    base = list(GAME_KEYWORDS.get(game_key, [game_key, game_key + " gameplay", game_key + " funny moments", game_key + " clips"]))
    meta = (hint or {}).get("meta") or {}
    words = [w.lower() for w in re.findall(r"[A-Za-z']{5,}", meta.get("transcript") or "") if w.lower() not in STOP]
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
    ch = pretty_channel(channel)
    kws = ([ch, f"{ch} {game_key}", f"{ch} live", f"{ch} clips"] if ch else []) + base + seen[:4] + \
          ["shorts", "gaming clips", "live stream highlights", "funny moments", "viral gaming"]
    out, total = [], 0
    for k in dict.fromkeys(x.strip() for x in kws if x.strip()):
        if total + len(k) + 1 > 450:      # YouTube caps the tag list at 500 characters
            break
        out.append(k)
        total += len(k) + 1
    return out


def generate(mp4, hint=None, pack=None, game=DEFAULT_GAME, clip_id=None):
    """-> dict(title, description, tags, hashtags, caption, headline). Pure text, no files touched."""
    hint, pack = hint or {}, pack or {}
    meta = hint.get("meta") or {}
    seed = zlib.crc32((clip_id or hint.get("id") or Path(mp4).name).encode())
    game = _clean(game) or DEFAULT_GAME
    gkey = game.lower()
    channel = channel_of(mp4, meta)
    ch = pretty_channel(channel)
    phrase = phrase_from(hint, pack.get("title"))
    emoji = EMOJI[seed % len(EMOJI)]
    live = hint.get("kind") == "live" or str(meta.get("trigger", "")).startswith("chat")
    if phrase:
        pats = [f"{_caps(phrase)} {emoji} {ch} in {game}" if ch else f"{_caps(phrase)} {emoji} {game}",
                f"{ch} {game}: {_caps(phrase)} {emoji}" if ch else f"{game}: {_caps(phrase)} {emoji}",
                f"\"{phrase.rstrip('!?.,')}\" {emoji} {ch + ' ' if ch else ''}{game}"]
    elif live and ch:
        pats = [f"{ch}'s {game} Moment Had Chat GOING CRAZY {emoji}", f"Chat LOST IT When {ch} Did This In {game} {emoji}",
                f"{ch} Playing {game} Gets Wild {emoji}"]
    elif ch:
        pats = [f"{ch} Playing {game} Gets Wild {emoji}", f"This {game} Moment Is INSANE {emoji} {ch}"]
    else:
        pats = [f"This {game} Moment Is INSANE {emoji}", f"{game} Gameplay Gets Out Of Hand {emoji}"]
    title = pats[seed % len(pats)]
    if len(title) > TITLE_MAX:
        title = pats[0] if len(pats[0]) <= TITLE_MAX else title[:TITLE_MAX - 1].rstrip() + "…"
    tags_game = GAME_TAGS.get(gkey, [_hashtag(game)])
    hashtags = list(dict.fromkeys(tags_game[:1] + ["Shorts"] + ([_hashtag(ch)] if ch else []) + tags_game[1:] +
                                  ["Gaming", "LiveStream", "FunnyMoments", "Clips", "Viral"]))
    tags = _keywords(hint, gkey, channel)
    lines = [title]
    cap = _clean(meta.get("caption"))
    if cap and cap.lower() not in title.lower():
        lines.append(cap)
    if ch:
        lines.append(f"Clip from {ch}'s " + ("live stream" if live else "stream") + f" ({game}). All credit to the creator.")
    lines.append("Subscribe for more " + game + " clips every day.")
    lines.append(" ".join("#" + h for h in hashtags[:12]))
    lines.append("Keywords: " + ", ".join(tags[:12]))
    desc = _no_links("\n\n".join(lines))
    title = _no_links(title)
    caption = title + "\n\n" + " ".join("#" + h for h in hashtags[:6])
    headline = " ".join(phrase.split()[:4]).rstrip(",.;:") if phrase else f"{game} CHAOS"
    return dict(title=title, description=desc[:4900], tags=tags, hashtags=hashtags, caption=caption[:2200],
                headline=headline.upper())


def ensure_thumbnail(mp4, hint=None, headline=None, force=False):
    """Returns a thumbnail Path: the one the live pipeline made, else a new one beside the clip. None if it cannot."""
    mp4 = Path(mp4)
    meta = (hint or {}).get("meta") or {}
    out = mp4.with_name(mp4.stem + "_thumb.jpg")
    if not force:
        if meta.get("thumbnail") and mp4.with_name(meta["thumbnail"]).exists():
            return mp4.with_name(meta["thumbnail"])
        if out.exists():
            return out
    try:
        import thumbmaker
        thumbmaker.make_thumbnail(mp4, out, hook=headline or "GTA 6 CHAOS", channel=pretty_channel(channel_of(mp4, meta)))
        return out if out.exists() else None
    except Exception:
        return None


class MetaStore:
    """User edits per clip, kept in <home>/metadata.json. Edits override generated text field by field."""
    FIELDS = ("title", "description", "tags", "caption", "headline")

    def __init__(self, home):
        self.path = Path(home) / "metadata.json"
        self.lock = threading.Lock()

    def _all(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def game(self):
        return self._all().get("_game") or DEFAULT_GAME

    def set_game(self, game):
        g = _clean(game)[:40]
        if not g:
            raise ValueError("game name is empty")
        with self.lock:
            d = self._all()
            d["_game"] = g
            self._write(d)

    def edits(self, clip_id):
        return self._all().get("clips", {}).get(clip_id, {})

    def save(self, clip_id, fields):
        clean = {}
        for k in self.FIELDS:
            if k not in (fields or {}):
                continue
            v = fields[k]
            if k == "tags":
                v = [t.strip() for t in (v if isinstance(v, list) else str(v).split(",")) if t.strip()][:40]
            else:
                v = str(v).strip() if v is not None else ""
            if k == "title":
                if not v:
                    raise ValueError("title cannot be empty")
                v = v[:100]
            if k == "description":
                v = v[:5000]
            clean[k] = v
        with self.lock:
            d = self._all()
            d.setdefault("clips", {}).setdefault(clip_id, {}).update(clean)
            self._write(d)
        return clean

    def reset(self, clip_id):
        with self.lock:
            d = self._all()
            d.get("clips", {}).pop(clip_id, None)
            self._write(d)

    def _write(self, d):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(self.path)
