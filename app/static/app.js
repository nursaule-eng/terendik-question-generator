
'use strict';
const LEVELS = [
  {n:1, name:'Факт', desc:'Вспомнить определение или факт', color:'var(--l1)'},
  {n:2, name:'Понимание', desc:'Объяснить своими словами, сравнить', color:'var(--l2)'},
  {n:3, name:'Применение', desc:'Применить знание к задаче', color:'var(--l3)'},
  {n:4, name:'Анализ', desc:'Разобрать, найти ошибку', color:'var(--l4)'},
  {n:5, name:'Синтез', desc:'Предложить и обосновать решение', color:'var(--l5)'},
];
const LBL = {ready:'Готов',processing:'Обработка',error:'Ошибка',pending:'Ожидает проверки',accepted:'Принят',rejected:'Отклонён',passed:'AI-проверка пройдена',failed:'AI-проверка не пройдена',stale:'Нужна повторная проверка',queued:'В очереди',running:'Выполняется',waiting:'Ожидание лимита',paused:'Пауза',cancelled:'Остановлено',done:'Завершено',active:'Не завершена',finished:'Завершена'};
const LANG = {kk:'Қазақша',ru:'Русский',en:'English'};
const S = {doc:null, detail:null, view:'material', jobs:[], trackJob:null, attempt:null, editing:null, polling:false, capacity:null, assignments:[], gradingTimer:null};
const ACTIVE_JOB = ['queued','running','waiting','paused'];
const $ = id => document.getElementById(id);

/* ---------- helpers ---------- */
function h(tag, attrs, ...kids){
  const n = document.createElement(tag);
  if (attrs) for (const [k,v] of Object.entries(attrs)){
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'style') n.style.cssText = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k in n && typeof v !== 'string') n[k] = v;
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
function toast(msg, error=false){
  const t = h('div', {class:'toast'+(error?' error':'')}, h('span', {text:msg}));
  $('toasts').append(t); while ($('toasts').children.length > 3) $('toasts').firstChild.remove(); setTimeout(()=>t.remove(), error ? 9000 : 5000);
}
function confirmAction(message){
  return new Promise(resolve=>{
    const dialog=h('dialog',{class:'confirm-dialog'});
    const finish=value=>{dialog.close();dialog.remove();resolve(value);};
    dialog.append(h('form',{method:'dialog'},h('h2',null,'Подтвердите действие'),h('p',null,message),
      h('div',{class:'actions'},h('button',{type:'button',class:'btn ghost',onclick:()=>finish(false)},'Отмена'),
        h('button',{type:'button',class:'btn primary',onclick:()=>finish(true)},'Подтвердить'))));
    dialog.addEventListener('cancel',e=>{e.preventDefault();finish(false);});
    document.body.append(dialog);dialog.showModal();
  });
}
async function api(path, opt={}){
  if (opt.body && !(opt.body instanceof FormData)){ opt.body = JSON.stringify(opt.body); opt.headers = {'Content-Type':'application/json'}; }
  const r = await fetch(path, opt);
  let data = null; try { data = await r.json(); } catch {}
  if (!r.ok) throw Error((data && (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))) || 'Сервер вернул ошибку '+r.status);
  return data;
}
async function busy(btn, fn){
  const old = btn ? [...btn.childNodes] : null;
  if (btn){ btn.disabled = true; btn.prepend(h('span',{class:'spin'})); }
  try { await fn(); } catch(e){ toast(e.message, true); }
  finally { if (btn && btn.isConnected){ btn.replaceChildren(...old); btn.disabled = btn.id==='gen-button' && S.jobs.some(j=>ACTIVE_JOB.includes(j.status)); } }
}
const btn = (text, cls, fn, extra={}) => { const b = h('button', {type:'button', class:'btn '+(cls||''), ...extra}, text); b.addEventListener('click', ()=>busy(b, fn)); return b; };
const levelOf = n => LEVELS[(n||1)-1];
function meter(n){ const m = h('span',{class:'meter','aria-hidden':'true'}); for (let i=1;i<=5;i++) m.append(h('i', i<=n ? {style:'background:'+levelOf(n).color} : null)); return m; }
function bars(n){ const b = h('div',{class:'bars','aria-hidden':'true'}); for (let i=1;i<=5;i++) b.append(h('i', i<=n ? {class:'on'+n} : null)); return b; }
const fmtDate = s => { if(!s) return ''; const d = new Date(s.replace(' ','T')+'Z'); return isNaN(d) ? s : d.toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}); };
function seg(container, name, items, value){
  container.replaceChildren(...items.map(([v,t]) => h('label',null, h('input',{type:'radio',name, value:v, checked: v===value}), h('span',{text:t}))));
}
const segVal = name => (document.querySelector(`input[name="${name}"]:checked`)||{}).value;

/* ---------- static pieces ---------- */
function buildStatic(){
  $('ladder').replaceChildren(...LEVELS.map(l => h('div',{class:'rung'}, h('b',null, l.n+'. '+l.name), h('div',null, h('i',{style:`width:${20*l.n}%;background:${l.color}`}), h('span',{text:l.desc})))));
  const depth = $('depth');
  depth.append(h('label',{class:'lvl mix'}, h('input',{type:'radio',name:'gen-level',value:'',checked:true}), h('div',null, h('div',{class:'t',text:'Смесь 1–5'}), h('div',{class:'d',text:'Равномерно по всем уровням'})), bars(5)));
  for (const l of LEVELS) depth.append(h('label',{class:'lvl'}, h('input',{type:'radio',name:'gen-level',value:String(l.n)}), h('div',null, h('div',{class:'t',text:l.n+'. '+l.name}), h('div',{class:'d',text:l.desc})), bars(l.n)));
  const types = [['mixed','Смешанные'],['mcq','Тестовые'],['open','Открытые']];
  seg($('gen-type'),'gen-type',types,'mixed'); seg($('test-type'),'test-type',types,'mixed'); seg($('f-type'),'f-type',types,'mixed'); seg($('as-type'),'as-type',types,'mixed');
  $('gen-topic').addEventListener('change', ()=>loadCapacity().catch(()=>{})); $('gen-count').addEventListener('input', renderCapacity);
  document.querySelectorAll('input[name="f-type"]').forEach(i => i.addEventListener('change', ()=>loadQuestions().catch(e=>toast(e.message,true))));
  ['f-status','f-auto','f-topic','f-level'].forEach(id => $(id).addEventListener('change', ()=>loadQuestions().catch(e=>toast(e.message,true))));
  document.querySelectorAll('.step').forEach(b => b.addEventListener('click', ()=>show(b.dataset.view)));
}

