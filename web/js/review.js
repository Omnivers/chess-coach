// #/review/:id — full disclosure. The no-names rule is a live-play rule
// only; every field the API returns here (best_uci, eval, pv, explanations)
// is fair to render.

import { api } from './api.js';
import { createBoard } from './board.js';
import { createScrubber } from './scrubber.js';
import { severityClass, prettyHighlight, naOr, uciToSquares } from './util.js';
import { makeT } from './i18n.js';
import { strings } from './strings/review.js';

const t = makeT(strings);

let board = null;
let scrubber = null;
let els = {};
let data = null;
let ply = 0;

function plyOf(obj, index) {
  return obj.ply ?? obj.move_ply ?? obj.index ?? index;
}
function uciOf(move) {
  return move.uci ?? move.move_uci ?? null;
}
function sanOf(move) {
  return move.san ?? move.move_san ?? '?';
}

export function mount(root, params) {
  root.innerHTML = `
    <div class="review-layout">
      <section class="board-col" aria-label="${t('positionAria')}">
        <div class="board-frame"><div id="board-wrap"><div id="review-board" class="board-surface"></div></div><div class="board-ranks" aria-hidden="true"></div><div class="board-files" aria-hidden="true"></div></div>
        <div class="review-controls">
          <button id="rev-first" title="${t('firstTitle')}" aria-label="${t('firstTitle')}">⏮</button>
          <button id="rev-prev" title="${t('prevTitle')}" aria-label="${t('prevTitle')}">◀</button>
          <button id="rev-next" title="${t('nextTitle')}" aria-label="${t('nextTitle')}">▶</button>
          <button id="rev-last" title="${t('lastTitle')}" aria-label="${t('lastTitle')}">⏭</button>
        </div>
        <a class="pgn-link" id="pgn-link" target="_blank" rel="noopener">${t('downloadPgn')}</a>
      </section>
      <section class="stack" aria-label="${t('reviewAria')}">
        <div class="panel stack">
          <h2>${t('accuracyHeading')}</h2>
          <div class="review-stats" id="review-stats"></div>
        </div>
        <div class="panel stack" id="scrubber-wrap" aria-label="${t('timelineAria')}"></div>
        <div class="panel stack">
          <h2>${t('movesHeading')}</h2>
          <div class="move-list" id="move-list"></div>
        </div>
        <div class="panel stack" aria-labelledby="review-summary-h">
          <h2 id="review-summary-h">${t('coachTakeHeading')}</h2>
          <div class="review-summary" id="review-summary">${t('loading')}</div>
        </div>
        <div class="panel stack">
          <h2>${t('wentWellHeading')}</h2>
          <div class="review-list-block" id="highlight-list"></div>
        </div>
        <div class="panel stack">
          <h2>${t('secondLookHeading')}</h2>
          <div class="review-list-block" id="mistake-list"></div>
        </div>
      </section>
    </div>
  `;
  els = {
    stats: root.querySelector('#review-stats'),
    moveList: root.querySelector('#move-list'),
    summary: root.querySelector('#review-summary'),
    highlights: root.querySelector('#highlight-list'),
    mistakes: root.querySelector('#mistake-list'),
    pgnLink: root.querySelector('#pgn-link'),
  };
  board = createBoard(root.querySelector('#review-board'), {});
  scrubber = createScrubber(root.querySelector('#scrubber-wrap'), { onSeek: goToPly });

  root.querySelector('#rev-first').addEventListener('click', () => goToPly(0));
  root.querySelector('#rev-prev').addEventListener('click', () => goToPly(ply - 1));
  root.querySelector('#rev-next').addEventListener('click', () => goToPly(ply + 1));
  root.querySelector('#rev-last').addEventListener('click', () => goToPly((data?.moves.length || 1) - 1));

  load(params.id);
}

export function unmount() {
  board?.destroy();
  scrubber?.destroy();
  board = null;
  scrubber = null;
  data = null;
}

async function load(externalId) {
  els.summary.textContent = t('loading');
  els.pgnLink.href = `/review/${externalId}/pgn`;
  try {
    data = await api.get(`/review/${externalId}`);
  } catch (err) {
    els.summary.textContent = t('loadError', { message: err.message });
    return;
  }
  renderStats();
  renderMoveList();
  renderList(els.highlights, data.highlights || [], 'highlight');
  renderList(els.mistakes, data.mistakes || [], 'mistake');
  scrubber.render(data);
  goToPly(0);
  loadSummary(externalId);
}

async function loadSummary(externalId) {
  try {
    const summary = await api.get(`/review/${externalId}/summary`);
    els.summary.textContent = '';
    for (const para of String(summary.text || '').split(/\n{2,}/)) {
      const p = document.createElement('p');
      p.textContent = para;
      els.summary.appendChild(p);
    }
    if (!els.summary.childElementCount) els.summary.textContent = t('noSummary');
  } catch (err) {
    els.summary.textContent = t('summaryError', { message: err.message });
  }
}

