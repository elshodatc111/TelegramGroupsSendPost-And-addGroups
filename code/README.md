# Telegram Group Post

Telegram akkauntingizga ulangan guruh/kanallarga bir vaqtda (oraliq interval bilan) post yuboruvchi
**local web platforma**. Python + FastAPI + Telethon + SQLite. Faqat `127.0.0.1` da ishlaydi.

## Papka tuzilmasi
```
C:\TelegramGroupPost\
  data\   ← ma'lumotlar: app.db, sessions\, media\, logs\
  code\   ← shu loyiha kodi
    run.bat            ← ishga tushirish (Windows)
    requirements.txt
    app\
      main.py          ← veb-yo'nalishlar (routes)
      telegram_service.py ← Telegram (Telethon): kirish, guruhlar, yuborish
      sender.py        ← navbat, tasodifiy interval, FloodWait, rejalashtirish
      db.py, config.py
      templates\, static\
```

## Ishga tushirish
1. Python 3.10+ o'rnatilgan bo'lsin.
2. **`C:\TelegramGroupPost\run.bat`** (yoki `code\run.bat`) ni ikki marta bosing. Birinchi marta Python muhiti va kutubxonalar avtomatik o'rnatiladi (Python yo'q bo'lsa winget orqali o'rnatishga urinadi), keyin brauzerda `http://127.0.0.1:8000` o'zi ochiladi. Keyingi safar bir zumda ishga tushadi.
3. **Sozlamalar** → https://my.telegram.org dan olingan `api_id` / `api_hash` → telefon raqam → kod (→ 2FA parol).
4. **Guruhlar** → "Telegramdan yangilash". Kerak bo'lsa toifa (ro'yxat) yarating.
5. **Post yaratish** → matn (+ rasm/video) → guruhlarni tanlash → interval → **Oldindan ko'rish** → **Tasdiqlash**.
6. **Yuborishlar** sahifasida jonli progress, to'xtatish/davom ettirish va tarix.

## Excel orqali guruhlarga a'zo bo'lish
**A'zo bo'lish** bo'limida .xlsx/.csv/.txt fayl yuklanadi (`@username`, `t.me/username`, `t.me/+taklif` formatlari). Dastur ustunlarni o'zi topadi, siz tanlaysiz,
so'ng xavfsiz tezlikda (standart 60-180s, kuniga ~40 ta) a'zo bo'ladi. Limit to'lsa avtomatik kutib davom etadi.
Hisobot: Ulandi / Allaqachon a'zo / So'rov yuborildi / Ulanmadi (sababi bilan), filtr, qidiruv, Excel'ga eksport, xatolarni qayta urinish.
Xatolar `data\logs\app.log` fayliga yoziladi.

## Imkoniyatlar
- Post turlari: matn, matn+rasm, matn+video (HTML format ixtiyoriy)
- Guruh tanlash, qidiruv, saqlangan ro'yxatlar (toifalar)
- Oldindan ko'rish → faqat tasdiqlangandan keyin yuboriladi
- Har guruh orasida min–max tasodifiy interval; FloodWait/SlowMode'da avtomatik kutish
- Jonli progress, to'xtatish, uzilgan yuborishni davom ettirish
- Vaqtga rejalashtirish (dastur ochiq turgan bo'lsa ishlaydi)
- Shablonlar, yuborishlar tarixi

## Muhim eslatmalar
- Bu **user account** (MTProto) orqali ishlaydi, shuning uchun Telegram cheklovlariga rioya qiling: uzun interval (30–120s), kam takror, faqat o'zingiz a'zo bo'lgan guruhlarga yuboring. Ommaviy reklama spam hisoblanib akkaunt cheklanishi mumkin.
- `data\sessions` papkasi akkauntingizga kirish kalitini saqlaydi — hech kimga bermang.
- Rejalashtirilgan postlar uchun kompyuter va dastur o'sha vaqtda yoqilgan bo'lishi kerak.

## Kengaytirish g'oyalari (keyingi bosqichlar)
Bir nechta akkaunt · avtomatik takrorlanuvchi postlar (kunlik/haftalik) · guruh mavzulari (forum topic) ·
o'zgaruvchilar ({guruh_nomi}) · bir nechta rasm (albom) · statistika/eksport (CSV) · parol bilan himoya ·
Telegram bot orqali bildirishnoma · alembic migratsiyalari.