/* ---------- model status ---------- */
async function loadConfig(){
  const c = await api('/api/config');
  const box = $('model');
  box.replaceChildren(...[
    h('div',{class:'model-line'}, h('span',{class:'dot '+(c.configured?'ready':'error')}), c.configured ? 'Модель настроена' : 'Модель не настроена'),
    c.configured ? null : h('p',{class:'hint',text:'Заполните '+c.missing.join(', ')+' в .env и перезапустите сервер.'}),
    c.configured ? h('button',{class:'link-btn',type:'button',onclick:async e=>{ const b=e.currentTarget; b.textContent='Проверяю соединение…'; try{ const r=await api('/api/model/check',{method:'POST'}); toast(r.message||'Соединение работает'); } catch(err){ toast(err.message,true);} finally{ b.textContent='Проверить соединение'; } }},'Проверить соединение') : null
  ].filter(Boolean));
}

async function loadStorage(){
  const st = await api('/api/storage');
  const box = $('storage');
  const restoreInput = h('input',{type:'file',accept:'.json,application/json',hidden:true,onchange:async e=>{
    const f = e.target.files[0]; if (!f) return;
    if (!await confirmAction('Восстановить данные из резервной копии? Уже существующие документы будут пропущены, новые — добавлены.')) { e.target.value=''; return; }
    try{ const fd = new FormData(); fd.append('file', f); const r = await api('/api/backup',{method:'POST',body:fd}); toast(r.message); await loadDocuments(); }
    catch(err){ toast(err.message,true); } finally{ e.target.value=''; } }});
  box.replaceChildren(...[
    st.persistent ? h('div',{class:'storage-ok',text: st.backend==='postgres' ? '● Данные хранятся в базе PostgreSQL и не пропадут при перезапуске.' : '● Данные на постоянном диске.'})
      : h('div',{class:'storage-warn',text: (st.notice || 'Данные хранятся на временном диске сервера.') + ' Регулярно скачивайте резервную копию.'}),
    h('a',{class:'link-btn',href:'/api/backup',download:''},'Скачать резервную копию'),
    h('button',{class:'link-btn',type:'button',onclick:()=>restoreInput.click()},'Восстановить из копии'), restoreInput,
    location.hostname!=='localhost' && location.hostname!=='127.0.0.1' ? h('a',{class:'link-btn',href:'/logout'},'Выйти') : null
  ].filter(Boolean));
}

/* ---------- documents ---------- */
async function loadDocuments(){
  const trash = $('show-trash').checked;
  const docs = await api('/api/documents?archived='+trash);
  $('shelf-title').textContent = trash ? 'Корзина' : 'Материалы';
  const list = $('docs');
  if (!docs.length){ list.replaceChildren(h('p',{class:'empty-shelf',text: trash ? 'В корзине пусто.' : 'Пока нет материалов.'})); return; }
  list.replaceChildren(...docs.map(d => {
    const ext = (d.format || d.name.split('.').pop() || '').slice(0,4);
    const state = d.status === 'ready' && d.topic_status === 'processing' ? 'processing' : d.status;
    const stateText = trash ? 'Нажмите, чтобы восстановить' : (d.status==='ready' && d.topic_status==='processing' ? 'Ищу темы…' : LBL[d.status]||d.status);
    return h('button',{class:'doc'+(d.id===S.doc && !trash?' active':''),type:'button',title:d.name, onclick: async ()=>{
        try{
          if (trash){ const r = await api('/api/documents/'+d.id+'/restore',{method:'POST'}); toast(r.message||'Документ восстановлен'); await loadDocuments(); }
          else await selectDocument(d.id);
        } catch(e){ toast(e.message,true); }
      }},
      h('span',{class:'doc-ico',text:ext}),
      h('span',null, h('span',{class:'doc-name',text:d.name}), h('span',{class:'doc-state'}, h('span',{class:'dot '+state}), stateText)));
  }));
}
async function upload(file){
  if (!file) return;
  if (file.size > 20*1024*1024) return toast('Файл больше 20 МБ. Сожмите PDF или разделите его на части.', true);
  if (!/\.(pdf|txt|docx)$/i.test(file.name)) return toast('Поддерживаются PDF с текстовым слоем, DOCX и TXT. Файлы .doc сохраните как .docx.', true);
  const drop = $('drop'); drop.classList.add('busy');
  try{
    const fd = new FormData(); fd.append('file', file);
    const d = await api('/api/documents',{method:'POST',body:fd});
    toast(d.message || 'Файл загружен. Текст и темы обрабатываются в фоне.');
    $('show-trash').checked = false;
    await loadDocuments(); await selectDocument(d.id);
  } catch(e){ toast(e.message,true); }
  finally{ drop.classList.remove('busy'); $('file').value=''; }
}
async function selectDocument(id){
  S.doc = id; S.detail = null; for(const s of document.querySelectorAll('.topic-select'))s.replaceChildren(); S.jobs = []; S.trackJob = null; S.attempt = null; S.capacity = null;
  $('attempt').replaceChildren(); $('result-view').replaceChildren(); $('gen-language').value = 'auto';
  await loadDetail();
  $('welcome').hidden = true; $('workspace').hidden = false;
  show(S.view);
  await loadDocuments();
  updateExportLinks();
  await Promise.all([loadJobs(), loadQuestions(), loadAttempts(), loadCapacity()]);
}
function clearDocument(){
  S.doc = null; S.detail = null;
  $('workspace').hidden = true; $('welcome').hidden = false;
}
async function loadDetail(){
  if (!S.doc) return;
  const d = await api('/api/documents/'+S.doc); S.detail = d;
  $('doc-title').textContent = d.name.replace(/\.(pdf|txt|docx)$/i,'');
  const fact = (b, t) => h('span',{class:'fact'}, h('b',{text:b}), ' '+t);
  $('doc-facts').replaceChildren(
    h('span',{class:'chip '+(d.status==='ready'?'ok':d.status==='error'?'bad':'warn')}, LBL[d.status]||d.status),
    fact(d.topics.length, plural(d.topics.length,'тема','темы','тем')),
    fact(d.fragment_count ?? 0, plural(d.fragment_count||0,'фрагмент','фрагмента','фрагментов')),
    fact((d.text_length||0).toLocaleString('ru-RU'), 'символов'),
    d.language ? fact(LANG[d.language]||d.language, '') : null,
    fact((d.format||'').toUpperCase(), 'загружен '+fmtDate(d.created_at))
  );
  // errors
  const errs = $('doc-errors'); errs.replaceChildren();
  if (d.error) errs.append(h('p',{class:'note bad',text:d.error}));
  if (d.warning) errs.append(h('p',{class:'note',text:d.warning}));
  if (d.topic_error) errs.append(h('p',{class:'note',text:d.topic_error}));
  if (d.text_length && d.text_length < 20000) errs.append(h('p',{class:'note',text:'Материал короткий ('+d.text_length.toLocaleString('ru-RU')+' символов). Вопросов 4–5 уровня может не хватить: для демонстрации лучше взять полноценную главу или учебник.'}));
  if (errs.children.length) errs.style.marginBottom = '16px';
  $('topic-lead').textContent = d.topic_status === 'processing' ? 'Модель читает документ порциями и выделяет темы…' : d.topics.length ? 'Нажмите на тему, чтобы сразу создать по ней вопросы.' : 'Темы ещё не найдены. Генерация по всему материалу доступна и без них.';
  $('topics').replaceChildren(...(d.topic_status === 'processing' && !d.topics.length ? [h('div',{class:'progress',style:'grid-column:1/-1'},h('i'))] :
    d.topics.map(t => h('button',{class:'topic',type:'button',onclick:()=>{ $('gen-topic').value=String(t.id); show('generate'); }},
      h('h3',{text:t.name}), t.keywords && t.keywords.length ? h('p',{text:t.keywords.join(', ')}) : null, h('span',{text:'Создать вопросы по теме'})))));
  $('doc-actions').replaceChildren(
    btn('Повторить анализ тем','', async()=>{ await api('/api/documents/'+d.id+'/topics',{method:'POST'}); toast('Поиск тем запущен.'); await loadDetail(); }, {disabled: d.status!=='ready' || d.topic_status==='processing'}),
    btn('В корзину','danger', async()=>{ if(!await confirmAction('Переместить документ, его вопросы и ссылки для студентов в корзину? Всё можно восстановить, результаты сохранятся.')) return; const r = await api('/api/documents/'+d.id,{method:'DELETE'}); toast(r.message||'Документ в корзине'); clearDocument(); await loadDocuments(); })
  );
  for (const s of document.querySelectorAll('.topic-select')){
    const old = s.value;
    s.replaceChildren(h('option',{value:''},'Весь материал'), ...d.topics.map(t => h('option',{value:String(t.id)}, t.name)));
    if ([...s.options].some(o=>o.value===old)) s.value = old;
  }
}
function plural(n, one, few, many){ n = Math.abs(n)%100; const n1 = n%10; if (n>10&&n<20) return many; if (n1>1&&n1<5) return few; if (n1===1) return one; return many; }

