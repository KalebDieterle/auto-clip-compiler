'use strict';
/* Auto Clip Compiler UI. Talks to Python only through window.pywebview.api (see clipcompiler/ui/api.py). */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const S = {
  api: null, state: null, screen: null, seq: 0, busy: null,
  scan: null, rescan: false, review: null, visible: [], sel: -1, filter: 'all',
  judge: null, reasons: {}, minScore: 7,
};

// ---------- helpers ----------
const money = (v) => '$' + (v < 10 ? v.toFixed(2) : Math.round(v).toLocaleString());
const num = (v) => Number(v).toLocaleString();
const clock = (s) => { s = Math.max(0, s); const m = Math.floor(s / 60); return `${m}:${(s - m * 60).toFixed(1).padStart(4, '0')}`; };
const duration = (s) => s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round(s % 3600 / 60)} min` : s >= 60 ? `${Math.round(s / 60)} min` : `${Math.max(1, Math.round(s))} s`;
const when = (t) => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
const day = (t) => new Date(t * 1000).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

async function call(name, ...args) {
  const result = await S.api[name](...args);
  if (result && result.error) { toast(result.error, 'bad'); throw new Error(result.error); }
  return result;
}

function toast(text, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`; el.textContent = text; $('#toasts').append(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 400); }, kind === 'bad' ? 6000 : 3500);
}

function confirmBox(title, body, yes = 'Continue') {
  return new Promise((resolve) => {
    $('#modalTitle').textContent = title; $('#modalBody').textContent = body; $('#modalYes').textContent = yes;
    $('#modal').hidden = false;
    const done = (v) => { $('#modal').hidden = true; resolve(v); };
    $('#modalYes').onclick = () => done(true); $('#modalNo').onclick = () => done(false);
  });
}

function animateNumber(el, to, format = num) {
  const from = Number(el.dataset.v || 0); el.dataset.v = to;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches || from === to) { el.textContent = format(to); return; }
  const start = performance.now(), span = 450;
  const step = (now) => {
    const k = Math.min(1, (now - start) / span), e = 1 - Math.pow(1 - k, 3);
    el.textContent = format(from + (to - from) * e);
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ---------- segmented controls, sliders, folder buttons ----------
function seg(el, value) {
  if (value !== undefined) $$('button', el).forEach((b) => b.classList.toggle('on', b.dataset.v === String(value)));
  const on = $('button.on', el), glider = $('.seg-glider', el);
  if (on && glider && on.offsetWidth) { glider.style.width = on.offsetWidth + 'px'; glider.style.transform = `translateX(${on.offsetLeft}px)`; }
  return on ? on.dataset.v : null;
}
function onSeg(el, handler) {
  el.addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) { seg(el, b.dataset.v); handler(b.dataset.v); } });
}
function fillRange(input) {
  const k = (input.value - input.min) / (input.max - input.min) * 100;
  input.style.setProperty('--fill', k + '%');
}
function setFolder(button, path, empty = 'Choose folder') {
  $('span', button).textContent = path || empty; button.classList.toggle('set', !!path); button.title = path || '';
}
async function pickFolder(initial) { return (await call('pick_folder', initial || '')).path; }

// ---------- navigation ----------
const onEnter = {};
function go(name) {
  S.screen = name;
  $$('.screen').forEach((s) => s.classList.toggle('on', s.dataset.screen === name));
  const tab = { judging: 'home', batch: 'review', render: 'review', welcome: null }[name] ?? name;
  $$('.nav').forEach((n) => n.classList.toggle('on', n.dataset.go === tab));
  $('.rail nav').style.visibility = name === 'welcome' ? 'hidden' : '';
  requestAnimationFrame(() => $$('.screen.on .seg').forEach((el) => seg(el)));
  onEnter[name]?.();
}
$$('.nav').forEach((n) => n.addEventListener('click', () => {
  if (n.dataset.go === 'home' && S.busy === 'judge') return go('judging');
  if (n.dataset.go === 'review' && S.busy === 'render') return go('render');
  if (n.dataset.go === 'review' && isBatchRun(S.state?.run)) return go('batch');
  go(n.dataset.go);
}));
$$('[data-url]').forEach((b) => b.addEventListener('click', () => call('open_url', b.dataset.url)));

// ---------- event loop ----------
async function poll() {
  try {
    const r = await S.api.events(S.seq);
    for (const e of r.events) { S.seq = e.seq; try { handle(e); } catch (err) { console.error(err); } }
    setBusy(r.busy);
  } catch (err) { console.error(err); }
  setTimeout(poll, S.busy ? 250 : 900);
}
function setBusy(kind) {
  S.busy = kind;
  $('#busyDot').classList.toggle('on', !!kind);
  $('#busyDot').title = kind ? `Working: ${kind}` : '';
}

const STOP_TEXT = {
  budget: (m) => `Budget cap reached (${m}). Everything judged so far is saved; raise the cap in New to judge more.`,
  daily_limit: () => "Gemini's daily limit was reached. Everything judged so far is saved. Turn on billing in AI Studio, or continue tomorrow.",
  auth: () => 'Google rejected the API key. Replace it in Settings.',
  target: () => 'Found enough moments for the target length.',
  cancelled: () => 'Stopped. Everything judged so far is saved and will not be paid for again.',
  batch_expired: () => 'Google did not finish this batch within 48 hours, so nothing was charged. Run it again, or use Live.',
  batch_failed: () => 'Google reported the batch as failed. Nothing was charged for unfinished clips; try again or use Live.',
  batch_cancelled: () => 'The batch was cancelled. Clips Google finished first are saved.',
};

