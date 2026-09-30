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

## Imkoniyatlar (v4)
- **Kirish**: lokal, bitta foydalanuvchi; ixtiyoriy parol (Sozlamalar). Google orqali kirish va ko'p foydalanuvchi rejimi olib tashlandi.
- **Isitish rejasi** (`/warmup`): kunlik post va a'zo bo'lish limiti (14 kunlik standart, tahrirlanadi); limitga yetganda to'xtaydi, ertasi kuni davom etadi.
- **Tahlil**: eng yaxshi vaqt (soat/kun xaritasi, Statistika sahifasida), guruhlar tahlili (`/group-stats`: a'zolar, 7/30 kun dinamika, ko'rishlar, ball 0-100), guruh tafsiloti grafiklari.
- **Guruhdan chiqish** (`/groups/leave`): yozish taqiqlangan/ban/o'chirilgan postlar va past ko'rish nomzodlari; avtomatik (6 soatda bir, 5 tagacha) yoki qo'lda; chiqilgan guruh qora ro'yxatga tushadi.
- **Kalendar** (`/calendar`): yuborishlar va takrorlanuvchi kampaniyalar; rejalashtirilganini sudrab ko'chirish.
- **Kunlik hisobot** (`/reports`): Excel, har kuni avtomatik saqlanadi (`data/reports/`).
- **Guruhlar Excel import/eksport**; **bir nechta akkauntdan parallel yuborish** (compose'da).
- Github: faqat kod (`.gitignore` data/ ni chiqarmaydi); boshqa kompyuterda ma'lumotlar avtomatik tozalanadi (`machine.py`).

## Cheklovlar / qarorlar
- Foydalanuvchi akkauntlari inline URL tugma yubora olmaydi (faqat bot) — qo'shilmadi.
- API ID/HASH har foydalanuvchida alohida (foydalanuvchining barcha akkauntlari uchun bitta).
- Lead kuzatuvi, proxy, Instagram: keyinga qoldirilgan (Instagram keyingi bosqich).
- Keyinga qoldirilgan: AI (matn yaratish), Telegram bot bildirishnomalari, Windows avtoishga tushirish/zaxira, .exe.

## Modullar (app/)
main.py (ilova, parol, middleware) · r_account/r_posts/r_groups/r_join/r_stats/r_inbox (marshrutlar) · sender.py (yuborish, rejalashtiruvchi) ·
joiner.py (a'zo bo'lish) · jobsvc.py (variantlar, kampaniya) · jobops.py (statistika/o'chirish/tahrir) · limits.py (salomatlik, chegaralar) ·
telegram_service.py (Telethon, akkaunt bo'yicha) · accounts.py · reports.py · textutil.py · db.py (SQLite + migratsiya)

## Ma'lumotlar bazasi
Eski bazadan avtomatik migratsiya: `account.session` -> akkaunt #1, eski postlar -> 1 variantli yuborishlar, shablonlar -> kampaniyalar.

## Sinov holati
Soxta Telegram xizmati bilan avtomatik sinovdan o'tgan (variant rotatsiyasi va davomi, akkauntlar ajratilishi, statistika/eksport, inbox, parol). Haqiqiy akkaunt bilan sinash kerak.

## v5 — Avto-topish (o'zbek auditoriyali guruhlarni topish va a'zo bo'lish)
- Sahifa: `/autojoin` (yon menyu: Avto-topish). Modul: `discovery.py`, marshrutlar: `r_autojoin.py`, jadvallar: `autojoin`, `disc_candidates`.
- Kategoriyalar: dasturlash/IT, o'quv markazlari, universitet/maktab/bog'cha, biznes, koreys tili, avtomatlashtirish (tahrirlash mumkin).
- Oqim: qidirish (`search_public`) -> tekshirish (`inspect_public`: a'zolar, kunlik xabar, o'zbek tili ulushi) -> filtr (`classify`) -> kuniga N ta a'zo bo'lish (mavjud `joiner` orqali, isitish rejasi bilan cheklanadi) -> N kun kutish -> qoidalar tekshiruvi -> `avto-tayyor` tegi (Post yaratishda shu teg bo'yicha tanlanadi).
- Holatlar: ready, review (qo'lda), approved, queued, joined/requested, active, noads, blocked, low, failed, rejected.
- Qonuniylik filtri: `DEFAULT_BANS` (~680 so'z, uz lotin/kirill, rus, ingliz; 9 kategoriya: Diniy, Da'vat, Zo'ravonlik, 18+, Qimor, Firibgarlik, Giyohvand/alkogol, Siyosat, Gumonli/xavfli). Ro'yxat `/autojoin?tab=bans` sahifasida kategoriya bo'yicha to'liq tahrirlanadi (`autojoin.ban_json`), 'Standartni tiklash' va 'Tekshirib ko'rish' bor, 'Mening taqiq so'zlarim' alohida. Moslik so'z boshidan; oxiriga `$` qo'yilsa butun so'z. Nom+tavsif+xabarlar tekshiriladi. Taqiqlangan guruh tasdiqlanmaydi; ro'yxat saqlanganda kutilayotgan nomzodlar `recheck` bilan qayta tekshiriladi.
- Faqat toza ('ready') guruhlarga avtomatik a'zo bo'linadi; matn kam/mavzu mos emas/admin tasdig'i kerak bo'lsa 'review' (qo'lda).
- Sahifa 4 tabli: Umumiy, Nomzodlar, Taqiq ro'yxati, Sozlamalar.
- Diqqat: filtr kalit so'zlarga asoslangan yordamchi vosita, huquqiy kafolat emas. Kuniga 20 ta a'zo bo'lish FloodWait/cheklovga olib kelishi mumkin (xavfsiz: 5-15). Haqiqiy akkauntda sinab ko'rish kerak.
- Ishga tushirish: har 20 daqiqada `discovery.tick()`; akkaunt o'chirilsa nomzodlar ham o'chadi.

## v6 — Guruh auditi (`/audit`)
- Modul: `auditor.py`, marshrutlar: `r_audit.py`, sahifa: `templates/audit.html`, jadval: `audit_cfg`, `groups` ustunlari: `muted, verdict, verdict_reason, audited_at`.
- Oqim: **Tahlil** (guruhlar ro'yxati yangilanadi, har guruh uchun `group_info`: a'zolar soni + tavsifdagi reklama taqiqi + yozish huquqi) -> **Hukm** (`leave`/`keep`/`unknown`) -> **Chiqish** -> **Mute** (barcha qolgan guruhlar).
- Chiqish sharti: a'zolar < min (standart 100) YOKI yozib bo'lmaydi YOKI qoidada reklama taqiqlangan (har biri sozlamada o'chiriladi). `saqlash` tegi bilan himoyalangan guruhlardan chiqilmaydi.
- Xavfsizlik: kunlik chiqish chegarasi (standart 10), tasodifiy kechikish 25–60 s, faol yuborish bor bo'lsa chiqilmaydi, chiqilgan guruh qora ro'yxatga tushadi (Avto-topish ham qayta a'zo bo'lmaydi). Belgilab qo'lda chiqishda kunlik chegara qo'llanmaydi.
- Mute: `TelegramService.mute` — `mute_until=2147483647`, previews o'chiq, silent (Telegram'da "abadiy mute"); xabarlar keladi, lekin bildirishnoma/ovoz yo'q. Allaqachon mute bo'lsa tegilmaydi.
- Avtomatik rejim: har 30 daqiqada `auditor.tick` (12 soatda bir tahlil, chiqish, yangi guruhlarni mute).
- `fetch_groups` endi akkauntga xos yozish cheklovini (banned_rights) ham hisobga oladi.

## v6.1 — Faqat reklama yuborish mumkin bo'lgan guruhlar
- `TelegramService.check_target(kind,key,min_members,check_ads)`: a'zo bo'lishdan OLDIN tekshiradi — kanal (broadcast) emasmi, a'zolar yozish huquqi bormi, a'zolar soni >= min (Guruh auditi sozlamasi, standart 100), tavsif va mahkamlangan (pinned) xabarda reklama taqiqi yo'qmi. Excel/qidiruv/avto-topish paketlari (`joiner._join_one`) mos kelmaganini `invalid` ("Mos emas: sabab") deb belgilaydi va a'zo bo'lmaydi. O'chirish: `settings.join_precheck = 0`.
- `/discover` qidiruvi `strict` rejimda: kanallar, yozib bo'lmaydiganlar, a'zosi kam va reklama taqiqlangan guruhlar yashiriladi (sabab bo'yicha soni ko'rsatiladi).
- Avto-topish `classify`: `ads_flag` (tavsif + pinned) bo'lsa 'low'; a'zolar chegarasi = max(avto-topish, audit).
- `ads_prohibited` va `group_info` endi mahkamlangan xabarni ham tekshiradi (audit ham shundan foydalanadi).

## v6.2 — Kanallar auditga kiritildi
- Sabab: `fetch_groups` yozish huquqi yo'q kanallarni butunlay tashlab yuborardi, shuning uchun ular "Mening guruhlarim"da ko'rinmasdi va audit ularni ko'rmasdi (chiqib ketilmasdi). Endi kanallar ham `groups` jadvaliga `kind='channel', can_post=0` bilan yoziladi (compose'da o'chirilgan ko'rinadi), audit ularni "Kanal: reklama yozib bo'lmaydi" sababi bilan chiqishga belgilaydi.
- Audit sahifasiga "Hammasini bajarish" (tahlil -> chiqish -> mute) tugmasi qo'shildi (`auditor.run_all`).
