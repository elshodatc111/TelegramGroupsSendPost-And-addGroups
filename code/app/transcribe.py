"""Videolardagi nutqni matnga aylantirish.

Ikki usul:
- lokal: faster-whisper (GPU bo'lsa tezroq; bepul). Uchun ffmpeg va `pip install faster-whisper` kerak.
- bulut: OpenAI audio transkripsiya (har daqiqasi pullik, kanal hisobiga yoziladi).
Sozlama: stt_mode = auto | local | openai | off  (auto: lokal bor bo'lsa shu, aks holda OpenAI).
"""
import asyncio
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import ai, ch_tg, db
from .config import CODE_DIR, TMP_DIR, log

_model = None
_model_key = None
_lock = threading.Lock()
MAX_SEC = 600           # bitta videodan eng ko'pi bilan 10 daqiqa audio olinadi


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _prepare_cuda_dlls():
    """pip orqali o'rnatilgan CUDA (cublas/cudnn) kutubxonalarini Windows uchun ulaydi."""
    try:
        import site
        bases = list(site.getsitepackages()) + [site.getusersitepackages()]
        for b in bases:
            nv = Path(b) / "nvidia"
            if nv.exists():
                for sub in nv.iterdir():
                    bin_dir = sub / "bin"
                    if bin_dir.exists():
                        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                        if hasattr(os, "add_dll_directory"):
                            os.add_dll_directory(str(bin_dir))
    except Exception:
        log.warning("CUDA DLL yo'llari ulanmadi", exc_info=True)


def local_status() -> dict:
    """Sozlamalar sahifasi uchun: lokal transkripsiya tayyormi."""
    st = {"ffmpeg": bool(ffmpeg_path()), "fw": False, "gpu": False, "model": db.get_setting("whisper_model") or "large-v3"}
    try:
        import faster_whisper  # noqa: F401
        st["fw"] = True
        try:
            import ctranslate2
            st["gpu"] = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            pass
    except Exception:
        pass
    st["ready"] = st["ffmpeg"] and st["fw"]
    return st


def mode() -> str:
    m = (db.get_setting("stt_mode") or "auto").lower()
    if m == "auto":
        return "local" if local_status()["ready"] else "openai"
    return m


def _load_model():
    global _model, _model_key
    name = db.get_setting("whisper_model") or "large-v3"
    with _lock:
        if _model is not None and _model_key == name:
            return _model
        _prepare_cuda_dlls()
        from faster_whisper import WhisperModel
        last = None
        for device, ctype in (("cuda", "int8_float16"), ("cuda", "int8"), ("cpu", "int8")):
            try:
                _model = WhisperModel(name, device=device, compute_type=ctype)
                _model_key = name
                log.info("Whisper yuklandi: %s (%s/%s)", name, device, ctype)
                return _model
            except Exception as e:                    # GPU yo'q / CUDA kutubxonasi yo'q
                last = e
                log.info("Whisper %s/%s ishlamadi: %s", device, ctype, e)
        raise RuntimeError(f"Whisper yuklanmadi: {last}")


def _run_local(wav: str, lang: str | None) -> tuple[str, str]:
    model = _load_model()
    segs, info = model.transcribe(wav, language=lang, vad_filter=True, beam_size=5)
    text = " ".join(s.text.strip() for s in segs).strip()
    return text, (info.language or lang or "")


def _extract_audio(src: str, dst: str):
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg topilmadi (o'rnatish: tools\\install_whisper.bat)")
    flags = 0x08000000 if sys.platform == "win32" else 0
    p = subprocess.run([ff, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-t", str(MAX_SEC), dst],
                       capture_output=True, creationflags=flags, timeout=600)
    if p.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError("Audio ajratib bo'lmadi: " + p.stderr.decode("utf-8", "ignore")[-200:])


def lang_hint(ch) -> str | None:
    return {"uz": "uz", "uz_cyrl": "uz", "ru": "ru"}.get((ch["lang"] or "uz"), None)


async def transcribe_post(ch, post) -> str:
    """Post videosini matnga aylantiradi va ch_posts.transcript ga yozadi."""
    if post["transcript"]:
        return post["transcript"]
    if mode() == "off":
        raise RuntimeError("Transkripsiya Sozlamalarda o'chirilgan")
    comp_tg = ch["tg_id"]
    if post["comp_id"]:
        c = db.one("SELECT tg_id FROM ch_competitors WHERE id=?", (post["comp_id"],))
        comp_tg = c["tg_id"]
    TMP_DIR.mkdir(exist_ok=True)
    base = TMP_DIR / f"tr_{post['id']}"
    video = await ch_tg.download_video(ch["account_id"], comp_tg, post["msg_id"], str(base) + "_v")
    if not video:
        raise RuntimeError("Video yuklab bo'lmadi yoki juda katta")
    wav = str(base) + ".wav"
    try:
        await asyncio.to_thread(_extract_audio, video, wav)
        lang = lang_hint(ch)
        m = mode()
        if m == "local":
            text, det = await asyncio.to_thread(_run_local, wav, lang)
        else:
            dur = min(float(post["duration"] or 60), MAX_SEC)
            text, det = await ai.transcribe_openai(ch["id"], wav, lang, dur), lang or ""
        db.ex("UPDATE ch_posts SET transcript=?, tr_lang=?, tr_at=? WHERE id=?", (text, det, db.now(), post["id"]))
        return text
    finally:
        for p in (video, wav):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def installer_path() -> Path:
    return CODE_DIR / "tools" / "install_whisper.bat"
