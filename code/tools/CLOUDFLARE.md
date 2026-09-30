# Kuzatuv havolasi (Cloudflare Worker) — bir martalik sozlash

Maqsad: har bir post uchun alohida havola bo'lsin (masalan `https://sizning-nom.workers.dev/c1/ab12cd`),
u bosilganda foydalanuvchi o'zgarmas botingiz havolasiga (`t.me/atkobot?start=telegram`) o'tadi,
dastur esa qaysi postdan nechta bosish bo'lganini hisoblaydi. Botning o'z statistikasi o'zgarmaydi.

1. https://dash.cloudflare.com ga kiring (bepul akkaunt ochish mumkin).
2. **Storage & Databases → KV → Create** bosing, nomi: `tgpost-clicks`.
3. **Workers & Pages → Create → Create Worker** bosing, nom bering (masalan `atko-go`), **Deploy** qiling, so'ng **Edit code**.
4. Ichidagi hamma kodni o'chirib, `cloudflare_worker.js` faylidagi kodni qo'ying va **Deploy** bosing.
5. Worker sahifasida **Settings → Bindings → Add → KV namespace**: Variable name = `CLICKS`, namespace = `tgpost-clicks`.
6. **Settings → Variables and Secrets → Add**: Type = Secret, nomi `SECRET`, qiymati — o'zingiz o'ylab topgan uzun parol (kamida 20 belgi). **Deploy**.
7. Dasturda **Kanallarim → Sozlamalar → Kuzatuv havolasi** bo'limiga Worker manzilini (`https://atko-go.<nom>.workers.dev`) va shu `SECRET` ni kiriting, **Saqlash va tekshirish** bosing.

Eslatmalar
- Bepul KV tarifida kuniga 1000 ta yozuv limiti bor: kuniga 1000 dan ko'p bosish bo'lsa, ortig'i sanalmaydi (havola baribir ishlayveradi). Tizim tahlili bo'limi buni ko'rsatadi.
- Bu "bosish" soni: bot ochilishi va /start bosilishi yig'indisi emas, taxminiy yaqin ko'rsatkich.
- SECRET ni hech kimga bermang. Dastur uni shifrlab saqlaydi.
