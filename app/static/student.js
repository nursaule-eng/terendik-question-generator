'use strict';
/* Student page: one test by link. No access to the question bank or answers before finishing. */
const T = {
  kk: {
    loading:'Жүктелуде…', notFound:'Тест табылмады. Сілтемені оқытушыдан тексеріңіз.', closed:'Оқытушы бұл тестке жауап қабылдауды жапты.',
    questions:n=>`${n} сұрақ`, openNote:n=>`оның ішінде ${n} ашық сұрақ — оларды жасанды интеллект тексереді`,
    name:'Аты-жөніңіз', namePh:'Мысалы: Айгерім Сапарова', start:'Тестті бастау', nameShort:'Аты-жөніңізді толық жазыңыз (кемінде 2 таңба).',
    q:(i,n)=>`Сұрақ ${i} / ${n}`, yourAnswer:'Жауабыңыз', answerPh:'Өз сөзіңізбен жауап беріңіз', answered:(a,n)=>`Жауап берілді: ${a} / ${n}`,
    finish:'Тестті аяқтау', confirmEmpty:n=>`Жауапсыз сұрақтар: ${n}. Сонда да аяқтау керек пе?`, saved:'Жауаптар осы құрылғыда сақталады.',
    resultTitle:p=>p>=85?'Тамаша нәтиже!':p>=60?'Жақсы нәтиже':'Қайталайтын тақырыптар бар',
    mcqScore:(c,t)=>`Тест сұрақтары: ${c} / ${t} дұрыс.`, points:(p,m)=>`Жалпы балл: ${p} / ${m}.`,
    grading:'Ашық жауаптарды жасанды интеллект тексеруде… Бұл бетті жаппаңыз.', gradingError:'Ашық жауаптарды автоматты тексеру сәтсіз аяқталды. Оқытушы кейін тексереді.',
    correct:'Дұрыс', wrong:'Қате', skipped:'Сіз бұл сұрақты өткізіп жібердіңіз.', mine:' — сіздің жауабыңыз', noAnswer:'Жауап жоқ',
    reference:'Эталон жауап', explanation:'Түсіндірме', source:'Дереккөз', score:'Баға', strengths:'Жақсы жазылғаны', missed:'Жетіспейтіні', advice:'Не қайталау керек',
    again:'Қайта өту', hidden:'Оқытушы дұрыс жауаптарды көрсетпеуді таңдады.', error:'Қате орын алды. Қайталап көріңіз.', open:'Ашық сұрақ', test:'Тест сұрағы'
  },
  ru: {
    loading:'Загрузка…', notFound:'Тест не найден. Проверьте ссылку у преподавателя.', closed:'Преподаватель закрыл приём ответов по этому тесту.',
    questions:n=>`${n} ${plural(n,'вопрос','вопроса','вопросов')}`, openNote:n=>`из них ${n} открытых — их проверит ИИ`,
    name:'Имя и фамилия', namePh:'Например: Айгерим Сапарова', start:'Начать тест', nameShort:'Укажите имя и фамилию (минимум 2 символа).',
    q:(i,n)=>`Вопрос ${i} из ${n}`, yourAnswer:'Ваш ответ', answerPh:'Ответьте своими словами', answered:(a,n)=>`Отвечено: ${a} из ${n}`,
    finish:'Завершить тест', confirmEmpty:n=>`Без ответа: ${n}. Всё равно завершить?`, saved:'Ответы сохраняются на этом устройстве.',
    resultTitle:p=>p>=85?'Отличный результат!':p>=60?'Хороший результат':'Есть что повторить',
    mcqScore:(c,t)=>`Тестовые вопросы: верно ${c} из ${t}.`, points:(p,m)=>`Общий балл: ${p} из ${m}.`,
    grading:'ИИ проверяет открытые ответы… Не закрывайте страницу.', gradingError:'Автоматическая проверка открытых ответов не удалась. Преподаватель проверит их позже.',
    correct:'Верно', wrong:'Неверно', skipped:'Вы пропустили этот вопрос.', mine:' — ваш ответ', noAnswer:'Нет ответа',
    reference:'Эталонный ответ', explanation:'Объяснение', source:'Источник', score:'Оценка', strengths:'Что хорошо', missed:'Что упущено', advice:'Что повторить',
    again:'Пройти ещё раз', hidden:'Преподаватель решил не показывать правильные ответы.', error:'Произошла ошибка. Попробуйте ещё раз.', open:'Открытый вопрос', test:'Тестовый вопрос'
  },
  en: {
    loading:'Loading…', notFound:'Test not found. Please check the link with your teacher.', closed:'The teacher has closed this test.',
    questions:n=>`${n} question${n===1?'':'s'}`, openNote:n=>`${n} open-ended — graded by AI`,
    name:'Full name', namePh:'e.g. Aigerim Saparova', start:'Start the test', nameShort:'Please enter your full name (at least 2 characters).',
    q:(i,n)=>`Question ${i} of ${n}`, yourAnswer:'Your answer', answerPh:'Answer in your own words', answered:(a,n)=>`Answered: ${a} of ${n}`,
    finish:'Finish the test', confirmEmpty:n=>`${n} unanswered. Finish anyway?`, saved:'Your answers are kept on this device.',
    resultTitle:p=>p>=85?'Excellent!':p>=60?'Good result':'Some topics to review',
    mcqScore:(c,t)=>`Multiple choice: ${c} of ${t} correct.`, points:(p,m)=>`Total: ${p} of ${m} points.`,
    grading:'AI is grading your open answers… Please keep this page open.', gradingError:'Automatic grading failed. Your teacher will review the open answers.',
    correct:'Correct', wrong:'Incorrect', skipped:'You skipped this question.', mine:' — your answer', noAnswer:'No answer',
    reference:'Model answer', explanation:'Explanation', source:'Source', score:'Score', strengths:'Strengths', missed:'Missing', advice:'Review',
    again:'Try again', hidden:'Your teacher chose not to show the correct answers.', error:'Something went wrong. Please try again.', open:'Open question', test:'Multiple choice'
  }
};
function plural(n, one, few, many){ n=Math.abs(n)%100; const n1=n%10; if(n>10&&n<20) return many; if(n1>1&&n1<5) return few; if(n1===1) return one; return many; }