/* ---------- views ---------- */
function show(v){
  S.view = v;
  document.querySelectorAll('.step').forEach(b => { const on = b.dataset.view===v; b.classList.toggle('active',on); b.setAttribute('aria-selected',on); });
  document.querySelectorAll('.view').forEach(s => s.classList.toggle('active', s.id==='view-'+v));
  if (v==='bank') loadQuestions().catch(e=>toast(e.message,true));
  if (v==='test') loadAttempts().catch(e=>toast(e.message,true));
  if (v==='students') loadStudents().catch(e=>toast(e.message,true));
  if (v==='generate') loadCapacity().catch(()=>{});
}

/* ---------- generation ---------- */
function params(prefix){
  if (!S.doc) throw Error('Сначала выберите материал слева.');
  const lv = prefix==='gen' ? segVal('gen-level') : $('test-level').value;
  return { document_id:S.doc, topic_id: $(prefix+'-topic').value ? Number($(prefix+'-topic').value) : null,
    count: Number($(prefix+'-count').value), level: lv ? Number(lv) : null, type: segVal(prefix+'-type') || 'mixed',
    language: prefix==='gen' ? $('gen-language').value : 'auto' };
}
async function loadCapacity(){
  if (!S.doc) return;
  const topic = $('gen-topic').value;
  S.capacity = await api('/api/documents/'+S.doc+'/capacity'+(topic ? '?topic_id='+topic : ''));
  renderCapacity();
}
function renderCapacity(){
  const box = $('gen-capacity'), c = S.capacity, n = Number($('gen-count').value) || 0;
  if (!c){ box.hidden = true; return; }
  const scope = $('gen-topic').value ? 'по этой теме' : 'по материалу';
  if (n > c.remaining){
    box.hidden = false; box.className = 'note';
    box.textContent = c.remaining <= 0
      ? `Материала ${scope} уже почти исчерпан: в банке ${c.existing} прошедших проверку вопросов при оценке ≈${c.estimate}. Новые вопросы будут чаще отклоняться как повторы — выберите другую тему или загрузите больше материала.`
      : `Материала ${scope} хватит примерно на ${c.remaining} новых неповторяющихся вопросов (≈${c.estimate} всего, уже есть ${c.existing}). Можно запустить ${n}, но часть может не пройти проверку.`;
  } else box.hidden = true;
}
$('gen-form').addEventListener('submit', e => { e.preventDefault(); busy($('gen-button'), async ()=>{
  const p = params('gen');
  if (!p.count || p.count < 1 || p.count > 50) throw Error('Количество вопросов — от 1 до 50 за один запуск.');
  if (S.capacity && p.count > S.capacity.remaining && !await confirmAction(S.capacity.remaining <= 0
      ? 'Материала по выбранной теме, вероятно, уже не хватит на новые неповторяющиеся вопросы. Всё равно запустить?'
      : `Материала хватит примерно на ${S.capacity.remaining} новых вопросов, а запрошено ${p.count}. Запустить всё равно?`)) return;
  if (p.level === 5 && p.type === 'mcq') toast('Для 5 уровня лучше открытые вопросы: синтез решения сложно проверить выбором из 4 вариантов.');
  const job = await api('/api/generate',{method:'POST',body:p}); S.trackJob = job.id;
  toast('Генерация запущена. Можно закрыть вкладку — работа продолжится на сервере.'); await loadJobs();
});});
function stagesOf(j){
  const st = j.stage || j.message || '';
  const reading = j.status==='queued' || /Подготовка|читаем/i.test(st);
  const creating = /Создаётся|Уточняется/.test(st), checking = /Проверяется/.test(st);
  const cls = on => on ? 'on' : '';
  return h('div',{class:'stages'},
    h('span',{class: reading ? 'on' : 'done'},'1 · Читаем материал'),
    h('span',{class: creating ? 'on' : (checking ? 'done' : (reading ? '' : 'done'))},'2 · Создаём вопрос'),
    h('span',{class: cls(checking)},'3 · Проверяем'),
    h('span',{class:''}, `${j.created||0} из ${j.requested} готово`));
}
function countdown(resumeAt){
  if (!resumeAt) return '';
  const left = Math.max(0, Math.round((new Date(resumeAt.replace(' ','T')+'Z') - Date.now())/1000));
  if (left > 3600) return 'примерно в ' + new Date(resumeAt.replace(' ','T')+'Z').toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
  return left > 60 ? `через ${Math.ceil(left/60)} мин.` : `через ${left} сек.`;
}
async function loadJobs(){
  if (!S.doc) return;
  const id = S.doc; const jobs = await api('/api/jobs?document_id='+id); if (id!==S.doc) return;
  const before = S.jobs;
  S.jobs = jobs;
  const active = jobs.some(j => ACTIVE_JOB.includes(j.status));
  $('gen-button').disabled = active;
  const tracked = jobs.find(j => j.id===S.trackJob);
  if (tracked && !ACTIVE_JOB.includes(tracked.status)){
    toast(tracked.status==='done' ? `Готово: ${tracked.created} новых вопросов прошли проверку.` : (tracked.message||'Генерация завершилась'), tracked.status==='error');
    S.trackJob = null; loadQuestions().catch(()=>{}); loadCapacity().catch(()=>{});
  } else if (before.some(b => { const now = jobs.find(j=>j.id===b.id); return now && now.created !== b.created; })) {
    loadQuestions().catch(()=>{});
  }
  const box = $('jobs');
  if (!jobs.length){ box.replaceChildren(h('p',{class:'empty',text:'Запусков пока не было. Выберите уровень и нажмите «Создать вопросы».'})); return; }
  const card = j => {
    const running = ['queued','running','waiting'].includes(j.status);
    const pct = Math.min(100, Math.round(100*(j.created||0)/Math.max(1,j.requested)));
    const chipCls = running ? 'sky' : j.status==='done' ? 'ok' : j.status==='paused' ? 'warn' : j.status==='cancelled' ? '' : 'bad';
    const p = j.params || {};
    const descr = [p.level ? 'уровень '+p.level : 'уровни 1–5', p.type==='mcq' ? 'тестовые' : p.type==='open' ? 'открытые' : 'смешанные', p.language && p.language!=='auto' ? (LANG[p.language]||p.language) : null].filter(Boolean).join(' · ');
    return h('div',{class:'job'+(j.status==='error'?' err':'')},
      h('div',{class:'job-top'}, h('span',{class:'chip '+chipCls}, LBL[j.status]||j.status), h('span',{text:fmtDate(j.created_at)}), descr ? h('span',{text:descr}) : null),
      running || j.status==='paused' ? stagesOf(j) : null,
      running || j.status==='paused' ? h('div',{class:'meterbar'}, h('i',{style:'width:'+pct+'%'})) : null,
      j.status==='waiting' ? h('p',{class:'note',text:'Модель ограничила частоту запросов. Генерация продолжится автоматически '+countdown(j.resume_at)+' Готовые вопросы уже в банке.'}) : null,
      j.status==='paused' ? h('p',{class:'note',text:(j.message||'')+(j.resume_at ? ' Лимит обычно восстанавливается '+countdown(j.resume_at)+'.' : '')}) : null,
      running && j.stage && j.status!=='waiting' ? h('p',{class:'hint',text:j.stage}) : null,
      j.status==='error' || j.status==='cancelled' ? h('p',{text:j.message||'Ошибка генерации'}) : null,
      !running && j.status!=='paused' ? h('div',{class:'funnel'},
          h('span',null,h('b',{text:j.requested}),'запрошено'),
          h('span',null,h('b',{text:(j.created||0)+(j.rejected||0)}),'проверено'),
          h('span',{style:'color:var(--ok)'},h('b',{text:j.created||0}),'прошли проверку'),
          h('span',{style:'color:var(--danger)'},h('b',{text:j.rejected||0}),'отклонено')) : null,
      j.status==='done' && j.created < j.requested ? h('p',{class:'note',text:'Получено меньше запроса: материала не хватило на новые неповторяющиеся вопросы или часть не прошла проверку. Причины — в комментариях ниже.'}) : null,
      h('div',{class:'actions'},
        running ? btn('Остановить','danger', async()=>{ const r = await api('/api/jobs/'+j.id+'/cancel',{method:'POST'}); toast(r.message); await loadJobs(); }) : null,
        j.status==='paused' ? btn('Продолжить','gold', async()=>{ const r = await api('/api/jobs/'+j.id+'/resume',{method:'POST'}); S.trackJob=j.id; toast(r.message); await loadJobs(); }) : null,
        j.status==='paused' ? btn('Завершить','ghost', async()=>{ const r = await api('/api/jobs/'+j.id+'/cancel',{method:'POST'}); toast(r.message); await loadJobs(); }) : null,
        j.status==='error' && p.document_id ? btn('Продолжить с места ошибки','', async()=>{ const r = await api('/api/jobs/'+j.id+'/resume',{method:'POST'}); S.trackJob=j.id; toast(r.message); await loadJobs(); }) : null,
        (j.created||0) > 0 && !running ? btn('Открыть в банке','ghost', async()=>{ show('bank'); }) : null),
      j.details && j.details.length ? h('details',{class:'more'}, h('summary',null,'Комментарии модели и проверки ('+j.details.length+')'), ...j.details.map(t=>h('p',{text:t}))) : null);
  };
  const [first, ...rest] = jobs;
  box.replaceChildren(...[card(first), rest.length ? h('details',{class:'more'}, h('summary',null,'Предыдущие запуски ('+rest.length+')'), h('div',{class:'grid',style:'margin-top:10px'}, ...rest.map(card))) : null].filter(Boolean));
}

