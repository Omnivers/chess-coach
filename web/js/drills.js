// #/drills — spaced-repetition tactics queue. Scheduling lives server-side
// (SRS); this view only presents positions, submits attempts, and shows the
// result the server hands back.

import { api } from './api.js';
import { createBoard, computeDests, isPromotion } from './board.js';
import { naOr } from './util.js';
import { askPromotion } from './promotion.js';

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
        <h1>Drills</h1>
        <span class="queue-counter" id="queue-counter"></span>
      </div>
      <div class="drill-layout">
        <section class="board-col" aria-label="Drill position">
          <div class="drill-side-to-move" id="side-to-move"></div>
          <div id="board-wrap"><div id="drill-board" class="board-surface"></div></div>
          <div id="promotion-picker" hidden></div>
          <div id="drill-feedback"></div>
        </section>
        <aside class="panel stack" aria-label="Drill stats">
          <h2>This week</h2>
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
    els.stats.innerHTML = '<p class="muted">Stats unavailable.</p>';
  }
}

function renderStats(s) {
  els.stats.innerHTML = '';
  const rows = [
    ['Due now', String(s.due_now)],
    ['Due today', String(s.due_today)],
    ['Total', String(s.total)],
    ['Retired', String(s.retired)],
    ['7-day retention', naOr(s.retention_7d, (v) => `${(v * 100).toFixed(0)}%`)],
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
      cap.textContent = 'not enough games yet';
      tile.appendChild(cap);
    }
    els.stats.appendChild(tile);
  }
}

async function loadQueue() {
  els.feedback.innerHTML = '<p class="muted">Loading…</p>';
  try {
    queue = await api.get('/drills/due?limit=8');
  } catch (err) {
    els.feedback.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'inline-error';
    p.textContent = `Could not load drills: ${err.message}`;
    els.feedback.appendChild(p);
    return;
  }
  els.feedback.innerHTML = '';
  next();
}

function updateCounter() {
  els.counter.textContent = current ? `${queue.length + 1} left` : '0 left';
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
  els.sideToMove.textContent = `${color === 'white' ? 'White' : 'Black'} to move${current.motif ? ' · ' + current.motif : ''}`;
  els.feedback.innerHTML = '';
  shownAt = Date.now();
}

function showEmptyState() {
  els.sideToMove.textContent = '';
  board.setInert();
  els.feedback.innerHTML = `
    <div class="empty-state">
      <div class="glyph">✓</div>
      <p>Nothing due right now.</p>
      <p class="muted">Play a game or come back later — new drills are drawn from your own mistakes.</p>
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
  headline.textContent = resp.correct ? 'Correct.' : `Not quite — the answer was ${resp.solution_uci}.`;
  wrap.appendChild(headline);
  const schedule = document.createElement('p');
  schedule.className = 'schedule';
  schedule.textContent = resp.retired
    ? 'Retired from the rotation — you’ve got this one.'
    : `Next due in ${naOr(resp.interval_days, (v) => `${v}d`)} (rep ${resp.reps}).`;
  wrap.appendChild(schedule);
  const nextBtn = document.createElement('button');
  nextBtn.className = 'primary';
  nextBtn.textContent = 'Next';
  nextBtn.style.marginTop = 'var(--space-2)';
  nextBtn.addEventListener('click', next);
  els.feedback.innerHTML = '';
  els.feedback.append(wrap, nextBtn);
}

