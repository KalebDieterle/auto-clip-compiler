/* A fake window.pywebview.api for viewing the UI in a plain browser (screenshots, layout work).
   Inject before app.js loads (e.g. Playwright addInitScript). window.__MOCK options:
   { onboarded: true, key: true, hold: 23, content: 'both' }
   hold: stop the fake judge after N of 60 clips. content: funny (default), skill or both.
   smallScan: a 26-clip scan for the clip picker (scanSize: N clips instead); picked: N clips pre-chosen. */
(() => {
  const opt = Object.assign({ onboarded: true, key: true, hold: null, content: 'funny' }, window.__MOCK || {});
  const events = []; let seq = 0, busy = null;
  const emit = (e) => events.push({ ...e, seq: ++seq, at: Date.now() / 1000 });
  const settings = { version: 1, onboarded: opt.onboarded, source: opt.onboarded ? 'D:\\Videos\\OBS Clips' : '',
    output: opt.onboarded ? 'D:\\Videos\\Highlights' : '', cache_dir: '', mode: 'parts', judge_mode: opt.judgeMode || 'live', target_minutes: 30,
    part_minutes: 30, min_score: 7, budget_usd: 5, workers: 4, height: 1080, fps: 60, loudnorm: true,
    timestamp_overlay: false, model: 'gemini-flash-latest', content_mode: opt.content, min_skill_score: 7,
    skill_preset: 'balanced', skill_share: 0.4, game_override: 'auto' };
  const plays = [
    { reason: 'Flicks onto a head through the smoke, one tap', tags: ['Headshot', 'Big flick', 'Fast reaction', 'One-tap', 'Through smoke'], bad: [], score: 9 },
    { reason: 'Wins a 1v3 on B site with 8 HP left', tags: ['3K', '1v3', '2 headshots', 'Low HP', 'Decisive'], bad: [], score: 10 },
    { reason: 'Long-range bow headshot on a running player', tags: ['Headshot', 'Long range', 'Fast reaction'], bad: [], score: 8 },
    { reason: 'Holds the compound alone against three raiders', tags: ['2K', 'Outnumbered', 'Headshot'], bad: [], score: 8 },
    { reason: 'Clean AWP no-scope as they peek', tags: ['Headshot', 'No-scope', 'Flick'], bad: [], score: 9 },
    { reason: 'Quick double after the entry, both turned away', tags: ['2K', '2 headshots'], bad: ['Unaware enemy'], score: 7 }];
  const reasons = ['Gets launched off the cliff by his own rocket and screams the whole way down',
    'Tries to sneak past, steps on the one bear trap, everyone loses it', 'Sings "Pony" on a horse right before getting blown up',
    'Confidently calls the wrong compound, walks straight into the enemy base', 'Car flips on the first bump, slow-motion panic',
    'Accidentally locks his friend inside the base and refuses to admit it', 'Perfect impression of the shopkeeper; the timing is unreal'];
  const t0 = 1790000000;
  const moments = Array.from({ length: 34 }, (_, i) => {
    const start = 2 + (i * 7) % 12, len = 6 + (i * 5) % 12;
    return { id: (i + 1).toString(16).padStart(32, 'a'), clip: `Replay 2026-09-${String(1 + (i >> 1)).padStart(2, '0')} 21-${String(10 + i).padStart(2, '0')}-08.mp4`,
      recorded_at: t0 + i * 5400, score: [7, 8, 9, 7, 8, 10, 7][i % 7], reason: reasons[i % reasons.length], keep: i % 9 !== 4,
      start, end: start + len, orig_start: start, orig_end: start + len, duration: 30,
      window_start: Math.max(0, start - 3), window_end: Math.min(30, start + len + 3),
      ...lensFor(i, start, len) };
  });
  function lensFor(i, start, len) {
    const funny = { lenses: ['funny'], lens: 'funny', funny_score: [7, 8, 9, 7, 8, 10, 7][i % 7] };
    if (opt.content === 'funny') return funny;
    const p = plays[i % plays.length];
    const skill = { lenses: ['skill'], lens: 'skill', score: p.score, skill_score: p.score, reason: p.reason, skill_tags: p.tags,
      skill_drawbacks: p.bad, game: i % 3 === 2 ? 'rust' : 'cs2', kills: [start + 1.2, start + len * .6].slice(0, p.tags.some((t) => /\dK/.test(t)) ? 2 : 1) };
    if (opt.content === 'skill' || i % 5 === 1) return i % 10 === 1 && opt.content === 'both'
      ? { ...skill, lenses: ['funny', 'skill'], funny_score: 8, skill_reason: p.reason } : skill;
    return i % 3 === 0 ? { ...skill } : funny;
  }
  const decided = {};
  const view = (m) => ({ ...m, ...(decided[m.id] || {}) });
  const totals = () => { const k = moments.map(view).filter((m) => m.keep); const s = k.reduce((a, m) => a + m.end - m.start, 0);
    return { kept: k.length, total: moments.length, minutes: +(s / 60).toFixed(1), parts: Math.max(1, Math.ceil(s / 1800)) }; };
  const run = { created: '2026-10-13T14:05:00', status: 'review', mode: 'parts', min_score: 7, target_minutes: 30, part_minutes: 30,
    content_mode: opt.content, min_skill_score: 7, skill_share: 0.4,
    usage: { usd: 4.12 }, stopped: null, files: [], folder: 'D:\\Videos\\Highlights\\Compilation 2026-10-13 14-05-00', moments: moments.length };
  const hues = [262, 330, 200, 160, 20, 290];
  const thumb = (i) => 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${hues[i % 6]},55%,32%)"/><stop offset="1" stop-color="hsl(${(hues[i % 6] + 60) % 360},45%,14%)"/></linearGradient></defs><rect width="480" height="270" fill="url(#g)"/><circle cx="${120 + (i * 53) % 260}" cy="${90 + (i * 31) % 100}" r="46" fill="rgba(255,255,255,.08)"/><rect x="0" y="200" width="480" height="70" fill="rgba(0,0,0,.25)"/></svg>`);
  const later = (ms, fn) => setTimeout(fn, ms);
  // A small scan for the clip picker: 3 evenings of replays, some judged already, one waiting in a batch.
  const scanned = Array.from({ length: opt.scanSize || 26 }, (_, i) => {
    const night = Math.floor(i / 9), when = 1791150000 - night * 86400 - (i % 9) * 1260;
    const stamp = new Date(when * 1000), pad = (n) => String(n).padStart(2, '0');
    const name = `Replay ${stamp.getFullYear()}-${pad(stamp.getMonth() + 1)}-${pad(stamp.getDate())} ${pad(stamp.getHours())}-${pad(stamp.getMinutes())}-${pad(stamp.getSeconds())}.mp4`;
    return { path: 'D:\\Videos\\OBS Clips\\' + name, name, when, mb: 38 + (i * 37) % 160, ready: i % 3 === 0, pending: i === 7, unreadable: false };
  });
  let picked = opt.picked ? new Set(scanned.filter((_, i) => i % 4 !== 1).slice(0, opt.picked).map((c) => c.path)) : null;
  const summary = () => {
    const rows = scanned.filter((c) => !picked || picked.has(c.path)), todo = rows.filter((c) => !c.ready && !c.pending).length;
    const est = (per, approx) => ({ clips: todo, ready: rows.filter((c) => c.ready).length, in_batch: rows.filter((c) => c.pending).length,
      live: +(todo * per).toFixed(2), batch: +(todo * per / 2).toFixed(2), approx });
    const estimates = { funny: est(0.0046, false), skill: est(0.012, true), both: est(0.0166, true) }, now = estimates[settings.content_mode];
    return { clips: rows.length, total: scanned.length, picked: !!picked, cached: now.ready, to_judge: now.clips, in_batch: now.in_batch,
      unreadable: 0, gb: +(rows.reduce((a, c) => a + c.mb, 0) / 1000).toFixed(1), first: scanned[scanned.length - 1].when, last: scanned[0].when,
      estimate_usd: now.live, estimate_batch_usd: now.batch, estimates, batch_per_clip: 0.0022875 };
  };

  const api = {
    get_state: async () => ({ app: 'Auto Clip Compiler', version: '1.1.0', settings, key: { present: opt.key, masked: opt.key ? '\u2022\u2022\u2022\u2022k3Qw' : '' },
      ffmpeg: true, busy, run: opt.onboarded ? run : null, cache: { folder: 'C:\\Users\\you\\AppData\\Local\\AutoClipCompiler\\gemini-cache' },
      recent: opt.onboarded ? [{ folder: run.folder, created: run.created, status: 'review', moments: 34, mode: 'parts', files: [], content_mode: opt.content },
        { folder: 'x', created: '2026-10-12T15:05:00', status: 'rendered', moments: 128, mode: 'reel', files: ['a'] }] : [] }),
    events: async (since) => ({ events: events.filter((e) => e.seq > since), busy }),
    history: async () => ({ runs: opt.onboarded ? [{ folder: run.folder, created: run.created, status: 'review', moments: 34, mode: 'parts', files: [], content_mode: opt.content, usd: 0.56 },
      { folder: 'x', created: '2026-10-12T15:05:00', status: 'rendered', moments: 128, mode: 'reel', files: ['a'], usd: 5.8 },
      { folder: 'y', created: '2026-10-09T20:41:00', status: 'rendered', moments: 38, mode: 'reel', files: ['b'], usd: 0.21 }] : [] }),
    save_settings: async (c) => { Object.assign(settings, c); return { settings }; },
    pick_folder: async () => ({ path: 'D:\\Videos\\OBS Clips' }),
    set_key: async (k) => { opt.key = true; return { ok: true, saved: true, state: 'ok', masked: '\u2022\u2022\u2022\u2022' + k.slice(-4) }; },
    test_key: async () => ({ ok: true, state: 'ok', message: 'Key works.' }),
    clear_key: async () => { opt.key = false; return { ok: true }; },
    open_url: async () => ({ ok: true }), open_path: async () => ({ ok: true }), cancel: async () => ({ ok: true }),
    storage: async () => ({ cache_files: 2687, skill_files: opt.content === 'funny' ? 0 : 412, previews_mb: 182.4, logs: 'C:\\logs' }),
    clear_previews: async () => ({ ok: true }), edit_profiles: async () => ({ ok: true }),
    scan: async () => {
      busy = 'scan'; let d = 0;
      const tick = () => { d = Math.min(2687, d + 400); emit({ type: 'scan', done: d, total: 2687 });
        if (d < 2687) return later(60, tick);
        if (opt.smallScan) { emit({ type: 'scan_done', ...summary() }); busy = null; emit({ type: 'idle' }); return; }
        const estimates = { funny: { clips: 290, ready: 2397, in_batch: 0, live: 1.31, batch: 0.66, approx: false },
          skill: { clips: 2275, ready: 412, in_batch: 0, live: 18.9, batch: 9.45, approx: true },
          both: { clips: 2310, ready: 377, in_batch: 0, live: 20.21, batch: 10.11, approx: true } };
        emit({ type: 'scan_done', clips: 2687, total: 2687, picked: false, cached: 2397, to_judge: 290, unreadable: 0, gb: 61.4, first: 1779660000, last: 1791200000, estimate_usd: 1.31, estimate_batch_usd: 0.66, batch_per_clip: 0.0022875, estimates });
        busy = null; emit({ type: 'idle' }); };
      later(30, tick); return { ok: true, job: 'scan' };
    },
    scan_clips: async () => ({ clips: scanned.map((c) => ({ ...c, chosen: !picked || picked.has(c.path) })) }),
    select_clips: async (paths) => { picked = paths === null || paths === undefined || paths.length === scanned.length ? null : new Set(paths); return summary(); },
    open_clip: async () => ({ ok: true }),
    check_batch: async () => { run.batch.status = 'JOB_STATE_RUNNING'; run.batch.checked = '2026-10-13T14:31:00'; return { status: 'JOB_STATE_RUNNING', done: false, batch: run.batch }; },
    cancel_batch: async () => { run.status = 'cancelled'; return { ok: true }; },
    resume_batch: async () => ({ ok: true }),
    start_judge: async () => {
      if (settings.judge_mode === 'batch') {
        busy = 'batch'; Object.assign(run, { status: 'batch_uploading', judge_mode: 'batch', moments: 0, batch: { clips: 290 } }); let d = 0;
        const tick = () => { d = Math.min(290, d + 9); emit({ type: 'batch_upload', done: d, total: 290 });
          if (opt.hold && d >= opt.hold) return;
          if (d < 290) return later(40, tick);
          run.status = 'batch_waiting';
          run.batch = { job: 'batches/abc', clips: 290, estimate_usd: 0.66, submitted: '2026-10-13T14:12:00', expires: '2026-10-15T14:12:00', status: 'JOB_STATE_PENDING' };
          emit({ type: 'batch_waiting', batch: run.batch }); busy = null; emit({ type: 'idle' }); };
        later(30, tick); return { ok: true, job: 'batch' };
      }
      busy = 'judge'; emit({ type: 'judge_start', total: 60 }); let n = 0, usd = 0;
      const tick = () => {
        n++; const cached = n % 4 === 0; if (!cached) usd += 0.0046;
        const scores = n % 5 === 0 ? [] : [[8], [6, 9], [7], [4], [10, 7]][n % 5];
        const lenses = opt.content === 'funny' ? undefined : scores.map((_, j) => (opt.content === 'skill' || (n + j) % 2) ? 'skill' : 'funny');
        emit({ type: 'clip', name: `Replay 2026-10-11 2${n % 4}-${String(10 + n).padStart(2, '0')}-41.mp4`, status: n === 7 ? 'failed' : cached ? 'cached' : 'judged',
          error: n === 7 ? 'Gemini 503: model overloaded (after 7 retries)' : undefined, scores: n === 7 ? [] : scores,
          reasons: scores.filter((s) => s >= 7).map((_, j) => (lenses?.[j] === 'skill' ? plays[(n + j) % plays.length].reason : reasons[(n + j) % reasons.length])),
          lenses, done: n, total: 60, usd: +usd.toFixed(4) });
        if (opt.hold && n >= opt.hold) return;
        if (n < 60) return later(40, tick);
        emit({ type: 'judge_done', stopped: null }); emit({ type: 'review_ready', moments: moments.length, stopped: null }); busy = null; emit({ type: 'idle' });
      };
      later(50, tick); return { ok: true, job: 'judge' };
    },
    get_review: async () => ({ moments: moments.map(view), totals: totals(), run }),
    open_run: async () => api.get_review(),
    set_decision: async (id, keep, start, end) => {
      const d = decided[id] = decided[id] || {}; if (keep !== null && keep !== undefined) d.keep = keep;
      if (start !== null && start !== undefined) d.start = start; if (end !== null && end !== undefined) d.end = end;
      return { moment: view(moments.find((m) => m.id === id)), totals: totals() };
    },
    set_all: async (keep) => { moments.forEach((m) => { (decided[m.id] = decided[m.id] || {}).keep = keep; }); return api.get_review(); },
    thumbnail: async (id) => ({ url: thumb(moments.findIndex((m) => m.id === id)) }),
    preview: async () => ({ url: '' }),
    start_render: async () => {
      busy = 'render';
      const plan = [1, 2].map((p) => ({ part: p, from: p === 1 ? '2026-09-01' : '2026-09-10', to: p === 1 ? '2026-09-09' : '2026-09-17', moments: 16 + p, minutes: 27.4 - p }));
      emit({ type: 'render_start', parts: 2, plan }); let d = 0;
      const tick = () => { d++; emit({ type: 'render', part: 1, parts: 2, done: d, total: 17 });
        if (d < 17) return later(30, tick);
        emit({ type: 'part_done', part: 1, file: 'D:\\x\\Part 01.mp4' });
        emit({ type: 'render', part: 2, parts: 2, done: 7, total: 18 });
        if (opt.hold) return;
        emit({ type: 'part_done', part: 2, file: 'D:\\x\\Part 02.mp4' });
        emit({ type: 'render_done', files: ['a', 'b'] }); busy = null; emit({ type: 'idle' }); };
      later(30, tick); return { ok: true, job: 'render' };
    },
  };
  window.pywebview = { api };
})();
