"""Designed 1280x720 thumbnails: picks the best-looking frame, crops toward the busiest part of it,
grades it, and sets a short hook headline. The headline only comes from what you give it (--hook),
the --ai copy, or the clip's own transcript / chat. It does not invent text, but it cannot check that a phrase is true or fair out of context; a human must."""
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat

W, H = 1280, 720
FONT_CANDIDATES = ["/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                   "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/impact.ttf"]


def _font(size, path=None):
    for p in ([path] if path else []) + FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _duration(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def sample_frames(video, n=12):
    d = _duration(video)
    out, tmp = [], Path(tempfile.mkdtemp(prefix="thumbs_"))
    for i in range(n):
        t = d * (i + 0.5) / n
        p = tmp / f"f{i:02d}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                        "-q:v", "2", str(p)], capture_output=True)
        if p.exists():
            out.append((t, p))
    return out


def score_frame(img):
    """Sharp + contrasty + colourful + not dark. Higher is a better thumbnail candidate."""
    g = img.convert("L").resize((320, 180))
    edges = g.filter(ImageFilter.FIND_EDGES)
    sharp = ImageStat.Stat(edges).var[0]
    contrast = ImageStat.Stat(g).stddev[0]
    bright = ImageStat.Stat(g).mean[0]
    sat = ImageStat.Stat(img.convert("HSV").resize((160, 90))).mean[1]
    pen = 0.0
    if bright < 45:
        pen += (45 - bright) * 6
    if bright > 220:
        pen += (bright - 220) * 6
    return sharp * 0.5 + contrast * 2 + sat * 0.8 - pen


def busiest_center(img):
    """Centroid of edge energy (0..1, 0..1): where the detail is, as a cheap subject guess."""
    e = img.convert("L").resize((64, 36)).filter(ImageFilter.FIND_EDGES)
    px = e.load()
    tot = sx = sy = 0.0
    for y in range(36):
        for x in range(64):
            v = px[x, y] ** 2
            tot += v
            sx += v * x
            sy += v * y
    if tot == 0:
        return 0.5, 0.5
    return sx / tot / 63, sy / tot / 35


def cover_crop(img, zoom, cx, cy):
    iw, ih = img.size
    cw, ch = iw / zoom, ih / zoom
    if cw / ch > W / H:
        cw = ch * W / H
    else:
        ch = cw * H / W
    x0 = min(max(cx * iw - cw / 2, 0), iw - cw)
    y0 = min(max(cy * ih - ch / 2, 0), ih - ch)
    return img.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch))).resize((W, H), Image.LANCZOS)


def headline_from(hook, transcript, top_chat, channel):
    """Returns (text, source). Never makes anything up. Every headline still needs a human check."""
    if hook:
        return hook, "hook"
    t = re.sub(r"\s+", " ", transcript or "").strip()
    if t:
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
        best = max(sents, key=lambda s: (("!" in s or "?" in s) * 3 + min(len(s.split()), 6)))
        return " ".join(best.split()[:5]).rstrip(",.;:"), "transcript"
    # chat text is never used as a headline: it can be spam, a joke or unrelated to the clip
    return "LIVE CLIP", "default"


def _wrap(draw, text, font, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _stroked(draw, xy, text, font, fill, stroke, sw):
    x, y = xy
    draw.text((x + 6, y + 8), text, font=font, fill=(0, 0, 0, 160), stroke_width=sw, stroke_fill=(0, 0, 0, 160))
    draw.text((x, y), text, font=font, fill=fill, stroke_width=sw, stroke_fill=stroke)


def make_thumbnail(video, out, hook=None, transcript="", top_chat=None, channel="", handle=None, font_path=None,
                   frames=None):
    """Writes a 1280x720 JPEG. Returns dict(source=headline source, at=seconds, score=float)."""
    frames = frames or sample_frames(video)
    if not frames:
        raise RuntimeError("no frames could be read from the clip")
    best = max(frames, key=lambda tf: score_frame(Image.open(tf[1]).convert("RGB")))
    src = Image.open(best[1]).convert("RGB")
    cx, cy = busiest_center(src)
    base = cover_crop(src, 1.18, cx, cy)
    base = ImageEnhance.Contrast(base).enhance(1.18)
    base = ImageEnhance.Color(base).enhance(1.3)
    base = ImageEnhance.Sharpness(base).enhance(1.4)
    # left-to-right dark gradient so the text always reads
    grad = Image.new("L", (W, H))
    gp = grad.load()
    for x in range(W):
        a = int(215 * max(0.0, 1 - x / (W * 0.78)) ** 1.3)
        for y in range(H):
            gp[x, y] = a
    base = Image.composite(Image.new("RGB", (W, H), (8, 8, 14)), base, grad)
    # bottom vignette
    vg = Image.new("L", (W, H))
    vp = vg.load()
    for y in range(H):
        a = int(120 * max(0.0, (y - H * 0.6) / (H * 0.4)))
        for x in range(W):
            vp[x, y] = a
    base = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), base, vg)
    img = base.convert("RGBA")
    d = ImageDraw.Draw(img)
    text, source = headline_from(hook, transcript, top_chat, channel)
    text = text.upper()
    maxw, size = int(W * 0.58), 190
    while size > 70:
        f = _font(size, font_path)
        lines = _wrap(d, text, f, maxw)
        if len(lines) <= 3 and max(d.textlength(l, font=f) for l in lines) <= maxw:
            break
        size -= 8
    f = _font(size, font_path)
    lines = _wrap(d, text, f, maxw)
    lh = int(size * 1.02)
    y = (H - lh * len(lines)) // 2 - 10
    accent = (255, 214, 0, 255)
    for i, l in enumerate(lines):
        _stroked(d, (60, y + i * lh), l, f, accent if i == len(lines) - 1 else (255, 255, 255, 255), (0, 0, 0, 255),
                 max(size // 14, 5))
    # accent bar + small channel chip
    d.rectangle((60, y - 26, 60 + 190, y - 14), fill=(255, 40, 70, 255))
    label = (handle or channel or "").strip()
    if label:
        cf = _font(40, font_path)
        tw = d.textlength(label, font=cf)
        d.rounded_rectangle((50, H - 96, 50 + tw + 44, H - 36), radius=14, fill=(0, 0, 0, 190))
        d.text((72, H - 90), label, font=cf, fill=(255, 255, 255, 255))
    img.convert("RGB").save(out, quality=92)
    for _, p in frames:
        p.unlink(missing_ok=True)
    return dict(source=source, text=text, review_required=True, at=round(best[0], 1), score=round(score_frame(src), 1))