function handle(e) {
  switch (e.type) {
    case 'scan': return scanProgress(e);
    case 'scan_done': return scanDone(e);
    case 'judge_start': return judgeStart(e);
    case 'clip': return judgeClip(e);
    case 'stopping': return judgeStopping(e);
    case 'review_ready': return reviewReady(e);
    case 'render_start': return renderStart(e);
    case 'render': return renderProgress(e);
    case 'part_done': return partDone(e);
    case 'render_done': return renderDone(e);
    case 'render_cancelled': return renderStopped('Stopped. Earlier files were not touched.');
    case 'batch_upload': return batchUpload(e);
    case 'batch_waiting': refreshState().then(() => { if (S.screen === 'batch') paintBatch(); }); return;
    case 'batch_collecting': return batchCollecting(e);
    case 'batch_cancelled': refreshState().then(paintBatch); return;
    case 'scan_cancelled': return;
    case 'error':
      toast(e.message, 'bad');
      if (e.job === 'render') renderStopped(e.message);
      if (e.job === 'scan') { $('#scanState').textContent = e.message; $('#scanBar').hidden = true; }
      if (e.job === 'judge') banner(e.message, 'bad');
      if (e.job === 'batch' || e.job === 'collect') { $('#bHint').textContent = e.message; refreshState().then(paintBatch); }
      return;
    case 'idle':
      refreshState();
      if (S.rescan) { S.rescan = false; scan(); }
  }
}

async function refreshState() {
  S.state = await S.api.get_state();
  $('.nav[data-go="review"]').disabled = !S.state.run;
  renderRecent();
}

// ---------- onboarding ----------
let obStep = 0;
function obShow(i) {
  obStep = i;
  $$('.ob-step').forEach((s) => s.classList.toggle('on', +s.dataset.step === i));
  $$('#obSteps i').forEach((d, j) => d.classList.toggle('on', j <= i));
}
$$('[data-next]').forEach((b) => b.addEventListener('click', () => obShow(obStep + 1)));
$$('[data-back]').forEach((b) => b.addEventListener('click', () => obShow(obStep - 1)));
onEnter.welcome = () => {
  obShow(0);
  $('#obKeyNext').disabled = !S.state.key.present;
  if (S.state.key.present) keyState($('#obKeyState'), 'ok', `Saved key ${S.state.key.masked}`);
  setFolder($('#obSource'), S.state.settings.source); $('#obSourceNext').disabled = !S.state.settings.source;
  setFolder($('#obOutput'), S.state.settings.output); $('#obFinish').disabled = !S.state.settings.output;
};
function keyState(el, kind, text) {
  el.className = `keystate ${kind}`;
  const icon = kind === 'ok' ? '<svg class="ic"><use href="#i-check"/></svg>' : kind === 'wait' ? '<i class="spinner"></i>' : '';
  el.innerHTML = icon + `<span>${esc(text)}</span>`;
}
async function saveKey(input, stateEl, onOk) {
  const key = input.value.trim();
  if (!key) return;
  keyState(stateEl, 'wait', 'Checking with Google...');
  const r = await S.api.set_key(key, true);
  if (r.error) return keyState(stateEl, 'bad', r.error);
  if (r.saved) { input.value = ''; keyState(stateEl, 'ok', `Key works. Saved as ${r.masked}`); onOk?.(); refreshState(); return; }
  keyState(stateEl, 'bad', r.message);
  if (r.state === 'network' && await confirmBox("Couldn't reach Google", 'Save the key anyway and check it later?', 'Save anyway')) {
    const forced = await call('set_key', key, false); input.value = ''; keyState(stateEl, 'ok', `Saved as ${forced.masked}`); onOk?.(); refreshState();
  }
}
$('#obKeySave').addEventListener('click', () => saveKey($('#obKey'), $('#obKeyState'), () => { $('#obKeyNext').disabled = false; }));
$('#obKey').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#obKeySave').click(); });
$('#obSource').addEventListener('click', async () => {
  const p = await pickFolder(S.state.settings.source); if (!p) return;
  await saveSettings({ source: p }); setFolder($('#obSource'), p); $('#obSourceNext').disabled = false;
});
$('#obOutput').addEventListener('click', async () => {
  const p = await pickFolder(S.state.settings.output); if (!p) return;
  await saveSettings({ output: p }); setFolder($('#obOutput'), p); $('#obFinish').disabled = false;
});
$('#obFinish').addEventListener('click', async () => { await saveSettings({ onboarded: true }); go('home'); });

async function saveSettings(changes) {
  const r = await call('save_settings', changes);
  S.state.settings = r.settings; return r.settings;
}