/* ---------- bank ---------- */
function updateExportLinks(){
  if (!S.doc) return;
  const base = '/api/documents/'+S.doc+'/export?scope=accepted&format=';
  $('exp-docx').href = base+'docx'; $('exp-docx-students').href = base+'docx&answers=false';
  $('exp-csv').href = base+'csv'; $('exp-json').href = base+'json';
  for (const id of ['exp-docx','exp-docx-students','exp-csv','exp-json']) $(id).onclick = async e => {
    // Show a clear message instead of downloading an error page when nothing is accepted yet.
    const st = await api('/api/documents/'+S.doc+'/bank-status').catch(()=>null);
    if (st && !st.accepted_for_test){ e.preventDefault(); toast('Сначала примите вопросы в банке: выгружаются только принятые преподавателем.', true); }
  };
}
async function loadQuestions(){
  if (!S.doc) return;
  const qs = new URLSearchParams({document_id:S.doc});
  if ($('f-status').value) qs.set('status',$('f-status').value);
  if ($('f-auto').value) qs.set('auto_status',$('f-auto').value);
  if ($('f-level').value) qs.set('level',$('f-level').value);
  const ft = segVal('f-type'); if (ft && ft!=='mixed') qs.set('type',ft);
  const topic = S.detail && S.detail.topics.find(t => String(t.id)===$('f-topic').value); if (topic) qs.set('topic',topic.name);
  const [list, all, st] = await Promise.all([api('/api/questions?'+qs), api('/api/questions?document_id='+S.doc), api('/api/documents/'+S.doc+'/bank-status')]);
  renderBankStats(all, st);
  $('q-count').textContent = list.length ? 'Показано '+list.length+' из '+all.length : '';
  $('questions').replaceChildren(...(list.length ? list.map(questionCard) : [h('p',{class:'empty',text: all.length ? 'Под эти фильтры вопросов нет. Сбросьте один из фильтров.' : 'Банк пуст. Перейдите в «Генерацию», чтобы создать первые вопросы.'})]));
}
function renderBankStats(all, st){
  const accepted = all.filter(q=>q.review_status==='accepted').length;
  const stat = (n,t) => h('div',{class:'stat'}, h('b',{text:n}), h('span',{text:t}));
  $('bank-stats').replaceChildren(stat(all.length,'вопросов в банке'), stat(st.accepted_for_test ?? accepted,'принято в тест'), stat(st.auto_passed ?? '—','прошли AI-проверку'), stat(st.unchecked_level ?? 0,'без проверки уровня'));
  const counts = LEVELS.map(l => all.filter(q => q.checked_level===l.n).length);
  const max = Math.max(1, ...counts);
  $('coverage').replaceChildren(...LEVELS.map((l,i) => h('div',{class:'cov'},
    h('b',{text:counts[i]}),
    h('div',{class:'col',style:`height:${Math.max(4, 70*counts[i]/max)}px;background:${counts[i]?l.color:'#E3EAF0'}`}),
    h('small',{text:l.n+'. '+l.name}))));
}
function questionCard(q){
  const lv = q.checked_level || q.requested_level;
  const L = levelOf(lv);
  const review = q.review_status, auto = q.auto_status;
  const head = h('div',{class:'chips'},
    h('span',{class:'chip'}, q.checked_level ? meter(lv) : null, q.checked_level ? 'Уровень '+lv+' · '+L.name : 'Сложность не проверена'),
    h('span',{class:'chip',text: q.type==='mcq' ? 'Тестовый' : 'Открытый'}),
    q.topic ? h('span',{class:'chip',text:q.topic}) : null,
    h('span',{class:'chip '+(review==='accepted'?'gold':review==='rejected'?'bad':''), text: LBL[review]||review}),
    h('span',{class:'chip '+(auto==='passed'?'ok':auto==='failed'?'bad':'warn'), text: LBL[auto]||auto}));
  const notes = [];
  if (q.possible_duplicate) notes.push(h('p',{class:'note',text:'Похож на «'+q.possible_duplicate+'». Сравните перед тем, как принять.'}));
  if (!q.checked_level) notes.push(h('p',{class:'note',text:'Уровень сложности не проверен. Запустите повторную AI-проверку, иначе вопрос не попадёт в тест.'}));
  else if (q.level_mismatch) notes.push(h('p',{class:'note',text:'Запрашивался уровень '+q.requested_level+', проверка определила '+q.checked_level+'.'}));
  const letters = 'ABCD';
  const opts = q.options && q.options.length ? h('ol',{class:'opts'}, ...q.options.map((o,i)=>h('li',{class:i===q.correct_index?'right':''}, h('span',{class:'k',text:letters[i]||i+1}), h('span',{text:o})))) : null;
  const reasons = (q.validation && q.validation.reasons) || [];
  const answer = h('details',{class:'reveal'}, h('summary',null, q.type==='mcq' ? 'Объяснение и источник' : 'Эталонный ответ и источник'),
    h('div',{class:'answer'},
      h('dl',null,
        h('div',null,h('dt',{text:'Эталонный ответ'}),h('dd',{text:q.reference_answer})),
        q.explanation ? h('div',null,h('dt',{text:'Объяснение'}),h('dd',{text:q.explanation})) : null,
        h('div',null,h('dt',{text:'Почему такой уровень'}),h('dd',{text:q.difficulty_reason||'Уровень ещё не проверен'}))),
      reasons.length ? h('div',null, h('dt',{text:'AI-проверка',style:'font-size:12.5px;color:var(--muted);font-weight:600;margin-bottom:4px'}), h('ul',{class:'checks'+(auto==='passed'?'':' warn')}, ...reasons.map(r=>h('li',{text:r})))) : null,
      ...(q.sources||[]).map(s => h('blockquote',{class:'src'}, h('cite',{text:s.location||'Источник требует проверки'}), '«'+s.quote+'»'))));
  const canAccept = auto==='passed' && q.checked_level;
  const actions = h('div',{class:'actions'},
    review!=='accepted' ? btn('Принять','gold', async()=>{ await api('/api/questions/'+q.id+'/review',{method:'POST',body:{status:'accepted',revision:q.revision}}); toast('Вопрос принят в тест.'); await loadQuestions(); }, {disabled:!canAccept, title: canAccept ? null : 'Сначала вопрос должен пройти AI-проверку'}) :
      btn('Вернуть на проверку','', async()=>{ await api('/api/questions/'+q.id+'/review',{method:'POST',body:{status:'pending',revision:q.revision}}); toast('Вопрос возвращён на проверку.'); await loadQuestions(); }),
    btn('Редактировать','', async()=>openEditor(q)),
    btn('Проверить ещё раз','ghost', async()=>{ toast('AI проверяет вопрос…'); const r = await api('/api/questions/'+q.id+'/recheck',{method:'POST'}); toast(r.message||'Проверка завершена'); await loadQuestions(); }),
    review!=='rejected' ? btn('Отклонить','ghost', async()=>{ await api('/api/questions/'+q.id+'/review',{method:'POST',body:{status:'rejected',revision:q.revision}}); toast('Вопрос отклонён.'); await loadQuestions(); }) : null,
    btn('Удалить','danger push', async()=>{ if(!await confirmAction('Удалить вопрос из банка? Сохранённые попытки останутся.')) return; await api('/api/questions/'+q.id,{method:'DELETE'}); toast('Вопрос удалён.'); await loadQuestions(); }));
  return h('article',{class:'q',style:'--qc:'+L.color}, head, h('h3',{text:q.text}), opts, ...notes, answer, actions);
}

