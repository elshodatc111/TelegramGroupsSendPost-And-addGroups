# PROJECT v10 — "Majlislar" (Google Meet darslari) bo'limi

v9 ustiga 4-bo'lim qo'shildi. Mavjud bo'limlar (Group Post, Kanallarim, Tizim) o'zgarmagan; faqat kunlik hisobotga bitta varaq ("Majlislar") va Tizim tahliliga "Majlislar (Google Meet)" guruhi qo'shildi.

## Yangi fayllar (code/app/)
- `meet_sched.py` — vaqt (Asia/Tashkent), 7 jadval DDL (`ensure_schema()` lifespan'dan chaqiriladi, db.py'ga tegilmagan), sozlamalar, o'qituvchilar, guruhlar, jadval, darslar generatsiyasi, Meet+Calendar provisioning, bekor/ko'chirish, Telegram yuborish (alohida "meet" akkaunt), ogohlantirishlar, eslatma sikli, `health_items()`.
- `meet_google.py` — OAuth (Desktop client, loopback, PKCE, tokenlar secure.py bilan shifrlanadi), Meet REST v2 (spaces, members/co-host, conferenceRecords, participants, participantSessions), Calendar v3, `diagnose()`.
- `meet_track.py` — davomat polling (30–60 s), kirish/chiqish, jami daqiqa, kech/erta (standart 10/10), ismlarni birlashtirish, o'qituvchi aniqlash, ko'p odam ogohlantirishi, dars tugagach hisobot.
- `meet_reports.py` — oylik rangli jadval, Excel eksport, statistika, kunlik hisobot varag'i.
- `r_meet.py` — barcha `/meet/...` marshrutlar. `static/meet.css` — zumrad/teal/sariq mavzu, qorong'u rejim.
- `templates/meet_*.html` — 15 ta shablon (bosh sahifa, kalendar, o'qituvchilar, guruhlar, darslar, hisobot, statistika, ogohlantirishlar, diagnostika, sozlamalar, akkaunt, yo'riqnoma).

## Jadvallar
mt_teachers, mt_groups, mt_schedule, mt_lessons, mt_attendance, mt_settings, mt_alerts. Dars bo'yicha yuborilgan xabarlar `mt_lessons.flags` (JSON) da; ism birlashtirish va ro'yxat `mt_settings` da (`alias_<gid>`, `roster_<gid>`).

## Mavjud fayllardagi minimal o'zgarishlar
- `main.py`: import, `meet_sched.ensure_schema()`, supervise ostida `meet_sched` va `meet_track` sikllari, `ws=meet` cookie yo'naltirishi, router.
- `templates/base.html`: bo'lim tanlagichiga "Majlislar", meet navigatsiyasi, meet.css faqat `ws=='meet'` da.
- `syscheck.py`: `meet_sched.health_items()` qo'shildi (token, oxirgi polling, xatolar, faol majlislar, "Meet limiti: 60 daqiqa (Gmail)").
- `dailyreport.py`: `meet_reports.add_daily_sheet(wb, day)`.

## Asosiy qarorlar
- Har dars uchun yangi Meet xonasi (accessType OPEN), o'qituvchi gmaili avtomatik COHOST. Begonani chiqarish tugmasi va begona aniqlash tizimi yo'q.
- Har Telegram guruh uchun alohida Google Calendar; bosh sahifada umumiy kalendar.
- Havola: darsdan 10 daq oldin o'qituvchiga (@username), 5 daq oldin guruhga. Yetmasa adminga ogohlantirish + "nusxalash" tugmasi.
- Guruhga eslatma 60 va 10 daq oldin (sozlanadi); o'qituvchi 5 daq kechiksa admin xabari; dars tugagach qisqa hisobot.
- >60 daqiqa dars yaratishda ogohlantiriladi (Gmail limiti), ikkinchi havola yechimi yo'q.
- Record havolasi qo'lda kiritiladi. Host akkaunt sozlamada almashtiriladi.
- Google manzillari env orqali almashtiriladi (`TGP_GOOGLE_AUTH/TOKEN/USERINFO/CAL/MEET`) — sinov uchun.

## Sinov
`code/tests/` (mock Google serveri + pytest, 43 ta test): `TGP_TEST_DB=sqlite|mysql python -m pytest tests/test_meet.py -q`. Natija: sqlite 43/43, MariaDB 43/43.

## Haqiqiy xizmatlarda hali tekshirilmagan
- Haqiqiy Google (shaxsiy Gmail): co-host (spaces.members) va participants API ishlashi — "Google diagnostika" tugmasi buni ko'rsatadi.
- Haqiqiy Telegram yuborish, Windows DPAPI shifrlash, toast bildirishnomalar.
- Ish nusxasida mavjud bo'limlarning ba'zi shablonlari yo'q edi, shu sabab ularning render testi o'tkazib yuboriladi; Windows'dagi to'liq loyihada ishga tushirib ko'ring.

## Ishga tushirish tartibi
1. Yo'riqnoma → Google Cloud sozlash (Meet API + Calendar API, OAuth kalit, ilovani **Production**ga o'tkazish — aks holda token 7 kundan keyin o'chadi).
2. Majlislar → Sozlamalar: Client ID/Secret, "Google'ga ulash".
3. Akkaunt: Majlislar uchun Telegram akkaunt. O'qituvchi → guruh → jadval.
4. "Google diagnostika" ni bosing.
