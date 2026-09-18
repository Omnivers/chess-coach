// #/drills — spaced-repetition tactics queue. Scheduling lives server-side
// (SRS); this view only presents positions, submits attempts, and shows the
// result the server hands back.

import { api } from './api.js';
import { createBoard, computeDests, isPromotion } from './board.js';
import { naOr } from './util.js';
import { askPromotion } from './promotion.js';
import { makeT } from './i18n.js';
import { strings } from './strings/drills.js';

const t = makeT(strings);

let board = null;
let queue = [];
let current = null;
let currentChess = null;
let shownAt = 0;
let els = {};

export function mount(root) {
  root.innerHTML = `
    <div class="stack">
      <div class="spread">
        <h1>${t('heading')}</h1>
        <span class="queue-counter" id="queue-counter"></span>
      </div>
      <div class="drill-layout">
        <section class="board-col" aria-label="${t('boardAria')}">
          <div class="drill-side-to-move" id="side-to-move"></div>
          <div id="board-wrap"><div id="drill-board" class="board-surface"></div></div>
          <div id="promotion-picker" hidden></div>
          <div id="drill-feedback"></div>
        </section>
        <aside class="panel stack" aria-label="${t('statsAria')}">
          <h2>${t('statsHeading')}</h2>
          <div class="stat-grid" id="drill-stats"></div>
        </aside>
      </div>
    </div>
  `;
  els = {
    counter: root.querySelector('#queue-counter'),
    sideToMove: root.querySelector('#side-to-move'),
    feedback: root.querySelector('#drill-feedback'),
    stats: root.querySelector('#drill-stats'),
    promotionPicker: root.querySelector('#promotion-picker'),
    boardCol: root.querySelector('.board-col'),
  };
  board = createBoard(root.querySelector('#drill-board'), { onUserMove: handleMove });
  loadStats();
  loadQueue();
}

export function unmount() {
  board?.destroy();
  board = null;
  queue = [];
  current = null;
}

async function loadStats() {
  try {
    const s = await api.get('/drills/stats');
    renderStats(s);
  } catch {
    els.stats.innerHTML = `<p class="muted">${t('statsUnavailable')}</p>`;
  }
}

function renderStats(s) {
  els.stats.innerHTML = '';
  const rows = [
    [t('statDueNow'), String(s.due_now)],
    [t('statDueToday'), String(s.due_today)],
    [t('statTotal'), String(s.total)],
    [t('statRetired'), String(s.retired)],
    [t('statRetention7d'), naOr(s.retention_7d, (v) => `${(v * 100).toFixed(0)}%`)],
  ];
  for (const [label, value] of rows) {
    const tile = document.createElement('div');
    tile.className = 'stat-tile';
    const v = document.createElement('span');
    v.className = value === '—' ? 'value na' : 'value';
    v.textContent = value;
    const l = document.createElement('span');
    l.className = 'label';
    l.textContent = label;
    tile.append(v, l);
    if (value === '—') {
      const cap = document.createElement('span');
      cap.className = 'caption';
      cap.textContent = t('notEnoughGames');
      tile.appendChild(cap);
    }
    els.stats.appendChild(tile);
  }
}

async function loadQueue() {
  els.feedback.innerHTML = `<p class="muted">${t('loading')}</p>`;
  try {
    queue = await api.get('/drills/due?limit=8');
  } catch (err) {
    els.feedback.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'inline-error';
    p.textContent = t('loadError', { message: err.message });
    els.feedback.appendChild(p);
    return;
  }
  els.feedback.innerHTML = '';
  next();
}

function updateCounter() {
  els.counter.textContent = t('queueLeft', { n: current ? queue.length + 1 : 0 });
}

function next() {
  current = queue.shift() || null;
  updateCounter();
  if (!current) {
    showEmptyState();
    return;
  }
  currentChess = new Chess(current.fen);
  const color = current.side_to_move === 'black' ? 'black' : 'white';
  board.setOrientation(color);
  board.setPosition({ fen: current.fen, turnColor: color, movableColor: color, dests: computeDests(currentChess) });
  els.sideToMove.textContent = t('sideToMove', { color, motif: current.motif || '' });
  els.feedback.innerHTML = '';
  shownAt = Date.now();
}

function showEmptyState() {
  els.sideToMove.textContent = '';
  board.setInert();
  els.feedback.innerHTML = `
    <div class="empty-state">
      <div class="glyph">✓</div>
      <p>${t('emptyHeading')}</p>
      <p class="muted">${t('emptyBody')}</p>
    </div>
  `;
}

function revert() {
  const color = current.side_to_move === 'black' ? 'black' : 'white';
  board.setPosition({ fen: current.fen, turnColor: color, movableColor: color, dests: computeDests(currentChess) });
}

async function handleMove(orig, dest) {
  if (!current) return;
  let promotion;
  if (isPromotion(currentChess, orig, dest)) {
    promotion = await askPromotion(els.promotionPicker, currentChess.get(orig).color);
    if (!promotion) { revert(); return; }
  }
  const uci = orig + dest + (promotion || '');
  board.setPosition({ fen: currentChess.fen(), movableColor: undefined, dests: new Map(), turnColor: currentChess.turn() === 'w' ? 'white' : 'black' });
  try {
    const resp = await api.post(`/drills/${current.id}/attempt`, { moved_uci: uci, time_ms: Date.now() - shownAt });
    showFeedback(resp);
  } catch (err) {
    els.feedback.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'inline-error';
    p.textContent = err.message;
    els.feedback.appendChild(p);
    revert();
  }
}

function showFeedback(resp) {
  const wrap = document.createElement('div');
  wrap.className = `drill-feedback ${resp.correct ? 'correct' : 'incorrect'}`;
  const headline = document.createElement('p');
  headline.textContent = resp.correct ? t('correct') : t('incorrect', { solution: resp.solution_uci });
  wrap.appendChild(headline);
  const schedule = document.createElement('p');
  schedule.className = 'schedule';
  schedule.textContent = resp.retired
    ? t('retired')
    : t('nextDue', { interval: naOr(resp.interval_days, (v) => t('daysUnit', { n: v })), reps: resp.reps });
  wrap.appendChild(schedule);
  const nextBtn = document.createElement('button');
  nextBtn.className = 'primary';
  nextBtn.textContent = t('next');
  nextBtn.style.marginTop = 'var(--space-2)';
  nextBtn.addEventListener('click', next);
  els.feedback.innerHTML = '';
  els.feedback.append(wrap, nextBtn);
}

