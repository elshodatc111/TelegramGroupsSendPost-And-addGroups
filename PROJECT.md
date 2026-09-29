# Telegram Group Post — loyiha hujjati

Local kompyuterda ishlaydigan veb-platforma (Python, FastAPI + Jinja2, Telethon/MTProto). Telegram akkaunt(lar)dagi guruhlarga
reklama postlarini (matn, matn+rasm, matn+video, albom) tasodifiy interval bilan yuboradi.

## Papkalar
- `C:\TelegramGroupPost\data\`  — baza (app.db), sessionlar, media, importlar, loglar
- `C:\TelegramGroupPost\code\`  — dastur kodi (`app\` paketi, `requirements.txt`, `run.bat`)
- `C:\TelegramGroupPost\run.bat` — bir marta bosib ishga tushirish (venv, kutubxonalar, brauzer)

## Imkoniyatlar (v3)
- **Ko'p akkaunt**: tepada akkaunt tanlagich; guruhlar, yuborishlar, kampaniyalar, a'zo bo'lish, statistika, inbox — tanlangan akkauntga bog'liq. Session: `data/sessions/acc_<id>.session`.
- **Post variantlari**: har variantning o'z matni va media (rasm, albom<=10, video). Guruhlarga qat'iy ketma-ket (V1,V2,V3,V1...), bitta guruhga bitta variant, ketma-ket bir xil emas. Boshlanish nuqtasi oldingi yuborishdan davom etadi (`variant_ptr`).
- **Xavfsizlik**: akkaunt salomatligi (FloodWait/xato asosida sekinlashtirish), ish vaqti oynasi, kunlik limit, spintax `{a|b}`, `{guruh}`, `{username}`, qora ro'yxat (yozish taqiqlansa avtomatik), reklama taqiqi aniqlash.
- **Kampaniyalar**: saqlash, qayta ishga tushirish, takrorlash (kunlik/haftalik), A/B (variantlar), UTM, yuborilgan xabarlarni tahrirlash/o'chirish.
- **Statistika**: ko'rishlar/reaksiya/javoblar, kunlik grafik, variantlar taqqoslash, guruhlar reytingi, Excel va PDF eksport.
- **Guruhlar**: teglar, ro'yxatlar, qoidalarni tekshirish, qidiruv (discover), a'zolar soni dinamikasi, Excel orqali ommaviy a'zo bo'lish + hisobot.
- **Inbox**: javoblar, eslatmalar, shaxsiy xabarlar; tayyor javoblar.
- **Media kutubxona**, **parol himoyasi** (Sozlamalar), qizil/ko'k/oq dizayn (qorong'u rejim bor).

## Cheklovlar / qarorlar
- Foydalanuvchi akkauntlari inline URL tugma yubora olmaydi (faqat bot) — qo'shilmadi.
- Bitta API ID/HASH barcha akkauntlar uchun.
- Keyinga qoldirilgan: AI (matn yaratish), Telegram bot bildirishnomalari, Windows avtoishga tushirish/zaxira, .exe.

## Modullar (app/)
main.py (ilova, parol, middleware) · r_account/r_posts/r_groups/r_join/r_stats/r_inbox (marshrutlar) · sender.py (yuborish, rejalashtiruvchi) ·
joiner.py (a'zo bo'lish) · jobsvc.py (variantlar, kampaniya) · jobops.py (statistika/o'chirish/tahrir) · limits.py (salomatlik, chegaralar) ·
telegram_service.py (Telethon, akkaunt bo'yicha) · accounts.py · reports.py · textutil.py · db.py (SQLite + migratsiya)

## Ma'lumotlar bazasi
Eski bazadan avtomatik migratsiya: `account.session` -> akkaunt #1, eski postlar -> 1 variantli yuborishlar, shablonlar -> kampaniyalar.

## Sinov holati
Soxta Telegram xizmati bilan avtomatik sinovdan o'tgan (variant rotatsiyasi va davomi, akkauntlar ajratilishi, statistika/eksport, inbox, parol). Haqiqiy akkaunt bilan sinash kerak.
