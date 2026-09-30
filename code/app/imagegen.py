"""Kanallarim: post rasmi yaratish.

1) OpenAI rasm PROMPTINI yozadi (matn: sarlavha, qisqa izoh) — rasm chizmaydi.
2) Lokal model (diffusers: SD-Turbo / SDXL-Turbo / SD 1.5) fon rasmini chizadi (video karta, bepul).
3) Pillow brend qatlamini qo'yadi: logotip, sarlavha, telefon, manzil, mo'ljal, aloqa, 3 ta brend rangi.
Mahalliy modellar o'zbekcha/ruscha harflarni rasm ichida xato chizadi, shuning uchun barcha matn dasturning o'zi tomonidan yoziladi.
diffusers o'rnatilmagan bo'lsa, brend ranglaridan gradient fon ishlatiladi (karta baribir chiqadi).
"""
import asyncio
import random
import threading
import time
import uuid
from pathlib import Path

from . import ai, ch_data, db
from .config import MEDIA_DIR, log

MODELS = {
    "stabilityai/sd-turbo": ("SD-Turbo (tez, 2 qadam, 512 px) — tavsiya", 2, 0.0),
    "stabilityai/sdxl-turbo": ("SDXL-Turbo (sifatliroq, 6 GB da sekinroq)", 2, 0.0),
    "stable-diffusion-v1-5/stable-diffusion-v1-5": ("Stable Diffusion 1.5 (20 qadam, sekin)", 20, 7.0),
}
DEFAULT_MODEL = "stabilityai/sd-turbo"
SIZES = {"1080x1080": (1080, 1080), "1280x720": (1280, 720), "1080x1350": (1080, 1350)}
LAYOUTS = {"bottom": "Pastda matn", "center": "O'rtada matn", "split": "Chapda rangli panel"}
NEG = "text, letters, words, watermark, logo, signature, deformed, blurry, low quality, extra fingers"

_lock = threading.Lock()
_pipe = None
_pipe_key = None
jobs: dict[str, dict] = {}


# ---------------------------------------------------------------- holat
def status() -> dict:
    st = {"pillow": False, "torch": False, "cuda": False, "diffusers": False, "gpu": "", "vram_gb": 0.0,
          "model": db.get_setting("img_model") or DEFAULT_MODEL}
    try:
        import PIL  # noqa: F401
        st["pillow"] = True
    except Exception:
        pass
    try:
        import torch
        st["torch"] = True
        st["cuda"] = bool(torch.cuda.is_available())
        if st["cuda"]:
            p = torch.cuda.get_device_properties(0)
            st["gpu"], st["vram_gb"] = p.name, round(p.total_memory / 1e9, 1)
        import diffusers  # noqa: F401
        st["diffusers"] = True
    except Exception:
        pass
    st["ready"] = st["diffusers"] and st["cuda"]
    return st


def installer_path() -> Path:
    return Path(__file__).resolve().parent.parent / "tools" / "install_imagegen.bat"


# ---------------------------------------------------------------- fonlar
def _is_16xx(name: str) -> bool:
    n = name.upper()
    return "GTX 16" in n or "GTX 1650" in n or "GTX 1660" in n


def _load(dtype_name: str):
    global _pipe, _pipe_key
    import torch
    name = db.get_setting("img_model") or DEFAULT_MODEL
    key = (name, dtype_name)
    if _pipe is not None and _pipe_key == key:
        return _pipe
    from diffusers import AutoPipelineForText2Image
    dtype = torch.float16 if dtype_name == "fp16" else torch.float32
    kw = {"torch_dtype": dtype}
    if dtype_name == "fp16":
        kw["variant"] = "fp16"
    try:
        pipe = AutoPipelineForText2Image.from_pretrained(name, **kw)
    except Exception:
        kw.pop("variant", None)
        pipe = AutoPipelineForText2Image.from_pretrained(name, **kw)
    vram = torch.cuda.get_device_properties(0).total_memory / 1e9
    if "xl" in name.lower() and vram < 9:
        pipe.enable_model_cpu_offload()
    else:
        pipe.to("cuda")
    try:
        pipe.enable_attention_slicing()
        pipe.set_progress_bar_config(disable=True)
    except Exception:
        pass
    _pipe, _pipe_key = pipe, key
    return pipe


def unload():
    global _pipe, _pipe_key
    with _lock:
        _pipe, _pipe_key = None, None
    try:
        import gc
        import torch
        gc.collect()
        torch.cuda.empty_cache()
    except Exception:
        pass


def _is_black(img) -> bool:
    from PIL import ImageStat
    m = ImageStat.Stat(img.convert("L")).mean[0]
    return m < 4 or m > 251


