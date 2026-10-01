# PROJECT v11 — "Majlislar" Zoom'ga o'tkazildi (Google Meet olib tashlandi)

v10 dagi Google Meet bo'limi to'liq olib tashlandi va 4-bo'lim **faqat BEPUL Zoom akkauntlar** bilan qayta qurildi. Maqsad: video konferensiya yaratish va dars uzilib qolmasligini avtomatik nazorat qilish. Davomat, hisobot va statistika YO'Q. Boshqa bo'limlar (Group Post, Kanallarim, Tizim) o'zgarmagan.

## Asosiy g'oya
- Zoom akkauntlar puli (cheksiz sondagi bepul akkauntlar). Har akkaunt: Server-to-Server OAuth (Account ID, Client ID, Client Secret) + Zoom email. Secret `secure.py` bilan shifrlanadi.
- Kalendar dasturning o'zida (`zoom_schedule` / `zoom_lessons`), tashqi kalendar yo'q. `.ics` eksport bor.
- Dars boshlanishidan oldin bo'sh akkauntdan majlis yaratiladi (`start_url` — o'qituvchi uchun host havolasi, `join_url` — o'quvchilar uchun). 40 daqiqalik hisob guruhga havola ketgan paytdan boshlanadi (`group_link_min`, standart 5).
- **40 daqiqalik zanjir:** majlis tugashiga 3 daqiqa qolganda, dars rejada hali tugamagan bo'lsa, o'qituvchi botga BITTA «Davom etasizmi?» (✅/❌) oladi. «Davom etish» → boshqa bo'sh akkauntdan yangi majlis, yangi havola o'qituvchiga va guruhga; keyingi so'rov yangi majlis tugashidan 3 daqiqa oldin. «Bekor qilish» yoki javobsizlik zanjirni to'xtatadi.
- Hamma akkauntlar band bo'lsa: veb-platformada qizil ogohlantirish (har sahifada), Tizim tahlilida qator, adminga Telegram xabari; davom ettirish kutayotgan bo'lsa avtomatik qayta uriniladi.
- Parallel darslar: har dars alohida bo'sh akkauntdan oladi (band oynasi: [boshlanish−2 daq, tugash+4 daq]).

## Telegram
- **O'qituvchilar boti** (yagona bot, token Sozlamalarda, shifrlanadi): menyu — Bugungi darslar, Haftalik jadval, Guruhlarim, Hozirgi dars, Yordam. O'qituvchi `/start` bosganda @username bo'yicha bog'lanadi. Dars oldidan 20 va 10 daqiqada «darsingiz bor» xabari; 10 daqiqadagisida «Darsni boshlash» (host) tugmasi.
- **Guruhlarga** «Majlislar» Telegram akkaunti (Telethon, "Kanallarim" kabi alohida): 60 va 10 daqiqa eslatma (sozlanadi), 5 daqiqa oldin Zoom havolasi. Xabar yetmasa adminga ogohlantirish + Ogohlantirishlar sahifasida «Nusxalash» tugmasi.
- Vaqt mintaqasi: Asia/Tashkent.

