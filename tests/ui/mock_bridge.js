/* A fake window.pywebview.api for viewing the UI in a plain browser (screenshots, layout work).
   Inject before app.js loads (e.g. Playwright addInitScript). window.__MOCK options:
   { onboarded: true, key: true, hold: 23 }  // hold: stop the fake judge after N of 60 clips. */
(() => {
  const opt = Object.assign({ onboarded: true, key: true, hold: null }, window.__MOCK || {});
  const events = []; let seq = 0, busy = null;
  const emit = (e) => events.push({ ...e, seq: ++seq, at: Date.now() / 1000 });
  const settings = { version: 1, onboarded: opt.onboarded, source: opt.onboarded ? 'D:\\Videos\\OBS Clips' : '',
    output: opt.onboarded ? 'D:\\Videos\\Highlights' : '', cache_dir: '', mode: 'parts', judge_mode: opt.judgeMode || 'live', target_minutes: 30,
    part_minutes: 30, min_score: 7, budget_usd: 5, workers: 4, height: 1080, fps: 60, loudnorm: true,
    timestamp_overlay: false, model: 'gemini-flash-latest' };
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
      window_start: Math.max(0, start - 3), window_end: Math.min(30, start + len + 3) };
  });
  const decided = {};
  const view = (m) => ({ ...m, ...(decided[m.id] || {}) });
  const totals = () => { const k = moments.map(view).filter((m) => m.keep); const s = k.reduce((a, m) => a + m.end - m.start, 0);
    return { kept: k.length, total: moments.length, minutes: +(s / 60).toFixed(1), parts: Math.max(1, Math.ceil(s / 1800)) }; };
  const run = { created: '2026-10-13T14:05:00', status: 'review', mode: 'parts', min_score: 7, target_minutes: 30, part_minutes: 30,
    usage: { usd: 4.12 }, stopped: null, files: [], folder: 'D:\\Videos\\Highlights\\Compilation 2026-10-13 14-05-00', moments: moments.length };
  const hues = [262, 330, 200, 160, 20, 290];
  const thumb = (i) => 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${hues[i % 6]},55%,32%)"/><stop offset="1" stop-color="hsl(${(hues[i % 6] + 60) % 360},45%,14%)"/></linearGradient></defs><rect width="480" height="270" fill="url(#g)"/><circle cx="${120 + (i * 53) % 260}" cy="${90 + (i * 31) % 100}" r="46" fill="rgba(255,255,255,.08)"/><rect x="0" y="200" width="480" height="70" fill="rgba(0,0,0,.25)"/></svg>`);
  const later = (ms, fn) => setTimeout(fn, ms);

  const api = {
    get_state: async () => ({ app: 'Auto Clip Compiler', version: '1.0.0.dev0', settings, key: { present: opt.key, masked: opt.key ? '\u2022\u2022\u2022\u2022k3Qw' : '' },
      ffmpeg: true, busy, run: opt.onboarded ? run : null, cache: { folder: 'C:\\Users\\you\\AppData\\Local\\AutoClipCompiler\\gemini-cache' },
      recent: opt.onboarded ? [{ folder: run.folder, created: run.created, status: 'review', moments: 34, mode: 'parts', files: [] },
        { folder: 'x', created: '2026-10-12T15:05:00', status: 'rendered', moments: 128, mode: 'reel', files: ['a'] }] : [] }),
    events: async (since) => ({ events: events.filter((e) => e.seq > since), busy }),
    save_settings: async (c) => { Object.assign(settings, c); return { settings }; },
    pick_folder: async () => ({ path: 'D:\\Videos\\OBS Clips' }),
    set_key: async (k) => { opt.key = true; return { ok: true, saved: true, state: 'ok', masked: '\u2022\u2022\u2022\u2022' + k.slice(-4) }; },
    test_key: async () => ({ ok: true, state: 'ok', message: 'Key works.' }),
    clear_key: async () => { opt.key = false; return { ok: true }; },
    open_url: async () => ({ ok: true }), open_path: async () => ({ ok: true }), cancel: async () => ({ ok: true }),
    storage: async () => ({ cache_files: 2687, previews_mb: 182.4, logs: 'C:\\logs' }), clear_previews: async () => ({ ok: true }),
    scan: async () => {
      busy = 'scan'; let d = 0;
      const tick = () => { d = Math.min(2687, d + 400); emit({ type: 'scan', done: d, total: 2687 });
        if (d < 2687) return later(60, tick);
        emit({ type: 'scan_done', clips: 2687, cached: 2397, to_judge: 290, unreadable: 0, gb: 61.4, first: 1779660000, last: 1791200000, estimate_usd: 1.31, estimate_batch_usd: 0.66, batch_per_clip: 0.0022875 });
        busy = null; emit({ type: 'idle' }); };
      later(30, tick); return { ok: true, job: 'scan' };
    },
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
        emit({ type: 'clip', name: `Replay 2026-10-11 2${n % 4}-${String(10 + n).padStart(2, '0')}-41.mp4`, status: n === 7 ? 'failed' : cached ? 'cached' : 'judged',
          error: n === 7 ? 'Gemini 503: model overloaded (after 7 retries)' : undefined, scores: n === 7 ? [] : scores,
          reasons: scores.filter((s) => s >= 7).map((_, j) => reasons[(n + j) % reasons.length]), done: n, total: 60, usd: +usd.toFixed(4) });
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