def draw_background(prompt: str, w: int, h: int, seed: int | None = None):
    """Lokal model bilan fon chizadi. Xato bo'lsa (yoki qora rasm) fp32 bilan qayta urinadi. Qaytaradi: PIL.Image"""
    import torch
    name = db.get_setting("img_model") or DEFAULT_MODEL
    _, steps, guide = MODELS.get(name, ("", 2, 0.0))
    gen_w = max(256, (min(w, 768) // 8) * 8)
    gen_h = max(256, (int(gen_w * h / w) // 8) * 8)
    gname = torch.cuda.get_device_name(0)
    order = ["fp32", "fp16"] if _is_16xx(gname) else ["fp16", "fp32"]
    last = None
    with _lock:
        for dt in order:
            try:
                pipe = _load(dt)
                g = torch.Generator("cuda").manual_seed(seed if seed is not None else random.randint(1, 2**31 - 1))
                kw = dict(prompt=prompt, width=gen_w, height=gen_h, num_inference_steps=steps, guidance_scale=guide, generator=g)
                if guide > 0:
                    kw["negative_prompt"] = NEG
                img = pipe(**kw).images[0]
                if _is_black(img):
                    last = RuntimeError("qora rasm chiqdi")
                    unload_nolock()
                    continue
                return img
            except torch.cuda.OutOfMemoryError as e:
                last = e
                unload_nolock()
                torch.cuda.empty_cache()
            except Exception as e:
                last = e
                log.warning("Rasm chizishda xato (%s): %s", dt, e)
                unload_nolock()
    raise RuntimeError(f"Rasm chizib bo'lmadi: {last}")


def unload_nolock():
    global _pipe, _pipe_key
    _pipe, _pipe_key = None, None


def gradient_background(colors, w, h):
    from PIL import Image, ImageDraw
    c1, c2 = _rgb(colors[0]), _rgb(colors[2] if len(colors) > 2 else colors[1])
    img = Image.new("RGB", (w, h), c1)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.line([(0, y), (w, y)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    acc = _rgb(colors[1])
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.ellipse([int(w * 0.55), -int(h * 0.15), int(w * 1.15), int(h * 0.45)], fill=acc + (70,))
    od.ellipse([-int(w * 0.2), int(h * 0.6), int(w * 0.4), int(h * 1.2)], fill=acc + (45,))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


# ---------------------------------------------------------------- brend qatlami
def _rgb(hexv: str):
    hexv = (hexv or "#000000").lstrip("#")
    return tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb):
    r, g, b = [x / 255 for x in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _font(size: int, bold=False):
    from PIL import ImageFont
    cands = ([r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
             if bold else [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for p in cands:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in (text or "").split():
        t = (cur + " " + word).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text, box_w, box_h, start, bold, min_size=18, spacing=1.18):
    size = start
    while size >= min_size:
        f = _font(size, bold)
        lines = _wrap(draw, text, f, box_w)
        if len(lines) * size * spacing <= box_h and all(draw.textlength(ln, font=f) <= box_w for ln in lines):
            return f, lines, size
        size -= 2
    f = _font(min_size, bold)
    return f, _wrap(draw, text, f, box_w), min_size


def contact_lines(ch) -> list[str]:
    out = []
    if (ch["phones"] or "").strip():
        out.append("☎ " + " ".join(ch["phones"].split()))
    if (ch["address"] or "").strip():
        out.append("📍 " + " ".join(ch["address"].split()))
    if (ch["landmark"] or "").strip():
        out.append("Mo'ljal: " + " ".join(ch["landmark"].split()))
    if (ch["contacts"] or "").strip():
        out.append(" ".join(ch["contacts"].split()))
    return out


def compose(bg, ch, headline: str, subline: str, layout: str, size_key: str):
    from PIL import Image, ImageDraw
    W, H = SIZES.get(size_key, (1080, 1080))
    cols = ch_data.colors(ch)
    c_main, c_acc, c_dark = (_rgb(c) for c in cols)
    # fonni to'ldirib kesish
    bw, bh = bg.size
    k = max(W / bw, H / bh)
    bg = bg.resize((int(bw * k) + 1, int(bh * k) + 1), Image.LANCZOS)
    left, top = (bg.width - W) // 2, (bg.height - H) // 2
    img = bg.crop((left, top, left + W, top + H)).convert("RGBA")
    pad = int(W * 0.06)
    lines_c = [ln for ln in contact_lines(ch)]
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    text_col = (255, 255, 255)
    # ---- joylashuv
    foot_h = int(H * 0.05 * max(1, len(lines_c))) + pad // 2 if lines_c else 0
    foot_h = min(foot_h, int(H * 0.26))
    if layout == "split":
        pw = int(W * 0.52)
        d.rectangle([0, 0, pw, H], fill=c_dark + (236,))
        d.rectangle([0, 0, int(W * 0.018), H], fill=c_main + (255,))
        tx0, tx1, ty0 = pad, pw - pad, pad + (int(H * 0.12) if ch["logo"] else 0)
        ty1 = H - pad - foot_h
    elif layout == "center":
        d.rectangle([0, 0, W, H], fill=c_dark + (150,))
        tx0, tx1 = pad, W - pad
        ty0, ty1 = int(H * 0.22), H - pad - foot_h - int(H * 0.04)
    else:  # bottom
        gh = int(H * 0.72)
        for i in range(gh):
            a = int(235 * (i / gh) ** 1.6)
            d.line([(0, H - gh + i), (W, H - gh + i)], fill=c_dark + (a,))
        tx0, tx1 = pad, W - pad
        ty0, ty1 = int(H * 0.50), H - pad - foot_h
    img = Image.alpha_composite(img, ov)
    d = ImageDraw.Draw(img)
    box_w = tx1 - tx0
    # ---- sarlavha va izoh
    sub_h = int((ty1 - ty0) * 0.28) if subline else 0
    head_h = (ty1 - ty0) - sub_h - int(H * 0.02)
    f1, l1, s1 = _fit(d, headline, box_w, head_h, int(W * 0.085), True, 26)
    y = ty0
    if layout == "center":
        y = ty0 + max(0, (head_h - len(l1) * int(s1 * 1.18)) // 2)
    d.rectangle([tx0, y - int(H * 0.012) - 6, tx0 + int(W * 0.12), y - int(H * 0.012)], fill=c_acc)
    for ln in l1:
        x = tx0 if layout != "center" else (W - d.textlength(ln, font=f1)) // 2
        d.text((x + 2, y + 2), ln, font=f1, fill=(0, 0, 0, 110))
        d.text((x, y), ln, font=f1, fill=text_col)
        y += int(s1 * 1.18)
    if subline:
        f2, l2, s2 = _fit(d, subline, box_w, sub_h, int(W * 0.045), False, 18)
        y += int(H * 0.012)
        for ln in l2:
            x = tx0 if layout != "center" else (W - d.textlength(ln, font=f2)) // 2
            d.text((x, y), ln, font=f2, fill=c_acc if _lum(c_acc) > 0.35 else (255, 255, 255))
            y += int(s2 * 1.2)
    # ---- aloqa qatori
    if lines_c:
        fy = H - pad - int(foot_h) + pad // 2 if layout != "split" else H - pad - foot_h + pad // 2
        fx0 = tx0
        fw = (tx1 - tx0)
        fs = int(W * 0.028)
        f3 = _font(fs, False)
        band = [fx0 - 14, fy - 12, fx0 + fw + 14, H - pad // 2 + 4]
        if layout != "split":
            d.rounded_rectangle(band, radius=16, fill=c_main + (225,))
            col = (255, 255, 255) if _lum(c_main) < 0.6 else (20, 20, 20)
        else:
            col = (255, 255, 255)
        yy = fy
        for ln in lines_c:
            for part in _wrap(d, ln, f3, fw):
                if yy + fs > H - pad // 2:
                    break
                d.text((fx0, yy), part, font=f3, fill=col)
                yy += int(fs * 1.25)
    # ---- brend nomi va logotip
    if (ch["brand_name"] or "").strip():
        fb = _font(int(W * 0.03), True)
        bx = pad if layout != "split" else pad
        by = pad // 2 + (int(H * 0.085) if ch["logo"] else 0) if layout == "split" else pad // 2
        if layout != "split" and ch["logo"]:
            bx = pad + int(W * 0.12)
            by = pad // 2 + int(H * 0.02)
        d.text((bx, by), ch["brand_name"], font=fb, fill=(255, 255, 255))
    if ch["logo"]:
        try:
            lg = Image.open(MEDIA_DIR / ch["logo"]).convert("RGBA")
            lh = int(H * 0.09)
            lg.thumbnail((int(W * 0.2), lh), Image.LANCZOS)
            plate = Image.new("RGBA", (lg.width + 24, lg.height + 24), (255, 255, 255, 215))
            mask = Image.new("L", plate.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, plate.width, plate.height], radius=14, fill=255)
            img.paste(plate, (pad // 2, pad // 2), mask)
            img.paste(lg, (pad // 2 + 12, pad // 2 + 12), lg)
        except Exception:
            log.warning("Logotip qo'yilmadi", exc_info=True)
    return img.convert("RGB")


# ---------------------------------------------------------------- AI: prompt
PROMPT_SYSTEM = """You prepare a social-media post image for a business Telegram channel in Uzbekistan.
Write (1) an English prompt for a local text-to-image model that draws ONLY the background illustration/photo for this post:
one clear scene, subject and mood matching the post, simple composition, plenty of empty space for text, NO text, letters, numbers, logos,
watermarks, no real people's faces close-up; follow the channel's image style if given. Keep it under 60 words, comma-separated phrases.
(2) a short headline for the overlay (max 7 words) and a one-line subline (max 12 words) in the post's language (copy the language of the post).
Return ONLY JSON: {"image_prompt": "...", "headline": "...", "subline": "..."}"""


async def write_prompt(channel_id: int, post_text: str, title: str = "") -> dict:
    ch = ch_data.get(channel_id)
    style = (ch["img_style"] or "").strip()
    cols = ", ".join(ch_data.colors(ch))
    user = (f"Channel: {ch['title']} ({ch['type_key'] or 'business'}). Brand colours: {cols}.\n" +
            (f"Preferred image style: {style}\n" if style else "") +
            f"Post title: {title}\nPost text:\n{(post_text or '')[:1800]}")
    data, _ = await ai.chat(channel_id, "analysis", PROMPT_SYSTEM, user, max_out=600, note="rasm prompti")
    if not isinstance(data, dict) or not data.get("image_prompt"):
        raise ai.AIError("AI rasm promptini yozib bera olmadi")
    return {"image_prompt": str(data["image_prompt"]), "headline": str(data.get("headline", ""))[:80], "subline": str(data.get("subline", ""))[:120]}


# ---------------------------------------------------------------- ishga tushirish
def _work(job_id, cid, prompt, headline, subline, layout, size_key, engine, plan_id, idea_id):
    j = jobs[job_id]
    try:
        ch = ch_data.get(cid)
        W, H = SIZES.get(size_key, (1080, 1080))
        j["msg"] = "Fon tayyorlanmoqda"
        if engine == "local":
            st = status()
            if not st["ready"]:
                raise RuntimeError("Lokal rasm yaratish o'rnatilmagan yoki video karta (CUDA) topilmadi: Sozlamalarda o'rnating, yoki «Gradient fon» ni tanlang")
            bg = draw_background(prompt, W, H)
            used = db.get_setting("img_model") or DEFAULT_MODEL
        else:
            bg = gradient_background(ch_data.colors(ch), W, H)
            used = "gradient"
        j["msg"] = "Brend qatlami qo'yilmoqda"
        img = compose(bg, ch, headline, subline, layout, size_key)
        dest = MEDIA_DIR / "ch" / str(cid)
        dest.mkdir(parents=True, exist_ok=True)
        name = f"img_{uuid.uuid4().hex[:10]}.jpg"
        img.save(dest / name, quality=92)
        iid = db.ex("INSERT INTO ch_images(channel_id,plan_id,idea_id,prompt,name,size,engine,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cid, plan_id, idea_id, prompt[:2000], f"ch/{cid}/{name}", size_key, used, db.now()))
        j.update(done=True, ok=True, msg="Tayyor", image_id=iid, name=f"ch/{cid}/{name}")
    except Exception as e:
        log.warning("Rasm yaratilmadi: %s", e, exc_info=True)
        j.update(done=True, ok=False, msg=str(e)[:300])
    finally:
        j["t1"] = time.time()


def start(cid, prompt, headline, subline, layout, size_key, engine, plan_id=None, idea_id=None) -> str:
    for k in [k for k, v in jobs.items() if time.time() - v.get("t0", 0) > 3600]:
        jobs.pop(k, None)
    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"done": False, "ok": False, "msg": "Navbatda", "t0": time.time(), "channel": cid}
    threading.Thread(target=_work, args=(job_id, cid, prompt, headline, subline, layout, size_key, engine, plan_id, idea_id), daemon=True).start()
    return job_id


def attach_to_plan(image_id: int, plan_id: int) -> bool:
    import json
    img = db.one("SELECT * FROM ch_images WHERE id=?", (image_id,))
    it = db.one("SELECT * FROM ch_plan WHERE id=?", (plan_id,))
    if not img or not it or img["channel_id"] != it["channel_id"] or it["status"] == "sent":
        return False
    names = json.loads(it["media_json"] or "[]")
    if img["name"] not in names:
        names.append(img["name"])
    names = names[:10]
    mt = it["media_type"]
    if not mt or mt == "image":
        mt = "image" if len(names) == 1 else "album"
    db.ex("UPDATE ch_plan SET media_json=?, media_type=?, image_prompt=COALESCE(image_prompt, ?) WHERE id=?",
          (json.dumps(names), mt, img["prompt"], plan_id))
    return True