const token = decodeURIComponent(location.pathname.split('/').filter(Boolean)[1] || '');
const store = {
  get(k){ try{ return JSON.parse(localStorage.getItem(k)); }catch{ return null; } },
  set(k,v){ try{ localStorage.setItem(k, JSON.stringify(v)); }catch{} },
  del(k){ try{ localStorage.removeItem(k); }catch{} }
};
const KEY = 'terendik:' + token, DRAFT = 'terendik-draft:' + token;
const S = {lang: store.get('terendik:lang') || null, info:null, attempt:null, key:null, timer:null};
const $ = id => document.getElementById(id);
const t = k => T[S.lang][k];

function h(tag, attrs, ...kids){
  const n = document.createElement(tag);
  if (attrs) for (const [k,v] of Object.entries(attrs)){
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') n.className = v; else if (k === 'text') n.textContent = v;
    else if (k === 'style') n.style.cssText = v; else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
function toast(msg, error=false){ const el = h('div',{class:'toast'+(error?' error':'')}, h('span',{text:msg})); $('toasts').append(el); setTimeout(()=>el.remove(), 7000); }
async function api(path, opt={}){
  opt.headers = Object.assign({}, opt.headers || {}, S.key ? {'X-Attempt-Key': S.key} : {});
  if (opt.body){ opt.body = JSON.stringify(opt.body); opt.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(path, opt);
  let data = null; try { data = await r.json(); } catch {}
  if (!r.ok){ const e = Error((data && typeof data.detail === 'string' && data.detail) || t('error')); e.status = r.status; throw e; }
  return data;
}

function langSwitch(){
  $('lang').replaceChildren(...[['kk','Қазақша'],['ru','Русский'],['en','English']].map(([v,label]) =>
    h('label',null, h('input',{type:'radio',name:'lang',value:v,checked:S.lang===v,onchange:()=>{ S.lang=v; store.set('terendik:lang', v); document.documentElement.lang=v; render(); }}), h('span',{text:label}))));
  document.documentElement.lang = S.lang;
}

function render(){
  langSwitch();
  if (!S.info) return;
  if (!S.attempt) return renderStart();
  if (S.attempt.status === 'active') return renderTest();
  return renderResult();
}

function renderStart(){
  const info = S.info;
  const input = h('input',{id:'name',required:true,maxlength:'120',autocomplete:'name',placeholder:t('namePh')});
  const saved = store.get('terendik:name'); if (saved) input.value = saved;
  const form = h('form',{class:'panel grid'},
    h('h1',{text:info.title}),
    h('p',{class:'lead'}, t('questions')(info.question_count) + (info.open_questions ? ' · ' + t('openNote')(info.open_questions) : '')),
    info.is_open ? h('label',{class:'field'}, t('name'), input) : h('p',{class:'note bad',text:t('closed')}),
    info.is_open ? h('div',{class:'actions'}, h('button',{class:'btn gold big',type:'submit'}, t('start'))) : null);
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const name = input.value.trim();
    if (name.length < 2) return toast(t('nameShort'), true);
    const button = form.querySelector('button'); button.disabled = true;
    try{
      const a = await api(`/api/s/${encodeURIComponent(token)}/start`, {method:'POST', body:{name}});
      S.key = a.access_key; S.attempt = a;
      store.set(KEY, {id:a.id, key:a.access_key}); store.set('terendik:name', name); store.del(DRAFT);
      render(); scrollTo({top:0});
    } catch(err){ toast(err.message, true); button.disabled = false; }
  });
  $('app').replaceChildren(form);
}

function renderTest(){
  const a = S.attempt, n = a.questions.length;
  const draft = store.get(DRAFT) || {};
  const letters = 'ABCD';
  const form = h('form',{class:'qlist'});
  form.append(h('div',{class:'panel'}, h('h1',{text:S.info.title}), h('p',{class:'lead',text:t('questions')(n) + '. ' + t('saved')})));
  a.questions.forEach((q,i) => {
    const card = h('article',{class:'q'}, h('div',{class:'chips'}, h('span',{class:'qnum',text:t('q')(i+1,n)}), h('span',{class:'chip',text: q.type==='mcq' ? t('test') : t('open')})), h('h3',{text:q.text}));
    if (q.type === 'mcq') card.append(h('div',{class:'choice',role:'radiogroup'}, ...q.options.map((o,j)=>h('label',null,
      h('input',{type:'radio',name:q.id,value:String(j),checked: draft[q.id]===j}), h('span',{class:'k',text:letters[j]}), h('span',{text:o})))));
    else { const ta = h('textarea',{name:q.id,rows:'5',maxlength:'12000',placeholder:t('answerPh')}); ta.value = draft[q.id] || ''; card.append(h('label',{class:'field'}, t('yourAnswer'), ta)); }
    form.append(card);
  });
  const bar = h('i'); const counter = h('span',{class:'hint'});
  const submit = h('button',{class:'btn gold big',type:'submit'}, t('finish'));
  form.append(h('div',{class:'s-sticky'}, h('div',{class:'s-progress'}, bar), counter, submit));
  const collect = () => { const v = new FormData(form), out = {}; for (const q of a.questions){ out[q.id] = q.type==='mcq' ? (v.has(q.id) ? Number(v.get(q.id)) : null) : (v.get(q.id) || ''); } return out; };
  const update = () => { const ans = collect(); store.set(DRAFT, ans); const done = Object.values(ans).filter(x => x !== null && x !== '').length; bar.style.width = (100*done/n)+'%'; counter.textContent = t('answered')(done,n); };
  form.addEventListener('input', update); update();
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const answers = collect();
    const empty = Object.values(answers).filter(x => x === null || x === '').length;
    if (empty && !confirm(t('confirmEmpty')(empty))) return;
    submit.disabled = true;
    try{
      S.attempt = await api(`/api/s/${encodeURIComponent(token)}/attempts/${a.id}/finish`, {method:'POST', body:{answers}});
      store.del(DRAFT); render(); scrollTo({top:0});
    } catch(err){ toast(err.message, true); submit.disabled = false; }
  });
  $('app').replaceChildren(form);
}

