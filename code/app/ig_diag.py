"""Instagram diagnostikasi: ulanish, ruxsatlar, joylash imkoniyati, tunnel."""
from . import ig_api, ig_data, ig_tunnel

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


def _i(title, state, detail="", fix=""):
    return {"title": title, "state": state, "detail": detail, "fix": fix}


async def run(acc, test_tunnel=False) -> list[dict]:
    out = []
    tok = ig_data.token(acc)
    if not tok:
        return [_i("Token", FAIL, "Token yo'q", "Akkauntni qayta ulang (Akkauntlar sahifasi).")]
    try:
        prof = await ig_api.me(tok)
        out.append(_i("Ulanish va token", OK, f"@{prof.get('username')} · {prof.get('account_type')} · {prof.get('followers_count')} obunachi"))
        if prof.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
            out.append(_i("Akkaunt turi", FAIL, f"Turi: {prof.get('account_type')}", "Instagram ilovasida akkauntni Professional (Business yoki Creator) ga o'tkazing."))
    except ig_api.IGError as e:
        return [_i("Ulanish va token", FAIL, str(e)[:250],
                   "Token eskirgan yoki bekor qilingan: Akkauntlar sahifasida yangi token kiriting." if e.token_dead else "Internetni tekshiring yoki birozdan so'ng qayta urining.")]
    d = ig_data.token_days(acc)
    if d is None:
        out.append(_i("Token muddati", INFO, "Muddati noma'lum (qo'lda kiritilgan bo'lishi mumkin)"))
    elif d < 0:
        out.append(_i("Token muddati", FAIL, "Muddati o'tgan", "Qayta ulang."))
    elif d <= 10:
        out.append(_i("Token muddati", WARN, f"{d} kun qoldi", "«Tokenni yangilash» ni bosing; dastur o'zi ham yangilaydi."))
    else:
        out.append(_i("Token muddati", OK, f"{d} kun qoldi (dastur har safar muddati yaqinlashganda o'zi yangilaydi)"))
    uid = acc["ig_user_id"]
    try:
        ms = await ig_api.media_list(tok, uid, 3)
        out.append(_i("Postlarni o'qish", OK, f"{len(ms)} ta post olindi"))
    except ig_api.IGError as e:
        out.append(_i("Postlarni o'qish", FAIL, str(e)[:200], "instagram_business_basic ruxsati va tester roli kerak."))
    try:
        from datetime import datetime, timedelta
        y0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        v = await ig_api.total_value(tok, uid, ["reach"], y0, y0 + timedelta(days=1))
        out.append(_i("Statistika (insights)", OK if v else WARN, f"reach (kecha): {v.get('reach')}" if v else "Javob bo'sh",
                      "" if v else "instagram_business_manage_insights ruxsati kerak; yangi akkauntda ma'lumot 48 soatgacha kechikadi."))
    except ig_api.IGError as e:
        out.append(_i("Statistika (insights)", FAIL, str(e)[:200], "Tokenni yaratishda manage_insights ruxsatini belgilang."))
    fol = int(prof.get("followers_count") or 0)
    out.append(_i("Auditoriya (yosh/shahar)", OK if fol >= 100 else INFO,
                  "Mavjud" if fol >= 100 else f"Instagram 100 obunachidan boshlab beradi (hozir {fol})"))
    try:
        lim = await ig_api.publish_limit(tok, uid)
        out.append(_i("Joylash ruxsati (API)", OK, f"24 soatda ishlatilgan: {lim.get('used')} / {lim.get('total')}"))
    except ig_api.IGError as e:
        out.append(_i("Joylash ruxsati (API)", WARN, str(e)[:200], "instagram_business_content_publish ruxsati kerak. Busiz faqat «eslatma» rejimi ishlaydi."))
    mode = ig_data.publish_mode()
    out.append(_i("Joylash rejimi", INFO, ig_data.MODE_LABELS[mode]))
    exe = ig_tunnel.find_exe()
    if mode == "auto" or test_tunnel:
        if not exe:
            out.append(_i("cloudflared", FAIL, "Topilmadi", "Sozlamalar → «cloudflared'ni yuklab olish»."))
        else:
            out.append(_i("cloudflared", OK, exe))
            if test_tunnel:
                try:
                    base = await ig_tunnel.open_tunnel()
                    try:
                        ok, msg = await ig_tunnel.selftest(base)
                    finally:
                        await ig_tunnel.close_tunnel()
                    out.append(_i("Tunnel sinovi", OK if ok else FAIL, msg, "" if ok else "Avto rejimni o'chirib, «Eslatma» rejimida ishlating."))
                except Exception as e:
                    out.append(_i("Tunnel sinovi", FAIL, f"{type(e).__name__}: {e}"[:250], "Internet va antivirusni tekshiring."))
    else:
        out.append(_i("cloudflared", INFO, "Topildi" if exe else "O'rnatilmagan (eslatma rejimida kerak emas)"))
    return out