// ---------- home ----------
onEnter.home = () => {
  const s = S.state.settings;
  setFolder($('#srcPick'), s.source); setFolder($('#outPick'), s.output);
  seg($('#mode'), s.mode); modeLabels(s.mode);
  seg($('#judgeMode'), s.judge_mode); judgeHint(s.judge_mode);
  $('#length').value = s.mode === 'reel' ? s.target_minutes : s.part_minutes;
  $('#budget').value = s.budget_usd;
  $('#minScore').value = s.min_score; $('#minScoreOut').textContent = s.min_score; fillRange($('#minScore'));
  $('#homeSub').textContent = S.state.key.present ? '' : 'Add your Gemini API key in Settings to judge new clips.';
  renderRecent();
  if (s.source && !S.scan && !S.busy) scan();
  updateStart();
};
function modeLabels(mode) {
  $('#lengthLabel').textContent = mode === 'reel' ? 'Video length (min)' : 'Part length (min)';
}
$('#srcPick').addEventListener('click', async () => {
  const p = await pickFolder(S.state.settings.source); if (!p) return;
  await saveSettings({ source: p }); setFolder($('#srcPick'), p); scan();
});
$('#outPick').addEventListener('click', async () => {
  const p = await pickFolder(S.state.settings.output); if (!p) return;
  await saveSettings({ output: p }); setFolder($('#outPick'), p); renderRecent(); updateStart();
});
onSeg($('#mode'), async (v) => {
  modeLabels(v);
  const s = await saveSettings({ mode: v });
  $('#length').value = v === 'reel' ? s.target_minutes : s.part_minutes; updateStart();
});
const JUDGE_HINT = {
  live: 'Results in minutes while you watch. One video stops judging once it has enough.',
  batch: 'Half price. Google returns results within 24 hours, often under an hour. Your PC only needs to be on while clips upload. Judges every clip in the range.',
};
function judgeHint(v) { $('#judgeHint').textContent = JUDGE_HINT[v]; }
onSeg($('#judgeMode'), async (v) => { judgeHint(v); await saveSettings({ judge_mode: v }); paintEstimate(); updateStart(); });
$('#length').addEventListener('change', async (e) => {
  const v = Math.min(600, Math.max(1, Number(e.target.value) || 30)); e.target.value = v;
  await saveSettings(S.state.settings.mode === 'reel' ? { target_minutes: v } : { part_minutes: v }); updateStart();
});
$('#budget').addEventListener('change', async (e) => {
  const v = Math.max(0, Number(e.target.value) || 0); e.target.value = v; await saveSettings({ budget_usd: v }); updateStart();
});
$('#minScore').addEventListener('input', (e) => { $('#minScoreOut').textContent = e.target.value; fillRange(e.target); });
$('#minScore').addEventListener('change', (e) => saveSettings({ min_score: Number(e.target.value) }));
const rescanSoon = debounce(() => scan(), 450);
$('#since').addEventListener('change', rescanSoon); $('#until').addEventListener('change', rescanSoon);

async function scan() {
  if (!S.state.settings.source) return;
  if (S.busy === 'scan') { S.rescan = true; await S.api.cancel(); return; }
  if (S.busy) return;
  S.scan = null; updateStart();
  $('#scanStats').hidden = true; $('#scanRange').textContent = '';
  $('#scanState').innerHTML = '<i class="spinner"></i><span>Finding clips...</span>';
  const r = await S.api.scan(S.state.settings.source, $('#since').value || null, $('#until').value || null);
  if (r.error) { $('#scanState').textContent = r.error; return; }
  setBusy('scan');
}
function scanProgress(e) {
  $('#scanBar').hidden = false; $('#scanBar i').style.width = (e.total ? e.done / e.total * 100 : 0) + '%';
  $('#scanState').innerHTML = `<i class="spinner"></i><span>Reading clips ${num(e.done)} of ${num(e.total)}</span>`;
}
function scanDone(e) {
  S.scan = e; $('#scanBar').hidden = true;
  if (!e.clips) { $('#scanState').textContent = 'No video files found in this folder for these dates.'; $('#scanStats').hidden = true; updateStart(); return; }
  $('#scanState').textContent = ''; $('#scanStats').hidden = false;
  animateNumber($('#sClips'), e.clips); animateNumber($('#sCached'), e.cached); animateNumber($('#sTodo'), e.to_judge);
  paintEstimate();
  $('#scanRange').textContent = `${day(e.first)} to ${day(e.last)} · ${e.gb} GB` + (e.unreadable ? ` · ${e.unreadable} unreadable` : '')
    + (e.in_batch ? ` · ${num(e.in_batch)} waiting in a batch` : '');
  updateStart();
}
function batchMode() { return S.state.settings.judge_mode === 'batch'; }
function estimate() { return S.scan ? (batchMode() ? S.scan.estimate_batch_usd : S.scan.estimate_usd) : 0; }
function paintEstimate() {
  if (!S.scan) return;
  animateNumber($('#sCost'), estimate(), money);
  $('#sCost').nextElementSibling.textContent = batchMode() ? 'estimated, batch price' : 'estimated';
}
function updateStart() {
  const btn = $('#startBtn'), hint = $('#startHint'), s = S.state.settings, e = S.scan;
  hint.textContent = '';
  if (!e || !e.clips) { btn.disabled = true; btn.textContent = 'Start'; return; }
  btn.disabled = !s.output;
  if (!s.output) hint.textContent = 'Choose an output folder.';
  if (e.to_judge === 0) { btn.textContent = 'Build from saved judgments'; return; }
  const upTo = s.mode === 'reel' && !batchMode() ? 'up to ' : '';
  btn.textContent = `${batchMode() ? 'Submit' : 'Judge'} ${num(e.to_judge)} clips · ${upTo}~${money(estimate())}`;
  const perClip = batchMode() ? e.batch_per_clip : e.batch_per_clip * 2;
  if (!S.state.key.present) { btn.disabled = true; hint.textContent = 'Add your Gemini API key in Settings first.'; }
  else if (s.budget_usd && estimate() > s.budget_usd) {
    const fit = Math.floor(s.budget_usd / perClip);
    hint.textContent = batchMode() ? `Your ${money(s.budget_usd)} cap fits the newest ${num(fit)} clips; the rest are left for later.`
      : `Your ${money(s.budget_usd)} cap stops judging after roughly ${num(fit)} clips.`;
    if (batchMode()) btn.textContent = `Submit ${num(Math.min(fit, e.to_judge))} clips · ~${money(Math.min(fit, e.to_judge) * perClip)}`;
  }
  else if (s.mode === 'reel' && !batchMode()) hint.textContent = 'Judging stops once there is enough for the video, newest clips first.';
}
$('#startBtn').addEventListener('click', async () => {
  const e = S.scan, s = S.state.settings;
  const spend = Math.min(estimate(), s.budget_usd || Infinity);
  const what = batchMode() ? 'Submit this batch?' : 'Start judging?';
  if (spend >= 1 && !await confirmBox(what, `This can spend up to about ${money(spend)} on your Google account. Judged clips are saved, so they are never paid for twice.`, batchMode() ? 'Submit' : 'Start')) return;
  const mode = batchMode() && e.to_judge > 0;
  await call('start_judge'); S.scan = null;
  if (mode) { setBusy('batch'); await refreshState(); go('batch'); } else go('judging');
});