function renderResult(){
  const a = S.attempt, letters = 'ABCD';
  clearTimeout(S.timer);
  const grading = ['pending','running'].includes(a.grading_status);
  const pct = a.percent ?? (a.total ? Math.round(100*a.correct/a.total) : null);
  const box = h('div',{class:'qlist'});
  box.append(h('div',{class:'panel result'},
    h('div',{class:'ring',style:'--p:'+(pct??0)}, h('div',null, h('div',null, h('b',{text: pct==null ? '…' : pct+'%'})))),
    h('div',{class:'grid'}, h('h2',{text: pct==null ? S.info.title : t('resultTitle')(pct)}),
      a.total ? h('p',{class:'lead',text:t('mcqScore')(a.correct,a.total)}) : null,
      a.open_total && !grading ? h('p',{class:'lead',text:t('points')(a.points,a.max_points)}) : null,
      grading ? h('div',{class:'grid'}, h('p',{class:'note',text:t('grading')}), h('div',{class:'progress'},h('i'))) : null,
      a.grading_status==='error' ? h('p',{class:'note',text:t('gradingError')}) : null,
      a.show_answers ? null : h('p',{class:'hint',text:t('hidden')}))));
  a.questions.forEach((q,i) => {
    const card = h('article',{class:'q'}, h('div',{class:'chips'}, h('span',{class:'qnum',text:t('q')(i+1,a.questions.length)}),
      q.type==='mcq' ? h('span',{class:'chip '+(q.correct?'ok':'bad'),text:q.correct?t('correct'):t('wrong')}) :
        q.grade ? h('span',{class:'chip '+(q.grade.score>=7?'ok':q.grade.score>=4?'warn':'bad'),text:`${t('score')}: ${q.grade.score}/10`}) : null),
      h('h3',{text:q.text}));
    if (q.type==='mcq'){
      card.append(h('ol',{class:'opts'}, ...q.options.map((o,j)=>{ const mine = q.user_answer!=null && Number(q.user_answer)===j;
        const right = q.correct_index !== undefined && j===q.correct_index;
        return h('li',{class: right ? 'right' : mine ? (q.correct ? 'right' : 'wrong') : ''}, h('span',{class:'k',text:letters[j]}), h('span',null, o, mine ? h('span',{class:'hint',text:t('mine')}) : null)); })));
      if (q.user_answer==null) card.append(h('p',{class:'note',text:t('skipped')}));
    } else {
      card.append(h('dl',{class:'answer',style:'border:none;padding:0'}, h('div',null, h('dt',{text:t('yourAnswer')}), h('dd',{text:q.user_answer||t('noAnswer')}))));
      if (q.grade) card.append(h('div',{class:'grade'},
        h('div',null, h('b',{class:'score',text:q.grade.score+'/10'})),
        q.grade.strengths ? h('div',null, h('b',{text:t('strengths')+': '}), q.grade.strengths) : null,
        q.grade.missed ? h('div',null, h('b',{text:t('missed')+': '}), q.grade.missed) : null,
        q.grade.advice ? h('div',null, h('b',{text:t('advice')+': '}), q.grade.advice) : null));
    }
    if (q.reference_answer) card.append(h('details',{class:'reveal',open: q.type==='open' || !q.correct}, h('summary',null, t('reference')),
      h('div',{class:'answer'}, h('dl',null, h('div',null, h('dt',{text:t('reference')}), h('dd',{text:q.reference_answer})),
        q.explanation ? h('div',null, h('dt',{text:t('explanation')}), h('dd',{text:q.explanation})) : null),
        ...(q.sources||[]).map(s=>h('blockquote',{class:'src'}, h('cite',{text:t('source')+(s.location?': '+s.location:'')}), '«'+s.quote+'»')))));
    box.append(card);
  });
  if (S.info.is_open) box.append(h('div',{class:'actions'}, h('button',{class:'btn',type:'button',onclick:()=>{ store.del(KEY); S.attempt=null; S.key=null; render(); scrollTo({top:0}); }}, t('again'))));
  $('app').replaceChildren(box);
  if (grading) S.timer = setTimeout(async ()=>{ try{ S.attempt = await api(`/api/s/${encodeURIComponent(token)}/attempts/${a.id}`); render(); } catch{ S.timer = setTimeout(renderResult, 5000); } }, 4000);
}

async function init(){
  if (!S.lang) S.lang = 'kk';
  langSwitch();
  $('app').replaceChildren(h('p',{class:'lead',text:t('loading')}));
  try{
    S.info = await api(`/api/s/${encodeURIComponent(token)}`);
  } catch(err){
    $('app').replaceChildren(h('div',{class:'panel'}, h('p',{class:'note bad',text: err.status===404 ? t('notFound') : err.message})));
    return;
  }
  if (!store.get('terendik:lang')) S.lang = S.info.ui_language || 'kk';
  const saved = store.get(KEY);
  if (saved && saved.id && saved.key){
    S.key = saved.key;
    try{ S.attempt = await api(`/api/s/${encodeURIComponent(token)}/attempts/${saved.id}`); }
    catch{ store.del(KEY); S.key = null; S.attempt = null; }
  }
  document.title = S.info.title + ' · Тереңдік';
  render();
}
init();
