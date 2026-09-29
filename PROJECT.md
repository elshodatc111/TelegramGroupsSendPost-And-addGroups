# Telegram Group Post — loyiha xotirasi

**Maqsad:** Telegram akkauntga ulangan guruhlarga interval bilan ommaviy post yuborish uchun local web platforma (Python).
**Joylashuv:** `C:\TelegramGroupPost\` → `data\` (ma'lumotlar), `code\` (kod). Faqat web platforma.

## Qarorlar
- Framework: FastAPI + Jinja2 (server-side render), oddiy JS (polling orqali jonli progress)
- Telegram: Telethon (user account/MTProto); API ID/HASH va login web'dan sozlanadi
- DB: SQLite (`data\app.db`), session `data\sessions\account.session`, media `data\media`
- Post turlari: text, text+image, text+video; format: oddiy yoki HTML
- Interval: har guruh orasida min–max tasodifiy (default 20–60s, min 3s); FloodWait/SlowMode avtomatik kutish
- Jarayon: post tayyorlash → guruh tanlash → **preview** → tasdiqlash → navbat (bir vaqtda bitta job)
- Qo'shimcha: jonli progress+to'xtatish/davom, rejalashtirish, guruh ro'yxatlari, shablonlar, tarix
- Faqat 127.0.0.1 da tinglaydi (parolsiz, local foydalanish)

## v2 (Excel a'zo bo'lish + dizayn)
- Yangi modullar: excel_import.py (parse/eksport), joiner.py (a'zo bo'lish navbati), web.py (umumiy web yordamchilar)
- A'zo bo'lish: default 60-180s, kuniga 40 (24 soatlik oyna), FloodWait>5daq bo'lsa 'waiting' + avtomatik davom; ChannelsTooMuch bo'lsa to'xtaydi
- Video: hachoir metadata (DocumentAttributeVideo), bir marta yuklab boshqa guruhlarga qayta ishlatish (media cache), yuklash progressi
- Loglar: data\logs\app.log; global xato sahifasi
- Dizayn: yon panel, qorong'u/yorug' rejim, jonli ko'rinish, dashboard

## Holat
v1 tayyor (smoke-test o'tgan, Telegram'siz mock bilan). Haqiqiy akkaunt bilan birinchi sinov foydalanuvchi tomonidan.

## Kengaytirish rejasi
Ko'p akkaunt, takrorlanuvchi postlar, forum topic, o'zgaruvchilar, albom, statistika/CSV, parol himoyasi.