/* ---------- editor ---------- */
function openEditor(q){
  S.editing = q;
  $('e-text').value = q.text; $('e-type').value = q.type; $('e-level').value = String(q.requested_level);
  $('e-language').value = q.language || (S.detail && S.detail.language) || 'ru';
  $('e-options').value = (q.options||[]).join('\n'); $('e-correct').value = q.correct_index==null ? '' : String(q.correct_index+1);
  $('e-answer').value = q.reference_answer; $('e-explanation').value = q.explanation; $('e-topic').value = q.topic;
  $('e-sources').value = JSON.stringify((q.sources||[]).map(s=>({fragment_id:s.fragment_id, quote:s.quote})), null, 2);
  toggleMcq(); $('edit-dialog').showModal();
}
function toggleMcq(){ $('e-mcq').hidden = $('e-type').value !== 'mcq'; }
$('e-type').addEventListener('change', toggleMcq);
$('e-cancel').addEventListener('click', ()=>$('edit-dialog').close());
$('edit-form').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async ()=>{
  let sources; try { sources = JSON.parse($('e-sources').value); } catch { throw Error('В поле «Источники» некорректный JSON. Проверьте кавычки и запятые.'); }
  const mcq = $('e-type').value === 'mcq';
  const data = { text:$('e-text').value, type:$('e-type').value,
    options: mcq ? $('e-options').value.split('\n').map(v=>v.trim()).filter(Boolean) : [],
    correct_index: mcq && $('e-correct').value ? Number($('e-correct').value)-1 : null,
    reference_answer:$('e-answer').value, explanation:$('e-explanation').value, topic:$('e-topic').value,
    requested_level:Number($('e-level').value), language:$('e-language').value, sources, revision:S.editing.revision };
  const r = await api('/api/questions/'+S.editing.id,{method:'PUT',body:data});
  $('edit-dialog').close(); toast(r.message||'Изменения сохранены.'); await loadQuestions();
});});