function renderRecent() {
  const runs = S.state?.recent || [];
  $('#recentPanel').hidden = !runs.length;
  const label = { batch_waiting: 'at Google', batch_uploading: 'uploading', judging: 'stopped' };
  $('#recentList').innerHTML = runs.map((r, i) => {
    const what = r.status.startsWith('batch') || (r.judge_mode === 'batch' && !r.moments) ? `batch of ${r.batch?.clips ?? '?'} clips` : `${r.moments} moments`;
    return `<li data-i="${i}"><div>${esc(new Date(r.created).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }))}<br><span>${what} · ${r.mode === 'reel' ? 'one video' : 'parts'}</span></div><span class="pill ${r.status}">${label[r.status] || r.status}</span></li>`;
  }).join('');
  $$('#recentList li').forEach((li) => li.addEventListener('click', async () => {
    if (S.busy) return toast('Wait for the current job to finish.');
    S.review = await call('open_run', runs[li.dataset.i].folder); await refreshState();
    go(isBatchRun(S.state.run) ? 'batch' : 'review');
  }));
}

// ---------- judging ----------
const RING = 326.7;
function judgeStart(e) {
  S.judge = { total: e.total, done: 0, fresh: 0, moments: 0, started: performance.now(), firstFresh: null };
  $('#feed').innerHTML = ''; $('#judgeBanner').hidden = true;
  $('#jTotal').textContent = `of ${num(e.total)}`; $('#jDone').textContent = '0'; $('#jDone').dataset.v = 0;
  $('#jMoments').textContent = '0'; $('#jMoments').dataset.v = 0; $('#jSpent').textContent = '$0.00'; $('#jEta').textContent = '-';
  const cap = S.state.settings.budget_usd;
  $('#jCap').textContent = cap ? `of ${money(cap)} cap` : 'spent';
  $('#budgetBar').hidden = !cap; $('#budgetBar i').style.width = '0%';
  $('#ringFill').style.strokeDashoffset = RING;
  if (S.screen !== 'judging') go('judging');
}
function judgeClip(e) {
  const j = S.judge; if (!j) return;
  j.done = e.done;
  if (e.status === 'judged') { j.fresh++; j.firstFresh ??= performance.now(); }
  const min = S.state.settings.min_score;
  j.moments += (e.scores || []).filter((s) => s >= min).length;
  animateNumber($('#jDone'), e.done); animateNumber($('#jMoments'), j.moments);
  $('#jSpent').textContent = money(e.usd);
  $('#ringFill').style.strokeDashoffset = RING * (1 - e.done / Math.max(1, e.total));
  const cap = S.state.settings.budget_usd;
  if (cap) { $('#budgetBar i').style.width = Math.min(100, e.usd / cap * 100) + '%'; $('#budgetBar').classList.toggle('warn', e.usd / cap > .8); }
  const toJudge = S.scan?.to_judge ?? e.total;
  if (j.fresh >= 3) {
    const per = (performance.now() - j.started) / j.fresh / 1000, left = Math.max(0, toJudge - j.fresh);
    $('#jEta').textContent = left ? '~' + duration(per * left) : 'finishing';
  }
  const li = document.createElement('li');
  const chips = e.status === 'failed' ? '<span class="chip fail">failed</span>'
    : e.status === 'unjudged' ? '<span class="chip">not judged</span>'
    : (e.scores?.length ? e.scores.map((s) => `<span class="chip ${s >= min ? 'hot' : ''}">${s}</span>`).join('') : '<span class="chip">none</span>');
  const why = e.status === 'failed' ? e.error : (e.reasons || [])[0];
  li.innerHTML = `<span class="name">${esc(e.name)}${e.status === 'cached' ? ' <span class="pill">saved</span>' : ''}</span><span class="chips">${chips}</span>${why ? `<span class="why">${esc(why)}</span>` : ''}`;
  $('#feed').prepend(li);
  while ($('#feed').children.length > 60) $('#feed').lastChild.remove();
}
function banner(text, kind = 'warn') { const b = $('#judgeBanner'); b.className = `banner ${kind}`; b.textContent = text; b.hidden = false; }
function judgeStopping(e) {
  const text = STOP_TEXT[e.reason]?.(e.message) || e.message;
  if (e.reason !== 'target') banner(text, e.reason === 'auth' ? 'bad' : 'warn');
}
$('#cancelJudge').addEventListener('click', async () => { await S.api.cancel(); $('#cancelJudge').disabled = true; banner('Stopping after the clips in progress...'); });
onEnter.judging = () => { $('#cancelJudge').disabled = false; };

