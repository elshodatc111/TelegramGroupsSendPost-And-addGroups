"""Instagram formatiga moslash: rasm (PIL) va video (ffmpeg) uchun kesish / sig'dirish.

Ko'rinish (brauzerdagi jonli oldindan ko'rish) bilan bir xil qoidalar:
- kesish (cover): fayl formatni to'ldiradi, ortig'i kesiladi; zoom + X/Y siljitish;
- sig'dirish (contain): fayl butunligicha, atrofi xira (blur) fon, oq, qora yoki tanlangan rang.
"""
import asyncio
import re
import shutil
from pathlib import Path

from .config import log

FORMATS = {"4:5": (1080, 1350), "1:1": (1080, 1080), "1.91:1": (1080, 566), "9:16": (1080, 1920)}
LABELS = {"auto": "Avto", "4:5": "Post 4:5", "1:1": "Kvadrat 1:1", "1.91:1": "Keng 1.91:1", "9:16": "Reels/Stories 9:16"}
MIN_R, MAX_R = 0.8, 1.91            # lenta uchun ruxsat etilgan nisbat (eni/bo'yi): 4:5 ... 1.91:1


class FitError(Exception):
    pass


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def params(form) -> dict:
    """Forma maydonlaridan xavfsiz sozlamalar."""
    def num(k, d, lo, hi):
        try:
            return max(lo, min(hi, float(form.get(k) or d)))
        except (TypeError, ValueError):
            return d
    fmt = form.get("fit_fmt") if form.get("fit_fmt") in FORMATS else "auto"
    bg = (form.get("fit_bg") or "blur").strip()
    if bg not in ("blur", "white", "black") and not re.fullmatch(r"#[0-9a-fA-F]{6}", bg):
        bg = "blur"
    return {"fmt": fmt, "mode": "contain" if form.get("fit_mode") == "contain" else "cover", "bg": bg,
            "zoom": num("fit_zoom", 1.0, 1.0, 3.0), "x": num("fit_x", 50, 0, 100), "y": num("fit_y", 50, 0, 100)}


def auto_target(w: int, h: int, story: bool = False):
    """«Avto»: nisbat Instagram lentasiga to'g'ri kelsa o'zgarmaydi, bo'lmasa fon bilan sig'diriladi."""
    r = w / h if h else 1
    if story:
        return None
    if r < MIN_R - 0.005:
        return "4:5"
    if r > MAX_R + 0.005:
        return "1.91:1"
    return None


def _rgb(bg: str):
    if bg == "white":
        return (255, 255, 255)
    if bg == "black":
        return (0, 0, 0)
    if bg.startswith("#"):
        return tuple(int(bg[i:i + 2], 16) for i in (1, 3, 5))
    return None


def fit_image(src: Path, dst: Path, p: dict, fmt: str | None = None) -> Path:
    from PIL import Image, ImageFilter, ImageOps
    fmt = fmt or p["fmt"]
    W, H = FORMATS[fmt]
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = im.size
    if p["mode"] == "cover":
        s = max(W / w, H / h)
        sw, sh = w * s, h * s
        ox, oy = W * p["x"] / 100, H * p["y"] / 100
        cx, cy = (sw - W) * p["x"] / 100, (sh - H) * p["y"] / 100
        z = p["zoom"]
        left, top = cx + ox - ox / z, cy + oy - oy / z
        box = (left / s, top / s, (left + W / z) / s, (top + H / z) / s)
        out = im.resize((W, H), Image.LANCZOS, box=box)
    else:
        s = min(W / w, H / h)
        nw, nh = max(1, round(w * s)), max(1, round(h * s))
        fg = im.resize((nw, nh), Image.LANCZOS)
        col = _rgb(p["bg"])
        if col is None:
            sc = max(W / w, H / h)
            bgim = im.resize((max(W, round(w * sc)), max(H, round(h * sc))), Image.LANCZOS)
            l, t = (bgim.width - W) // 2, (bgim.height - H) // 2
            out = bgim.crop((l, t, l + W, t + H)).filter(ImageFilter.GaussianBlur(36))
        else:
            out = Image.new("RGB", (W, H), col)
        out.paste(fg, ((W - nw) // 2, (H - nh) // 2))
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, "JPEG", quality=92, optimize=True)
    return dst


def _vf(p: dict, fmt: str) -> str:
    W, H = FORMATS[fmt]
    if p["mode"] == "cover":
        z, X, Y = p["zoom"], p["x"], p["y"]
        cw, ch = f"trunc({W}/{z}/2)*2", f"trunc({H}/{z}/2)*2"
        return (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop=w={cw}:h={ch}:x='(iw-{W})*{X}/100+{W}*{X}/100-{W}*{X}/100/{z}':y='(ih-{H})*{Y}/100+{H}*{Y}/100-{H}*{Y}/100/{z}',"
                f"scale={W}:{H},setsar=1,format=yuv420p")
    col = _rgb(p["bg"])
    if col is None:
        return (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=28:2[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p")
    c = "0x%02x%02x%02x" % col
    return (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={c},setsar=1,format=yuv420p")


async def fit_video(src: Path, dst: Path, p: dict, fmt: str | None = None, timeout: int = 900) -> Path:
    ff = ffmpeg()
    if not ff:
        raise FitError("Videoni moslash uchun ffmpeg kerak: PowerShell'da `winget install Gyan.FFmpeg`, so'ng dasturni qayta ishga tushiring.")
    fmt = fmt or p["fmt"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ff, "-y", "-i", str(src), "-vf", _vf(p, fmt), "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise FitError("Video moslash juda uzoq davom etdi (15 daqiqadan ortiq)")
    if proc.returncode != 0 or not dst.exists():
        log.warning("ffmpeg xato: %s", (err or b"")[-600:].decode(errors="ignore"))
        raise FitError("Videoni moslab bo'lmadi (ffmpeg xatosi). Fayl formati qo'llab-quvvatlanmagan bo'lishi mumkin.")
    return dst


def image_size(path: Path):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).size
