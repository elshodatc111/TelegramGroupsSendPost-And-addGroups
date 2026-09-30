// ===== Umumiy interaktivlik =====
(function () {
  // Mavzu almashtirish
  const root = document.documentElement;
  function paintTheme() {
    document.querySelectorAll('[data-theme-label]').forEach(el => {
      el.textContent = root.dataset.theme === 'dark' ? "Yorug' rejim" : "Qorong'u rejim";
    });
    document.querySelectorAll('[data-theme-icon]').forEach(el => {
      el.innerHTML = '<use href="#i-' + (root.dataset.theme === 'dark' ? 'sun' : 'moon') + '"/>';
    });
  }
  document.querySelectorAll('[data-theme-toggle]').forEach(b => b.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('theme', root.dataset.theme); } catch (e) {}
    paintTheme();
  }));
  paintTheme();

  // Mobil menyu
  const side = document.getElementById('side');
  const burger = document.getElementById('burger');
  if (burger && side) {
    burger.addEventListener('click', () => side.classList.toggle('open'));
    document.addEventListener('click', e => {
      if (side.classList.contains('open') && !side.contains(e.target) && !burger.contains(e.target)) side.classList.remove('open');
    });
  }

  // Ochiladigan menyular (akkaunt tanlagich)
  document.querySelectorAll('.dd > [data-dd]').forEach(btn => btn.addEventListener('click', e => {
    e.stopPropagation();
    const dd = btn.parentElement, was = dd.classList.contains('open');
    document.querySelectorAll('.dd.open').forEach(d => d.classList.remove('open'));
    if (!was) dd.classList.add('open');
  }));
  document.addEventListener('click', e => { if (!e.target.closest('.dd')) document.querySelectorAll('.dd.open').forEach(d => d.classList.remove('open')); });

  // Uzoq davom etadigan formalar (AI, sinxron): yuborilganda tugma bloklanadi va holat ko'rsatiladi
  document.querySelectorAll('form[data-busy]').forEach(f => f.addEventListener('submit', () => {
    const msg = f.dataset.busy || 'Kuting...';
    f.querySelectorAll('button').forEach(b => { if (!b.dataset.keep) { b.dataset.t = b.innerHTML; b.textContent = msg; setTimeout(() => { b.disabled = true; }, 0); } });
  }));
  // Nusxa olish
  document.querySelectorAll('[data-copy],[data-copy-text]').forEach(b => b.addEventListener('click', () => {
    let t = b.dataset.copyText;
    if (t === undefined) {
      const el = document.getElementById((b.dataset.copy || '').replace(/^#/, ''));
      if (!el) return;
      t = el.innerText;
    }
    (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).catch(() => {
      const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
    }).finally(() => { const o = b.textContent; b.textContent = 'Nusxalandi'; setTimeout(() => b.textContent = o, 1500); });
  }));

  // Fayl tashlash zonalari
  document.querySelectorAll('[data-drop]').forEach(zone => {
    const input = zone.querySelector('input[type=file]');
    const nameEl = zone.querySelector('[data-name]');
    const show = () => {
      if (input.files.length && nameEl) {
        const f = input.files[0];
        nameEl.textContent = f.name + ' · ' + (f.size / 1048576).toFixed(1) + ' MB';
      }
    };
    input.addEventListener('change', show);
    ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove('over'); }));
    zone.addEventListener('drop', e => {
      if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; input.dispatchEvent(new Event('change')); }
    });
  });

  // Guruh tanlash (compose va groups sahifalari)
  const box = document.getElementById('glist');
  if (box) {
    const rows = [...box.querySelectorAll('.grow')];
    const cbs = () => rows.map(r => r.querySelector('input[type=checkbox]'));
    const counter = document.getElementById('gcount');
    const upd = () => {
      const n = cbs().filter(c => c.checked).length;
      if (counter) counter.textContent = n;
      document.dispatchEvent(new CustomEvent('gcount', { detail: n }));
    };
    const search = document.getElementById('gsearch');
    if (search) search.addEventListener('input', () => {
      const v = search.value.trim().toLowerCase();
      rows.forEach(r => r.classList.toggle('hide', !(r.dataset.title + ' ' + (r.dataset.tags || '')).includes(v)));
    });
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
    on('selAll', () => { rows.forEach(r => { const c = r.querySelector('input'); if (!r.classList.contains('hide') && !c.disabled) c.checked = true; }); upd(); });
    on('selNone', () => { cbs().forEach(c => c.checked = false); upd(); });
    document.querySelectorAll('.chip[data-ids]').forEach(ch => ch.onclick = () => {
      const ids = new Set(JSON.parse(ch.dataset.ids).map(String));
      cbs().forEach(c => { if (!c.disabled) c.checked = ids.has(c.value); });
      upd();
    });
    box.addEventListener('change', upd);
    upd();
  }
})();

// ===== Yordamchi funksiyalar (sahifalar ishlatadi) =====
window.esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
window.fmtHtml = (text, mode) => {
  let e = esc(text);
  if (mode === 'html') {
    ['b', 'strong', 'i', 'em', 'u', 's', 'code', 'pre'].forEach(t => { e = e.replace(new RegExp('&lt;(/?)' + t + '&gt;', 'g'), '<$1' + t + '>'); });
    e = e.replace(/&lt;a href=&quot;(https?:\/\/[^"<>\s]+?)&quot;&gt;/g, '<a href="$1" target="_blank" rel="noopener">').replace(/&lt;\/a&gt;/g, '</a>');
  }
  return e.replace(/\n/g, '<br>');
};
window.fmtDur = sec => {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + ' soat ' : '') + (h || m ? m + ' daqiqa ' : '') + (!h ? s + ' soniya' : '');
};
window.parseDt = s => new Date(String(s).replace(' ', 'T'));