async function reviewReady(e) {
  S.scan = null;
  if (S.screen === 'batch' || S.collecting) toast('Batch results are in. Review your moments.');
  S.collecting = false;
  if (e.stopped && e.stopped !== 'target') toast(STOP_TEXT[e.stopped]?.('') || e.stopped);
  S.review = await call('get_review'); await refreshState(); go('review');
}

// ---------- review ----------
const video = $('#video');
let thumbObserver;
onEnter.review = async () => {
  if (!S.review) S.review = await call('get_review');
  seg($('#filter'), S.filter); drawCards();
};
function drawCards() {
  const r = S.review, cards = $('#cards');
  const run = r.run;
  $('#reviewSub').textContent = `${r.totals.total} moments scoring ${run.min_score}+ · ${run.mode === 'reel' ? `one video, target ${run.target_minutes} min` : `parts of ~${run.part_minutes} min`}`;
  thumbObserver?.disconnect();
  thumbObserver = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (!en.isIntersecting) return;
    thumbObserver.unobserve(en.target); loadThumb(en.target);
  }), { root: cards, rootMargin: '200px' });
  cards.innerHTML = '';
  if (!r.moments.length) { cards.innerHTML = '<div class="empty">No moments reached the minimum score. Try a lower score or a wider date range.</div>'; }
  r.moments.forEach((m, i) => {
    const el = document.createElement('div');
    el.className = 'card'; el.dataset.i = i; el.style.animationDelay = Math.min(i, 24) * 18 + 'ms';
    el.innerHTML = `<div class="thumb loading"><span class="score s${m.score}">${m.score}</span><span class="mark"><svg class="ic"><use href="#i-check"/></svg></span><span class="len"></span></div>
      <div class="meta"><span class="when">${esc(when(m.recorded_at))}</span><span class="why">${esc(m.reason)}</span></div>`;
    el.addEventListener('click', () => select(i));
    $('.mark', el).addEventListener('click', (ev) => { ev.stopPropagation(); decide(i, !S.review.moments[i].keep, false); });
    cards.append(el); paintCard(i); thumbObserver.observe(el);
  });
  applyFilter(); totals(r.totals);
  if (S.sel < 0 || S.sel >= r.moments.length) S.sel = -1;
  const first = S.visible[0];
  if (S.sel < 0 && first !== undefined) select(first); else if (S.sel >= 0) select(S.sel, false);
}
async function loadThumb(el) {
  const m = S.review.moments[el.dataset.i];
  const r = await S.api.thumbnail(m.id);
  const thumb = $('.thumb', el); thumb.classList.remove('loading');
  if (r.url) { thumb.style.backgroundImage = `url("${r.url}")`; m.thumb = r.url; }
}
function paintCard(i) {
  const m = S.review.moments[i], el = $(`.card[data-i="${i}"]`); if (!el) return;
  el.classList.toggle('cut', !m.keep);
  $('.mark use', el).setAttribute('href', m.keep ? '#i-check' : '#i-x');
  $('.len', el).textContent = clock(m.end - m.start).replace(/\.\d$/, '');
}
function applyFilter() {
  S.visible = [];
  S.review.moments.forEach((m, i) => {
    const show = S.filter === 'all' || (S.filter === 'kept') === m.keep;
    $(`.card[data-i="${i}"]`).hidden = !show; if (show) S.visible.push(i);
  });
}
onSeg($('#filter'), (v) => { S.filter = v; applyFilter(); });
function totals(t) {
  animateNumber($('#dKept'), t.kept); animateNumber($('#dMin'), t.minutes, (v) => v.toFixed(1));
  $('#dParts').textContent = S.review.run.mode === 'parts' && t.parts ? ` · ${t.parts} part${t.parts > 1 ? 's' : ''}` : '';
  $('#renderBtn').disabled = !t.kept;
  $('#renderBtn').textContent = t.kept ? `Render ${t.kept} moments` : 'Render';
}