/* ---------- test ---------- */
async function loadAttempts(){
  if (!S.doc) return;
  const [st, list] = await Promise.all([api('/api/documents/'+S.doc+'/bank-status'), api('/api/attempts?document_id='+S.doc)]);
  const ok = st.accepted_for_test > 0;
  $('readiness').replaceChildren(h('div',{class:'ready-bar '+(ok?'ok':'no')},
    h('p',{text: ok ? `К тесту допущено ${st.accepted_for_test} из ${st.total} вопросов.` : `В банке ${st.total} ${plural(st.total,'вопрос','вопроса','вопросов')}, но преподаватель ещё ни одного не принял.`}),
    ok ? null : h('button',{class:'btn',type:'button',onclick:()=>show('bank')},'Открыть банк')));
  $('history').replaceChildren(...(list.length ? list.map(a => h('button',{class:'hist',type:'button',onclick:async()=>{ try{ renderAttempt(await api('/api/attempts/'+a.id)); }catch(e){toast(e.message,true);} }},
      h('b',{text: a.status==='active' ? 'Продолжить попытку' : 'Результат'}),
      h('span',{text: fmtDate(a.created_at) + (a.status==='finished' ? (a.percent!=null ? ` · ${a.percent}%` : a.total ? ` · ${a.correct} из ${a.total}` : '') : '')}))) : [h('p',{class:'empty',text:'Попыток пока нет.'})]));
}
$('test-form').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async ()=>{
  if (S.attempt && S.attempt.status==='active' && !await confirmAction('Начать новую попытку? Текущую можно продолжить из списка попыток.')) return;
  const a = await api('/api/attempts',{method:'POST',body:params('test')});
  renderAttempt(a); toast(`Тест начат: ${a.available ?? a.questions.length} ${plural(a.available ?? a.questions.length,'вопрос','вопроса','вопросов')}.`); await loadAttempts();
});});
function gradeBox(g){
  if (!g) return null;
  return h('div',{class:'grade'}, h('b',{text:'Оценка ИИ: '+g.score+'/10'}),
    g.strengths ? h('div',null, h('b',{text:'Что хорошо: '}), g.strengths) : null,
    g.missed ? h('div',null, h('b',{text:'Что упущено: '}), g.missed) : null,
    g.advice ? h('div',null, h('b',{text:'Что повторить: '}), g.advice) : null);
}
function renderAttempt(a, target){
  if (!target) S.attempt = a;
  clearTimeout(S.gradingTimer);
  if (['pending','running'].includes(a.grading_status)) S.gradingTimer = setTimeout(async ()=>{
    try{ const fresh = await api((a.assignment_id ? '/api/assignment-attempts/' : '/api/attempts/')+a.id); renderAttempt(fresh, target); if (!target) loadAttempts().catch(()=>{}); } catch {}
  }, 4000);
  const done = a.status === 'finished';
  const box = target || $('attempt');
  const letters = 'ABCD';
  const form = h('form',{class:'qlist'});
  if (done){
    const grading = ['pending','running'].includes(a.grading_status);
    const pct = a.percent ?? (a.total ? Math.round(100*a.correct/a.total) : null);
    form.append(h('div',{class:'panel result'},
      h('div',{class:'ring',style:'--p:'+(pct??0)}, h('div',null, h('div',null, h('b',{text: pct!=null ? pct+'%' : '…'}), h('small',{text: a.percent!=null ? 'итог' : 'тестовые'})))),
      h('div',{class:'grid'}, h('h2',{text: (a.student_name ? a.student_name+': ' : '') + (pct==null ? 'Тест завершён' : pct>=85 ? 'Отличный результат' : pct>=60 ? 'Хороший результат' : 'Есть что повторить')}),
        h('p',{class:'lead',text: (a.total ? `Тестовые: верно ${a.correct} из ${a.total}. ` : '') + (a.open_total ? (grading ? 'Открытые ответы проверяет ИИ…' : a.grading_status==='error' ? 'Автопроверка открытых ответов не удалась — запустите её ещё раз.' : `Общий балл: ${a.points} из ${a.max_points}.`) : '')}),
        grading ? h('div',{class:'progress'},h('i')) : null,
        a.grading_status==='error' || (a.open_total && !a.grading_status) ? btn('Проверить открытые ответы ИИ','', async()=>{ await api('/api/attempts/'+a.id+'/grade',{method:'POST'}); renderAttempt(await api((a.assignment_id ? '/api/assignment-attempts/' : '/api/attempts/')+a.id), target); }) : null)));
  } else {
    form.append(h('div',{class:'panel'}, h('h2',{text:'Тест'}), h('p',{class:'lead',text:`${a.questions.length} ${plural(a.questions.length,'вопрос','вопроса','вопросов')}. Ответы сохранятся, когда вы завершите тест.`})));
  }
  a.questions.forEach((q,i) => {
    const lv = q.checked_level || q.requested_level;
    const card = h('article',{class:'q',style: lv ? '--qc:'+levelOf(lv).color : ''},
      h('div',{class:'chips'}, h('span',{class:'qnum',text:'Вопрос '+(i+1)+' из '+a.questions.length}), lv ? h('span',{class:'chip'}, meter(lv), levelOf(lv).name) : null,
        done && q.type==='mcq' ? h('span',{class:'chip '+(q.correct?'ok':'bad'),text:q.correct?'Верно':'Неверно'}) : null),
      h('h3',{text:q.text}));
    if (q.type==='mcq'){
      if (done){
        card.append(h('ol',{class:'opts'}, ...q.options.map((o,j)=>{ const mine = q.user_answer!=null && Number(q.user_answer)===j; return h('li',{class: j===q.correct_index ? 'right' : mine ? 'wrong' : ''}, h('span',{class:'k',text:letters[j]}), h('span',null, o, mine ? h('span',{class:'hint',text:'  — ваш ответ'}) : null)); })));
        if (q.user_answer==null) card.append(h('p',{class:'note',text:'Вы пропустили этот вопрос.'}));
      } else card.append(h('div',{class:'choice',role:'radiogroup'}, ...q.options.map((o,j)=>h('label',null, h('input',{type:'radio',name:q.id,value:String(j)}), h('span',{class:'k',text:letters[j]}), h('span',{text:o})))));
    } else if (done){
      card.append(h('dl',{class:'answer',style:'border:none;padding:0'}, h('div',null,h('dt',{text:'Ответ'}),h('dd',{text:q.user_answer||'Нет ответа'}))));
      if (q.grade) card.append(gradeBox(q.grade));
      const cur = a.self_scores && a.self_scores[q.id];
      if (!target) card.append(h('div',{class:'field'}, 'Ваша оценка по сравнению с эталоном', h('div',{class:'seg'}, ...[0,1,2,3,4,5].map(n=>h('label',null, h('input',{type:'radio',name:q.id,value:String(n),checked:cur!=null && Number(cur)===n}), h('span',{text:String(n)}))))));
    } else card.append(h('label',{class:'field'}, 'Ваш ответ', h('textarea',{name:q.id,rows:5,maxlength:12000,placeholder:'Ответьте своими словами'})));
    if (done) card.append(h('details',{class:'reveal',open: q.type==='open' || !q.correct}, h('summary',null,'Эталон и источник'),
      h('div',{class:'answer'}, h('dl',null, h('div',null,h('dt',{text:'Эталонный ответ'}),h('dd',{text:q.reference_answer})), q.explanation ? h('div',null,h('dt',{text:'Объяснение'}),h('dd',{text:q.explanation})) : null),
        ...(q.sources||[]).map(s=>h('blockquote',{class:'src'}, h('cite',{text:(q.topic?q.topic+', ':'')+(s.location||'')}), '«'+s.quote+'»')))));
    form.append(card);
  });
  const hasOpen = a.questions.some(q=>q.type==='open');
  const submit = h('button',{class:'btn '+(done?'primary':'gold')+' big',type:'submit'}, done ? 'Сохранить самооценку' : 'Завершить тест');
  if (target) form.append(h('div',{class:'actions'}, h('button',{class:'btn ghost',type:'button',onclick:()=>{ clearTimeout(S.gradingTimer); box.replaceChildren(); }}, 'Закрыть')));
  else if (!done || hasOpen) form.append(h('div',{class:'actions'}, submit, h('button',{class:'btn ghost',type:'button',onclick:()=>{ clearTimeout(S.gradingTimer); box.replaceChildren(); S.attempt=null; }}, 'Закрыть')));
  form.addEventListener('submit', e => { e.preventDefault(); if (target) return; busy(submit, async ()=>{
    const v = new FormData(form);
    if (done){
      const scores = {}; for (const q of a.questions) if (q.type==='open' && v.get(q.id)!==null) scores[q.id] = Number(v.get(q.id));
      const r = await api('/api/attempts/'+a.id+'/self-scores',{method:'POST',body:{scores}}); toast(r.message||'Самооценка сохранена.');
    } else {
      const answers = {}; for (const q of a.questions) answers[q.id] = q.type==='mcq' ? (v.has(q.id) ? Number(v.get(q.id)) : null) : (v.get(q.id)||'');
      const unanswered = a.questions.filter(q => answers[q.id]===null || answers[q.id]==='').length;
      if (unanswered && !await confirmAction(`Без ответа: ${unanswered}. Всё равно завершить тест?`)) return;
      renderAttempt(await api('/api/attempts/'+a.id+'/finish',{method:'POST',body:{answers}}));
      toast('Тест завершён, результат сохранён.'); await loadAttempts();
    }
  });});
  const wasOpen = box.childElementCount > 0;
  box.replaceChildren(form);
  if (!wasOpen) box.scrollIntoView({behavior:'smooth',block:'start'});
}

