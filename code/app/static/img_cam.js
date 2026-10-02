/* Image Generator: odamni kamera orqali ro'yxatga olish (5 ta yo'naltirilgan kadr). Hech narsa OpenAI'ga yuborilmaydi: rasmlar faqat shu kompyuterda saqlanadi. */
(function () {
  const SHOTS = [['front', "To'g'ri kameraga qarang, yuz neytral"], ['left34', "Boshingizni chapga 3/4 burchakka buring"], ['right34', "Boshingizni o'ngga 3/4 burchakka buring"],
                 ['smile', "Tabassum qiling (tishlar ko'rinsin)"], ['up', "Biroz yuqoriga qarang (iyak ko'rinsin)"]];
  const $ = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h) e.innerHTML = h; return e; };

  function quality(canvas, face) {
    const w = 96, h = Math.round(96 * canvas.height / canvas.width), c = $('canvas'); c.width = w; c.height = h;
    const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(canvas, 0, 0, w, h);
    const d = x.getImageData(0, 0, w, h).data, g = new Float32Array(w * h); let sum = 0;
    for (let i = 0; i < w * h; i++) { g[i] = .299 * d[i * 4] + .587 * d[i * 4 + 1] + .114 * d[i * 4 + 2]; sum += g[i]; }
    const mean = sum / (w * h); let v = 0, n = 0;
    for (let y = 1; y < h - 1; y++) for (let xx = 1; xx < w - 1; xx++) { const i = y * w + xx, l = 4 * g[i] - g[i - 1] - g[i + 1] - g[i - w] - g[i + w]; v += l * l; n++; }
    const sharp = v / n, w2 = [];
    if (mean < 60) w2.push("Juda qorong'i: yorug'roq joyga o'ting");
    if (mean > 205) w2.push("Juda yorqin: yorug'likni kamaytiring");
    if (sharp < 60) w2.push("Rasm xira: kamerani qimirlatmang, yaqinroq turing");
    if (face === 0) w2.push("Yuz topilmadi");
    if (face > 1) w2.push("Kadrda bir nechta yuz bor");
    return w2;
  }

  async function open(btn) {
    const form = document.querySelector(btn.dataset.cam);
    if (!form) return;
    const shots = new Array(SHOTS.length).fill(null); let cur = 0, stream = null, detector = null;
    try { if ('FaceDetector' in window) detector = new window.FaceDetector({ fastMode: true, maxDetectedFaces: 3 }); } catch (e) { }
    const m = $('div', 'modal open'), box = $('div', 'box'); m.appendChild(box);
    box.innerHTML = '<div class="card-h"><h2>Kamera orqali ro\'yxatga olish</h2><button type="button" class="btn sm ghost" data-x>Yopish</button></div>' +
      '<p class="mut small" style="margin-top:0">5 ta kadr olinadi. Rasmlar faqat shu kompyuterda saqlanadi; ular odam qiyofasi videolarda o\'zgarmasligi uchun referens bo\'ladi. Yaxshi yoritilgan joyda, ko\'zoynaksiz va bosh kiyimsiz oling.</p>' +
      '<div class="cam-wrap"><video autoplay playsinline muted></video><div class="cam-oval"></div></div>' +
      '<div class="cam-step"></div><div class="cam-warn"></div>' +
      '<div class="actions" style="margin:10px 0"><button type="button" class="btn acc" data-snap>Suratga olish</button><label class="ig-chk"><input type="checkbox" data-timer checked> 3 soniya taymer</label></div>' +
      '<div class="cam-thumbs"></div><div class="actions" style="margin-top:12px"><button type="button" class="btn" data-done disabled>Tayyor: qo\'shish</button></div>';
    document.body.appendChild(m);
    const video = box.querySelector('video'), step = box.querySelector('.cam-step'), warn = box.querySelector('.cam-warn'), th = box.querySelector('.cam-thumbs');
    const close = () => { if (stream) stream.getTracks().forEach(t => t.stop()); m.remove(); };
    box.querySelector('[data-x]').onclick = close;
    const draw = () => {
      step.innerHTML = '<b>' + (cur + 1) + '/' + SHOTS.length + '.</b> ' + SHOTS[cur][1];
      th.innerHTML = '';
      SHOTS.forEach((s, i) => { const b = $('button', 'cam-th' + (i === cur ? ' cur' : '')); b.type = 'button'; b.title = s[1];
        b.innerHTML = shots[i] ? '<img src="' + shots[i].url + '" alt="">' : '<span>' + (i + 1) + '</span>'; b.onclick = () => { cur = i; draw(); }; th.appendChild(b); });
      box.querySelector('[data-done]').disabled = shots.some(x => !x);
    };
    draw();
    try { stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 960 }, facingMode: 'user' }, audio: false }); video.srcObject = stream; }
    catch (e) { warn.textContent = "Kameraga ruxsat berilmadi yoki kamera topilmadi. Brauzer manzil satridagi kamera belgisidan ruxsat bering yoki rasmlarni fayldan yuklang."; return; }
    const snap = async () => {
      if (!video.videoWidth) return;
      const c = $('canvas'); c.width = video.videoWidth; c.height = video.videoHeight; c.getContext('2d').drawImage(video, 0, 0);
      let faces = -1;
      if (detector) { try { const f = await detector.detect(c); faces = f.length; if (f.length === 1 && f[0].boundingBox.width < c.width * .18) faces = -2; } catch (e) { } }
      const w = quality(c, faces === -2 ? -1 : faces); if (faces === -2) w.push("Yuz juda kichik: kameraga yaqinlashing");
      const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', .92));
      if (shots[cur]) URL.revokeObjectURL(shots[cur].url);
      shots[cur] = { blob, url: URL.createObjectURL(blob) };
      warn.textContent = w.length ? w.join('. ') + '. Kerak bo\'lsa shu kadrni qayta oling.' : 'Yaxshi.';
      const nxt = shots.findIndex(x => !x); if (nxt >= 0) cur = nxt;
      draw();
    };
    box.querySelector('[data-snap]').onclick = () => {
      const t = box.querySelector('[data-timer]').checked ? 3 : 0, b = box.querySelector('[data-snap]');
      if (!t) return snap();
      let n = t; b.disabled = true; b.textContent = n; const iv = setInterval(() => { n--; if (n <= 0) { clearInterval(iv); b.disabled = false; b.textContent = 'Suratga olish'; snap(); } else b.textContent = n; }, 1000);
    };
    box.querySelector('[data-done]').onclick = () => {
      form.querySelectorAll('input[data-camfile]').forEach(x => x.remove());
      const dt = new DataTransfer();
      shots.forEach((s, i) => dt.items.add(new File([s.blob], 'cam_' + SHOTS[i][0] + '.jpg', { type: 'image/jpeg' })));
      const inp = $('input'); inp.type = 'file'; inp.name = 'photos'; inp.multiple = true; inp.hidden = true; inp.dataset.camfile = '1'; inp.files = dt.files; form.appendChild(inp);
      const note = form.querySelector('[data-cam-note]'); if (note) note.textContent = SHOTS.length + " ta kadr tayyor: formani yuboring.";
      close();
      if (btn.hasAttribute('data-cam-submit')) form.submit();
    };
  }
  document.querySelectorAll('[data-cam]').forEach(b => b.addEventListener('click', () => open(b)));
})();