async function select(i, play = true) {
  if (i === undefined || i < 0) return;
  S.sel = i;
  $$('.card.sel').forEach((c) => c.classList.remove('sel'));
  const card = $(`.card[data-i="${i}"]`); card?.classList.add('sel');
  card?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  const m = S.review.moments[i];
  $('#pScore').className = `score s${m.score}`; $('#pScore').textContent = m.score;
  $('#pWhen').textContent = `${when(m.recorded_at)} · ${m.clip}`; $('#pReason').textContent = m.reason;
  paintTrim(m); playerButtons(m);
  if (!play) return;
  $('#videoLoad').classList.add('on'); video.pause();
  video.removeAttribute('src'); video.poster = m.thumb || ''; video.load();
  const r = await S.api.preview(m.id);
  if (S.sel !== i) return;  // The user moved on while this preview was being made.
  $('#videoLoad').classList.remove('on');
  if (r.error) return toast(r.error, 'bad');
  video.src = r.url; video.dataset.id = m.id;
  video.onloadedmetadata = () => { video.currentTime = m.start - m.window_start; video.play().catch(() => {}); };
}
function playerButtons(m) {
  $('#pKeep').style.opacity = m.keep ? 1 : .55; $('#pCut').style.opacity = m.keep ? .55 : 1;
}
function paintTrim(m) {
  const span = m.window_end - m.window_start;
  $('#trimSel').style.left = (m.start - m.window_start) / span * 100 + '%';
  $('#trimSel').style.width = (m.end - m.start) / span * 100 + '%';
  $('#tStart').textContent = clock(m.start); $('#tEnd').textContent = clock(m.end); $('#tLen').textContent = clock(m.end - m.start);
}
video.addEventListener('timeupdate', () => {
  const m = S.review?.moments[S.sel]; if (!m || video.dataset.id !== m.id) return;
  const t = video.currentTime + m.window_start, span = m.window_end - m.window_start;
  $('#playhead').style.left = Math.min(100, Math.max(0, (t - m.window_start) / span * 100)) + '%';
  if (t >= m.end && !video.paused) video.currentTime = m.start - m.window_start;  // Loop the kept span.
});

async function decide(i, keep, advance = true) {
  const m = S.review.moments[i];
  const r = await call('set_decision', m.id, keep, null, null);
  S.review.moments[i] = { ...m, ...r.moment }; S.review.totals = r.totals;
  paintCard(i); totals(r.totals); if (i === S.sel) playerButtons(r.moment);
  if (S.filter !== 'all') applyFilter();
  if (advance) step(1);
}
async function trim(start, end) {
  const i = S.sel, m = S.review.moments[i];
  const r = await call('set_decision', m.id, null, start, end);
  S.review.moments[i] = { ...m, ...r.moment }; S.review.totals = r.totals;
  paintTrim(r.moment); paintCard(i); totals(r.totals);
}
function step(d) {
  const pos = S.visible.indexOf(S.sel);
  const next = S.visible[Math.min(S.visible.length - 1, Math.max(0, pos + d))];
  if (next !== undefined && next !== S.sel) select(next);
}
$('#pKeep').addEventListener('click', () => decide(S.sel, true));
$('#pCut').addEventListener('click', () => decide(S.sel, false));
$('#keepAll').addEventListener('click', async () => { S.review = await call('set_all', true, null); drawCards(); });
$('#cutAll').addEventListener('click', async () => { S.review = await call('set_all', false, null); drawCards(); });

// Trim handles: drag either edge inside the preview window; click the track to seek.
(() => {
  const track = $('.trim-track'), sel = $('#trimSel');
  const timeAt = (x) => {
    const m = S.review.moments[S.sel], rect = track.getBoundingClientRect();
    return m.window_start + Math.min(1, Math.max(0, (x - rect.left) / rect.width)) * (m.window_end - m.window_start);
  };
  track.addEventListener('pointerdown', (e) => {
    if (!S.review || S.sel < 0) return;
    const m = S.review.moments[S.sel], handle = e.target.dataset.h;
    if (!handle) { video.currentTime = timeAt(e.clientX) - m.window_start; return; }
    track.setPointerCapture(e.pointerId); sel.classList.add('dragging');
    const live = { start: m.start, end: m.end };
    const move = (ev) => {
      const t = Math.round(timeAt(ev.clientX) * 10) / 10;
      if (handle === 'start') live.start = Math.min(t, live.end - 1); else live.end = Math.max(t, live.start + 1);
      paintTrim({ ...m, ...live });
      video.currentTime = (handle === 'start' ? live.start : Math.max(live.start, live.end - 1.5)) - m.window_start;
    };
    const up = () => {
      track.removeEventListener('pointermove', move); track.removeEventListener('pointerup', up);
      sel.classList.remove('dragging');
      trim(live.start, live.end).then(() => { video.currentTime = S.review.moments[S.sel].start - m.window_start; video.play().catch(() => {}); });
    };
    track.addEventListener('pointermove', move); track.addEventListener('pointerup', up);
  });
})();

document.addEventListener('keydown', (e) => {
  if (S.screen !== 'review' || e.target.matches('input') || !$('#modal').hidden || S.sel < 0) return;
  const m = S.review.moments[S.sel], at = video.currentTime + m.window_start;
  const keys = {
    ArrowRight: () => step(1), ArrowDown: () => step(1), ArrowLeft: () => step(-1), ArrowUp: () => step(-1),
    k: () => decide(S.sel, true), x: () => decide(S.sel, false),
    ' ': () => (video.paused ? video.play() : video.pause()),
    '[': () => trim(at, null), ']': () => trim(null, at),
  };
  const fn = keys[e.key.length === 1 ? e.key.toLowerCase() : e.key];
  if (fn) { e.preventDefault(); fn(); }
});

$('#renderBtn').addEventListener('click', async () => {
  video.pause();
  await call('start_render'); go('render');
});