function renderStats() {
  els.stats.innerHTML = '';
  const rows = [
    [t('accuracyLabel'), naOr(data.accuracy, (v) => `${v.toFixed(1)}%`)],
    [t('acplLabel'), naOr(data.acpl, (v) => v.toFixed(0))],
  ];
  for (const [label, value] of rows) {
    const stat = document.createElement('div');
    stat.className = 'stat';
    const v = document.createElement('span');
    v.className = 'value';
    v.textContent = value;
    const l = document.createElement('span');
    l.className = 'label';
    l.textContent = label;
    stat.append(v, l);
    els.stats.appendChild(stat);
  }
}

function renderMoveList() {
  els.moveList.innerHTML = '';
  const moves = data.moves || [];
  for (let i = 0; i < moves.length; i += 2) {
    const row = document.createElement('div');
    row.className = 'row';
    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = `${i / 2 + 1}.`;
    row.append(num, plyCell(moves, i), plyCell(moves, i + 1));
    els.moveList.appendChild(row);
  }
}

function plyCell(moves, index) {
  const cell = document.createElement('span');
  cell.className = 'ply';
  const move = moves[index];
  if (!move) return cell;
  cell.classList.add('clickable');
  cell.dataset.ply = String(index);
  if (index === ply) cell.classList.add('current');
  const mistake = (data.mistakes || []).find((m, i) => plyOf(m, i) === index);
  if (mistake) {
    const dot = document.createElement('span');
    dot.className = `eval-badge ${severityClass(mistake.severity || 'inaccuracy')}`;
    cell.appendChild(dot);
  }
  cell.appendChild(document.createTextNode(sanOf(move)));
  cell.addEventListener('click', () => goToPly(index));
  return cell;
}

function renderList(container, items, kind) {
  container.innerHTML = '';
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = kind === 'highlight' ? t('nothingFlagged') : t('noMistakes');
    container.appendChild(empty);
    return;
  }
  items.forEach((item, i) => {
    const index = plyOf(item, i);
    const move = (data.moves || [])[index];
    const el = document.createElement('div');
    el.className = kind === 'highlight' ? 'highlight-item' : `mistake-item severity-${item.severity || 'inaccuracy'}`;
    const head = document.createElement('div');
    head.className = 'item-head';
    const san = document.createElement('span');
    san.className = 'san';
    san.textContent = move ? sanOf(move) : t('plyLabel', { n: index });
    const tag = document.createElement('span');
    tag.className = `badge ${kind === 'highlight' ? 'good' : severityClass(item.severity || 'inaccuracy')}`;
    tag.textContent = prettyHighlight(item.motif || item.kind || item.severity || kind);
    head.append(san, tag);
    const detail = document.createElement('div');
    detail.className = 'item-detail';
    const explanation = item.explanation || item.text || null;
    if (explanation) {
      const p = document.createElement('p');
      p.textContent = explanation;
      detail.appendChild(p);
    }
    const bestUci = item.best_uci || item.best_move;
    if (bestUci) {
      const pv = document.createElement('div');
      pv.className = 'pv';
      pv.textContent = `${t('betterLabel')} ${bestUci}${item.pv ? ' ' + item.pv.join(' ') : ''}`;
      detail.appendChild(pv);
    }
    if (!detail.childElementCount) {
      const p = document.createElement('p');
      p.textContent = t('noEngineDetail');
      detail.appendChild(p);
    }
    el.append(head, detail);
    el.addEventListener('click', () => {
      detail.classList.toggle('open');
      goToPly(index);
    });
    container.appendChild(el);
  });
}

function fenAtPly(targetPly) {
  const moves = data.moves || [];
  if (moves[targetPly]?.fen) return moves[targetPly].fen;
  const chess = new Chess();
  for (let i = 0; i <= targetPly; i++) {
    const uci = uciOf(moves[i] || {});
    if (!uci) break;
    const sq = uciToSquares(uci);
    if (!sq) break;
    chess.move({ from: sq.from, to: sq.to, promotion: sq.promotion || 'q' });
  }
  return chess.fen();
}

function goToPly(nextPly) {
  if (!data || !data.moves.length) return;
  ply = Math.max(0, Math.min(data.moves.length - 1, nextPly));
  const fen = fenAtPly(ply);
  const turnColor = fen.split(' ')[1] === 'w' ? 'white' : 'black';
  const move = data.moves[ply];
  const lastMove = move ? (() => { const sq = uciToSquares(uciOf(move)); return sq ? [sq.from, sq.to] : undefined; })() : undefined;
  board.setPosition({ fen, turnColor, movableColor: undefined, dests: new Map(), lastMove });
  scrubber.setPly(ply);
  renderMoveList();
}
