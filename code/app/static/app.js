// Guruh tanlash yordamchisi (compose va groups sahifalari uchun)
(function(){
  const box=document.getElementById('glist'); if(!box) return;
  const rows=[...box.querySelectorAll('.grow')];
  const cbs=()=>rows.map(r=>r.querySelector('input[type=checkbox]'));
  const counter=document.getElementById('gcount');
  const upd=()=>{ if(counter) counter.textContent=cbs().filter(c=>c.checked).length; };
  const visible=r=>r.style.display!=='none';
  const search=document.getElementById('gsearch');
  if(search) search.addEventListener('input',()=>{
    const v=search.value.trim().toLowerCase();
    rows.forEach(r=>r.style.display=r.dataset.title.includes(v)?'':'none');
  });
  const all=document.getElementById('selAll');
  if(all) all.onclick=()=>rows.forEach(r=>{const c=r.querySelector('input'); if(visible(r)&&!c.disabled) c.checked=true; upd();});
  const none=document.getElementById('selNone');
  if(none) none.onclick=()=>{cbs().forEach(c=>c.checked=false); upd();};
  document.querySelectorAll('.chip[data-ids]').forEach(ch=>ch.onclick=()=>{
    const ids=new Set(JSON.parse(ch.dataset.ids).map(String));
    cbs().forEach(c=>{ if(!c.disabled) c.checked=ids.has(c.value); }); upd();
  });
  box.addEventListener('change',upd); upd();
})();