// ---------- render ----------
function renderStart(e) {
  const plan = e.plan || [{ part: 1, minutes: S.review?.totals.minutes, moments: S.review?.totals.kept }];
  $('#renderTitle').textContent = 'Rendering'; $('#renderHint').textContent = '';
  $('#renderSub').textContent = 'Cutting from the original full-quality clips. You can keep using your PC.';
  $('#parts').innerHTML = plan.map((p) => `<li data-part="${p.part}"><span>${plan.length > 1 ? `Part ${p.part}` : 'Your video'}${p.from ? ` · ${esc(p.from)} to ${esc(p.to)}` : ''} · ${p.moments} moments · ${p.minutes} min</span><span class="pct">waiting</span><div class="bar"><i></i></div></li>`).join('');
  $('#cancelRender').hidden = false; $('#cancelRender').disabled = false; $('#openFolder').hidden = true; $('#backToReview').hidden = true;
  if (S.screen !== 'render') go('render');
}
function renderProgress(e) {
  const li = $(`#parts li[data-part="${e.part}"]`); if (!li) return;
  const k = e.done / e.total;
  $('.bar i', li).style.width = k * 96 + '%';  // The last few percent is joining the pieces.
  $('.pct', li).textContent = k < 1 ? `${Math.round(k * 100)}%` : 'joining';
}
function partDone(e) {
  const li = $(`#parts li[data-part="${e.part}"]`); if (!li) return;
  li.classList.add('done'); $('.bar i', li).style.width = '100%';
  $('.pct', li).innerHTML = '<button class="btn ghost sm play"><svg class="ic"><use href="#i-play"/></svg>Play</button>';
  $('.play', li).addEventListener('click', () => call('open_path', e.file));
}
function renderDone(e) {
  if (e.files?.length === 1 && !$('#parts li.done')) partDone({ part: 1, file: e.files[0] });
  $('#renderTitle').textContent = 'Done';
  $('#renderSub').textContent = `${e.files.length} video${e.files.length > 1 ? 's' : ''} saved. A DaVinci Resolve timeline (Combined.fcpxml) is in each render folder.`;
  $('#cancelRender').hidden = true; $('#openFolder').hidden = false; $('#backToReview').hidden = false;
  $('#openFolder').onclick = () => call('open_path', S.state.run?.folder || e.files[0]);
}
function renderStopped(text) {
  $('#renderTitle').textContent = 'Stopped'; $('#renderHint').textContent = text;
  $('#cancelRender').hidden = true; $('#backToReview').hidden = false;
}
$('#cancelRender').addEventListener('click', async () => { await S.api.cancel(); $('#cancelRender').disabled = true; });
$('#backToReview').addEventListener('click', () => go('review'));

// ---------- settings ----------
onEnter.settings = async () => {
  const s = S.state.settings;
  $('#keyMasked').textContent = S.state.key.present ? S.state.key.masked : 'No key saved';
  $('#keyRemove').disabled = $('#keyTest').disabled = !S.state.key.present;
  seg($('#height'), s.height); seg($('#fps'), s.fps);
  $('#loudnorm').checked = s.loudnorm; $('#timestamp').checked = s.timestamp_overlay;
  $('#workers').value = s.workers; $('#workersOut').textContent = s.workers; fillRange($('#workers'));
  setFolder($('#cachePick'), S.state.cache.folder);
  $('#about').textContent = `${S.state.app} ${S.state.version}`;
  const st = await S.api.storage();
  if (!st.error) $('#storageInfo').textContent = `${num(st.cache_files)} clips judged and saved · previews use ${st.previews_mb} MB`;
};
$('#keySave').addEventListener('click', () => saveKey($('#keyInput'), $('#keyState'), () => setTimeout(onEnter.settings, 50)));
$('#keyInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#keySave').click(); });
$('#keyTest').addEventListener('click', async () => {
  keyState($('#keyState'), 'wait', 'Checking with Google...');
  const r = await S.api.test_key(); keyState($('#keyState'), r.ok ? 'ok' : 'bad', r.message || r.error);
});
$('#keyRemove').addEventListener('click', async () => {
  if (!await confirmBox('Remove API key?', 'Judging new clips will need a key again. Saved judgments stay.', 'Remove')) return;
  await call('clear_key'); await refreshState(); keyState($('#keyState'), '', ''); onEnter.settings();
});
onSeg($('#height'), (v) => saveSettings({ height: Number(v) }));
onSeg($('#fps'), (v) => saveSettings({ fps: Number(v) }));
$('#loudnorm').addEventListener('change', (e) => saveSettings({ loudnorm: e.target.checked }));
$('#timestamp').addEventListener('change', (e) => saveSettings({ timestamp_overlay: e.target.checked }));
$('#workers').addEventListener('input', (e) => { $('#workersOut').textContent = e.target.value; fillRange(e.target); });
$('#workers').addEventListener('change', (e) => saveSettings({ workers: Number(e.target.value) }));
$('#cachePick').addEventListener('click', async () => {
  const p = await pickFolder(S.state.cache.folder); if (!p) return;
  await saveSettings({ cache_dir: p }); await refreshState(); S.scan = null; onEnter.settings();
});
$('#clearPreviews').addEventListener('click', async () => { await call('clear_previews'); toast('Preview cache cleared.'); onEnter.settings(); });
$('#openLogs').addEventListener('click', async () => { const st = await call('storage'); call('open_path', st.logs); });
window.addEventListener('resize', debounce(() => $$('.screen.on .seg').forEach((el) => seg(el)), 100));