/* ---------- students: tests by link, results, statistics ---------- */
async function loadStudents(){
  if (!S.doc) return;
  const id = S.doc;
  const [list, stats] = await Promise.all([api('/api/assignments?document_id='+id), api('/api/documents/'+id+'/stats')]);
  if (id !== S.doc) return;
  S.assignments = list;
  renderAssignments(list);
  renderStats(stats);
}
function studentLink(a){ return location.origin + '/s/' + a.token; }
function renderAssignments(list){
  const box = $('assignments');
  if (!list.length){ box.replaceChildren(h('p',{class:'empty',text:'Ссылок пока нет. Примите вопросы в банке и создайте тест для студентов выше.'})); return; }
  box.replaceChildren(...list.map(a => {
    const link = h('input',{readonly:true,value:studentLink(a),onfocus:e=>e.target.select()});
    const st = a.settings || {};
    const descr = [a.question_count+' '+plural(a.question_count,'вопрос','вопроса','вопросов'), st.level ? 'уровень '+st.level : 'все уровни',
      st.show_answers ? 'ответы показываются' : 'только баллы', LANG[st.ui_language]||''].filter(Boolean).join(' · ');
    const finished = a.results.filter(r=>r.status==='finished');
    const avg = finished.filter(r=>r.percent!=null);
    const table = a.results.length ? h('table',{class:'results'},
      h('thead',null,h('tr',null, h('th',null,'Студент'), h('th',null,'Статус'), h('th',null,'Тестовые'), h('th',null,'Итог'), h('th',null,'Когда'))),
      h('tbody',null, ...a.results.map(r => h('tr',{class: r.status==='finished' ? 'clickable' : '', title: r.status==='finished' ? 'Открыть ответы' : '', onclick: r.status==='finished' ? async()=>{ try{ const data = await api('/api/assignment-attempts/'+r.id); renderAttempt(data, $('result-view')); $('result-view').scrollIntoView({behavior:'smooth'}); }catch(e){ toast(e.message,true);} } : null},
        h('td',null,r.student_name||'—'),
        h('td',null, r.status==='finished' ? (['pending','running'].includes(r.grading_status) ? 'проверяется ИИ' : 'завершён') : 'проходит'),
        h('td',null, r.total ? `${r.correct} из ${r.total}` : '—'),
        h('td',null, r.percent!=null ? h('b',{text:r.percent+'%'}) : '—'),
        h('td',null, fmtDate(r.finished_at || r.created_at)))))) : h('p',{class:'hint',text:'Пока никто не прошёл. Отправьте ссылку студентам.'});
    return h('div',{class:'panel grid'},
      h('div',{class:'panel-head'}, h('div',null, h('h2',{text:a.title}), h('p',{class:'lead',text:descr})),
        h('span',{class:'chip '+(a.is_open?'ok':'')}, a.is_open ? 'Приём открыт' : 'Приём закрыт')),
      h('div',{class:'linkbox'}, link,
        btn('Копировать ссылку','primary', async()=>{ try{ await navigator.clipboard.writeText(link.value); toast('Ссылка скопирована. Отправьте её студентам.'); } catch{ link.select(); toast('Скопируйте ссылку вручную: Ctrl+C.'); } }),
        h('a',{class:'btn ghost',href:'/s/'+a.token,target:'_blank',rel:'noopener'},'Открыть как студент')),
      h('div',{class:'funnel'}, h('span',null,h('b',{text:a.results.length}),'начали'), h('span',null,h('b',{text:finished.length}),'завершили'),
        h('span',null,h('b',{text: avg.length ? Math.round(avg.reduce((x,r)=>x+r.percent,0)/avg.length)+'%' : '—'}),'средний итог')),
      table,
      h('div',{class:'actions'},
        btn(a.is_open ? 'Закрыть приём ответов' : 'Открыть приём снова','', async()=>{ const r = await api('/api/assignments/'+a.id,{method:'PATCH',body:{is_open:!a.is_open}}); toast(r.message); await loadStudents(); }),
        a.results.length ? h('a',{class:'btn ghost',href:'/api/assignments/'+a.id+'/results.csv',download:''},'Результаты в CSV') : null,
        !a.results.length ? btn('Удалить','danger push', async()=>{ if(!await confirmAction('Удалить ссылку? Студенты больше не смогут её открыть.')) return; const r = await api('/api/assignments/'+a.id,{method:'DELETE'}); toast(r.message); await loadStudents(); }) : null));
  }));
}
function renderStats(st){
  const box = $('stats');
  if (!st.attempts){ box.replaceChildren(h('p',{class:'empty',text:'Статистика появится после первых завершённых попыток.'})); return; }
  const color = p => p>=85 ? 'var(--ok)' : p>=60 ? 'var(--sky)' : p>=40 ? 'var(--gold)' : 'var(--danger)';
  const rows = items => items.map(t => h('div',{class:'barrow'}, h('span',{text:t.name}), h('div',{class:'track'}, h('i',{style:`width:${t.percent}%;background:${color(t.percent)}`})), h('b',{text:t.percent+'%'})));
  const stat = (n,t) => h('div',{class:'stat'}, h('b',{text:n}), h('span',{text:t}));
  box.replaceChildren(h('div',{class:'grid'},
    h('div',{class:'stats'}, stat(st.attempts,'завершённых попыток'), stat(st.answered,'ответов оценено'), stat(st.percent!=null ? st.percent+'%' : '—','средний результат'), stat(st.weak_topics.length,'тем для повторения')),
    st.weak_topics.length ? h('p',{class:'note',text:'Стоит повторить: '+st.weak_topics.map(t=>`${t.name} (${t.percent}%)`).join(', ')+'.'}) : h('p',{class:'hint',text:'Слабых тем не найдено: везде не ниже 60% (учитываются темы минимум с двумя ответами).'}),
    h('h3',{text:'По темам'}), ...rows(st.topics),
    h('h3',{text:'По уровням сложности'}), ...rows(st.levels.map(l=>({...l, name: l.name ? l.name+' · '+(levelOf(l.name)||{}).name : '—'}))),
    st.students.length ? h('h3',{text:'По студентам'}) : null,
    st.students.length ? h('table',{class:'results'}, h('thead',null,h('tr',null,h('th',null,'Студент'),h('th',null,'Попыток'),h('th',null,'Средний итог'),h('th',null,'Последняя'))),
      h('tbody',null, ...st.students.map(s => h('tr',null, h('td',null,s.name), h('td',null,String(s.attempts)), h('td',null,s.percent+'%'), h('td',null,fmtDate(s.last)))))) : null,
    st.timeline.length > 1 ? h('h3',{text:'Изменение результатов'}) : null,
    st.timeline.length > 1 ? h('div',{class:'coverage',style:'height:110px'}, ...st.timeline.slice(-12).map(p => h('div',{class:'cov',title:p.student+' · '+fmtDate(p.finished_at)},
      h('b',{text:p.percent+'%'}), h('div',{class:'col',style:`height:${Math.max(4, 70*p.percent/100)}px;background:${color(p.percent)}`}), h('small',{text:fmtDate(p.finished_at).split(',')[0]})))) : null));
}
$('assign-form').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async ()=>{
  if (!S.doc) throw Error('Сначала выберите материал слева.');
  const body = { document_id:S.doc, title:$('as-title').value.trim(), topic_id: $('as-topic').value ? Number($('as-topic').value) : null,
    count:Number($('as-count').value), level: $('as-level').value ? Number($('as-level').value) : null, type: segVal('as-type') || 'mixed',
    language:'auto', show_answers: $('as-show').value==='1', ui_language:$('as-ui').value };
  if (body.title.length < 2) throw Error('Укажите название теста.');
  const a = await api('/api/assignments',{method:'POST',body});
  toast(a.message || 'Ссылка создана. Скопируйте её и отправьте студентам.');
  $('as-title').value = '';
  await loadStudents();
});});

/* ---------- upload wiring ---------- */
$('file').addEventListener('change', e => upload(e.target.files[0]));
const drop = $('drop');
['dragenter','dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave','drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => upload(e.dataTransfer.files[0]));
$('show-trash').addEventListener('change', ()=>loadDocuments().catch(e=>toast(e.message,true)));

/* ---------- polling ---------- */
async function poll(){
  if (S.polling) return; S.polling = true;
  try{
    await loadDocuments();
    if (S.doc){
      if (S.detail && (S.detail.status==='processing' || S.detail.topic_status==='processing')) await loadDetail();
      const wasActive = S.jobs.some(j=>['queued','running','waiting'].includes(j.status));
      if (wasActive || S.view==='generate') await loadJobs();
      if (S.view==='students' && !(S.poll5 = ((S.poll5||0)+1) % 4)) await loadStudents();
    }
  } catch(e){ /* тихо: сервер мог перезапускаться */ }
  finally{ S.polling = false; }
}
async function init(){
  buildStatic();
  try { await Promise.all([loadConfig(), loadDocuments(), loadStorage()]); } catch(e){ toast(e.message,true); }
  setInterval(poll, 3000);
}
init();