## Fayllar (code/app/)
- `meet_zoom.py` — Zoom OAuth/REST (token keshi, 429 qayta urinish), `check_account` (token, user, majlis yaratish+o'chirish, start_url, join_before_host), Bot API yordamchilari.
- `meet_sched.py` — sxema (`zoom_*`), sozlamalar, o'qituvchilar, guruhlar, jadval, akkauntlar puli, majlis yaratish, zanjir (`continue_lesson`, `prompt_continue`), bekor qilish/ko'chirish, fon sikli (20 s), ogohlantirishlar, `.ics`, `health_items()`.
- `meet_bot.py` — o'qituvchilar boti (long polling).
- `meet_reports.py` — faqat kunlik hisobotga bitta varaq ("Majlislar").
- `r_meet.py` — `/meet/...` marshrutlar. `static/meet.css`, `templates/meet_*.html` (bosh sahifa, kalendar, o'qituvchilar, guruhlar/guruh, darslar/dars, Zoom akkauntlar, ogohlantirishlar, sozlamalar, diagnostika, Telegram akkaunt, yo'riqnoma, macros).
- **O'chirildi:** `meet_google.py`, `meet_track.py`, `templates/meet_reports.html`, `templates/meet_stats.html`, eski `tests/mock_google.py`. Ishga tushganda `mt_*` jadvallari (mt_teachers, mt_groups, mt_schedule, mt_lessons, mt_attendance, mt_settings, mt_alerts) avtomatik DROP qilinadi.

## Jadvallar
zoom_accounts, zoom_teachers, zoom_groups, zoom_schedule, zoom_lessons, zoom_meetings, zoom_settings, zoom_alerts.

## Mavjud fayllardagi o'zgarishlar
`main.py` (supervise: `meet_sched.loop`, `meet_bot.loop`), `syscheck.py` ("Majlislar (Zoom)" qatorlari), `templates/base.html` (nav/chip nomi), `dailyreport.py` (o'zgarmagan: `meet_reports.add_daily_sheet`).

## Zoom akkaunt qo'shish (har biri uchun)
Marketplace → Develop → Build App → **Server-to-Server OAuth** → Account ID/Client ID/Client Secret → Scopes: `meeting:write:meeting:admin`, `meeting:read:meeting:admin`, `meeting:update:meeting:admin`, `meeting:delete:meeting:admin`, `user:read:user:admin` → Activate. Keyin Majlislar → Zoom akkauntlar → qo'shish (yoki «Ko'plab qo'shish»: `email;account_id;client_id;client_secret[;max_min[;nom]]`).

## Sinov
`code/tests/` (mock Zoom + mock Telegram Bot API, pytest, 20 ta test): `TGP_TEST_DB=sqlite|mysql python -m pytest tests/test_meet.py -q`. Natija: SQLite 20/20, MariaDB 20/20. Tekshirilgan: akkaunt qo'shish/shifrlash/ommaviy qo'shish, diagnostika (scope xatosi), parallel darslar, hamma band → ogohlantirish, zanjir (so'rov → ✅ → boshqa akkaunt → havolalar), ❌ va javobsizlik, kerak bo'lmagan so'rov, band bo'lganda qayta urinish, bekor qilish/ko'chirish, yetmagan xabar + nusxalash, bot /start va menyular, jadval + `.ics`, barcha sahifalar render, kunlik varaq, Tizim tahlili, `mt_*` DROP.

## Haqiqiy xizmatlarda HALI TEKSHIRILMAGAN (birinchi ishga tushirishda ko'ring)
1. Bepul Zoom akkaunt API'ga (Server-to-Server OAuth) ruxsat berishi va scope nomlari — «Tekshirish» tugmasi sababini ko'rsatadi.
2. `start_url` bilan o'qituvchi Zoom akkauntisiz host bo'lib kira olishi.
3. Bepul akkauntda `join_before_host` ishlashi va 40 daqiqalik hisob aynan qachon boshlanishi (taxmin: guruhga havola ketganda).
4. Haqiqiy Telegram bot/Telethon yuborish, Windows DPAPI shifrlash.
5. Ish nusxasida boshqa bo'limlarning ba'zi shablonlari yo'q edi: Windows'dagi to'liq loyihada ishga tushirib ko'ring.

## Ishga tushirish (1 akkaunt bilan sinash)
1. Zoom'da 1 ta Server-to-Server ilova yarating, Zoom akkauntlar sahifasida qo'shing → «Tekshirish».
2. @BotFather'dan bot oching, tokenni Sozlamalarga kiriting; o'qituvchini @username bilan qo'shing; u botga /start bossin.
3. Telegram akkaunt (Majlislar) ulang; guruhni qo'shing, o'qituvchi va jadval belgilang (yoki «Qo'shimcha dars»).