// ---------- boot ----------
async function boot() {
  S.api = window.pywebview.api;
  S.state = await S.api.get_state();
  if (S.state.error) { document.body.textContent = S.state.error; return; }
  $('.nav[data-go="review"]').disabled = !S.state.run;
  setBusy(S.state.busy);
  go(S.state.settings.onboarded ? 'home' : 'welcome');
  if (!S.state.ffmpeg) toast('FFmpeg is missing, so clips cannot be read. Reinstall the app.', 'bad');
  poll();
  checkWaiting(); setInterval(checkWaiting, 120000);
}
if (window.pywebview?.api) boot(); else window.addEventListener('pywebviewready', boot, { once: true });

// ---------- batch ----------
function isBatchRun(run) { return !!run && ['batch_waiting', 'batch_uploading'].includes(run.status) || (run?.judge_mode === 'batch' && run?.status === 'cancelled' && !run?.moments); }
const BATCH_STATE = {
  JOB_STATE_PENDING: 'Queued at Google', JOB_STATE_RUNNING: 'Google is judging your clips',
  JOB_STATE_SUCCEEDED: 'Finished', JOB_STATE_FAILED: 'Google reported a failure', JOB_STATE_EXPIRED: 'Expired after 48 hours',
  JOB_STATE_CANCELLED: 'Cancelled',
};
const shortTime = (iso) => iso ? new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '-';
function steps(i) { $$('#bSteps li').forEach((li, j) => { li.classList.toggle('done', j < i); li.classList.toggle('on', j === i); }); }
function batchState(text, pulse = '') { $('#bState').textContent = text; $('#bPulse').className = `pulse ${pulse}`; }

onEnter.batch = () => paintBatch();
function paintBatch() {
  if (S.screen !== 'batch') return;
  const run = S.state.run; if (!run) return go('home');
  const b = run.batch || {};
  $('#bClips').textContent = b.clips != null ? num(b.clips) : '-';
  $('#bCost').textContent = b.usd != null ? money(b.usd) : b.estimate_usd != null ? '~' + money(b.estimate_usd) : '-';
  $('#bCost').nextElementSibling.textContent = b.usd != null ? 'charged' : 'estimated';
  $('#bSubmitted').textContent = shortTime(b.submitted); $('#bExpires').textContent = shortTime(b.expires);
  const uploading = run.status === 'batch_uploading', waiting = run.status === 'batch_waiting', cancelled = run.status === 'cancelled';
  $('#bBar').hidden = !uploading;
  $('#bCheck').hidden = !waiting; $('#bCancel').hidden = !(waiting || (uploading && S.busy === 'batch'));
  $('#bResume').hidden = !((uploading && S.busy !== 'batch') || (cancelled && !b.job));
  if (uploading) {
    steps(0);
    if (S.busy === 'batch') batchState('Uploading clips', '');
    else { batchState('Upload was interrupted', 'idle'); $('#bHint').textContent = 'Uploads already made are reused when you resume.'; }
  } else if (waiting) {
    steps(1); batchState(BATCH_STATE[b.status] || 'Waiting on Google', '');
    $('#bHint').textContent = `You can close the app or turn off the PC. It checks automatically every 2 minutes while open, and when you open it again.${b.checked ? ' Last checked ' + shortTime(b.checked) + '.' : ''}`;
  } else if (cancelled) {
    steps(-1); batchState('Cancelled', 'idle'); $('#bHint').textContent = 'Uploads were deleted from Google. Nothing unfinished was charged.';
  }
}
function batchUpload(e) {
  if (S.screen !== 'batch') { if (S.state.run) S.state.run.status = 'batch_uploading'; go('batch'); }
  $('#bResume').hidden = true; $('#bCancel').hidden = false; $('#bCheck').hidden = true; steps(0);
  $('#bBar').hidden = false; $('#bBar i').style.width = (e.total ? e.done / e.total * 100 : 0) + '%';
  batchState(`Uploading clips · ${num(e.done)} of ${num(e.total)}`);
  $('#bClips').textContent = num(e.total);
  $('#bHint').textContent = e.capped ? `${num(e.capped)} older clips are left out to stay within your budget cap.` : 'Keep the PC on until the upload finishes.';
}
function batchCollecting() {
  S.collecting = true;
  if (S.screen === 'batch') { steps(2); batchState('Collecting results', 'good'); $('#bBar').hidden = true; $('#bCheck').hidden = true; $('#bCancel').hidden = true; }
}
async function checkWaiting() {
  const waiting = (S.state?.recent || []).filter((r) => r.status === 'batch_waiting');
  for (const r of waiting) {
    const res = await S.api.check_batch(r.folder);
    if (res.collecting) { toast('A batch finished at Google. Collecting results...'); batchCollecting(); }
    else if (res.done && S.busy) toast('A batch finished; results will be collected when the current job ends.');
  }
  if (waiting.length) { await refreshState(); paintBatch(); }
}
$('#bCheck').addEventListener('click', async () => {
  $('#bCheck').disabled = true;
  const r = await call('check_batch', S.state.run.folder).finally(() => { $('#bCheck').disabled = false; });
  if (r.collecting) batchCollecting(); else { await refreshState(); paintBatch(); toast(BATCH_STATE[r.status] || r.status); }
});
$('#bCancel').addEventListener('click', async () => {
  if (!await confirmBox('Cancel this batch?', 'Uploads are deleted from Google. Clips Google already finished are kept; nothing unfinished is charged.', 'Cancel batch')) return;
  await call('cancel_batch', S.state.run.folder); await refreshState(); paintBatch();
});
$('#bResume').addEventListener('click', async () => { await call('resume_batch', S.state.run.folder); await refreshState(); paintBatch(); });
