// Telegram Group Post: kuzatuv havolalari uchun Cloudflare Worker.
// Havola: https://<worker>/<kanal-kalit>/<post-kod>  ->  kanal sozlamasidagi o'zgarmas havolaga yo'naltiradi
// va bosishlarni kun bo'yicha sanaydi. Bot/havola ko'rib chiqish (preview) so'rovlari sanalmaydi.
// Sozlash: Worker -> Settings -> Bindings: KV namespace (nomi: CLICKS) va Variables and Secrets: SECRET (matn).
const BOT_UA = /(bot|crawler|spider|preview|facebookexternalhit|slurp|whatsapp|skypeuri|discord|vkshare)/i;

function day() {                       // Toshkent vaqti (UTC+5)
  const d = new Date(Date.now() + 5 * 3600 * 1000);
  return d.toISOString().slice(0, 10);
}
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json" } });

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const parts = url.pathname.split("/").filter(Boolean);

    if (parts[0] && parts[0].startsWith("_")) {             // boshqaruv: faqat kalit bilan
      if (!env.SECRET || url.searchParams.get("key") !== env.SECRET) return json({ error: "forbidden" }, 403);
      if (parts[0] === "_ping") return json({ ok: true, kv: !!env.CLICKS });
      if (parts[0] === "_set" && req.method === "POST") {
        const b = await req.json();
        if (!/^[a-z0-9_-]{1,32}$/i.test(b.ch || "") || !/^https:\/\//.test(b.to || "")) return json({ error: "bad" }, 400);
        await env.CLICKS.put("t:" + b.ch, b.to);
        return json({ ok: true });
      }
      if (parts[0] === "_stats") {
        const out = {};
        let cursor;
        do {
          const r = await env.CLICKS.list({ prefix: "c:", cursor, limit: 1000 });
          for (const k of r.keys) {
            const [, ch, code, d] = k.name.split(":");
            const n = parseInt(await env.CLICKS.get(k.name)) || 0;
            ((out[ch] = out[ch] || {})[code] = out[ch][code] || {})[d] = n;
          }
          cursor = r.list_complete ? null : r.cursor;
        } while (cursor);
        return json(out);
      }
      return json({ error: "not found" }, 404);
    }

    if (parts.length === 2) {
      const [ch, code] = parts;
      const to = await env.CLICKS.get("t:" + ch);
      if (!to) return new Response("Havola topilmadi", { status: 404 });
      if (!BOT_UA.test(req.headers.get("user-agent") || "")) {
        ctx.waitUntil((async () => {
          try {
            const k = `c:${ch}:${code}:${day()}`;
            const n = (parseInt(await env.CLICKS.get(k)) || 0) + 1;
            await env.CLICKS.put(k, String(n));
          } catch (e) { /* KV bepul limiti (kuniga 1000 yozuv) tugagan bo'lishi mumkin: yo'naltirish baribir ishlaydi */ }
        })());
      }
      return Response.redirect(to, 302);
    }
    return new Response("OK", { status: 200 });
  },
};
