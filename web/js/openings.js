// #/openings — the principles primer (ROADMAP.md §5.3), not a repertoire.
// Static content off `GET /openings/primer`: no engine, no journal write,
// nothing here can go stale. The one interaction that matters is the link
// between a Principle card and the plies that demonstrate it — clicking a
// card highlights every matching ply in the current line's move list, so
// the abstract rule and the concrete moves read as one thing.

import { api } from './api.js';
import { createBoard } from './board.js';
import { makeT } from './i18n.js';
import { strings } from './strings/openings.js';

const t = makeT(strings);

let board = null;
let els = {};
let principles = [];
let lines = [];
let currentLine = null;
let currentPly = 0; // 0 = starting position, before any move
let highlightPrincipleId = null;
let lastMoveByPly = []; // parallel to currentLine.plies; [from, to] or undefined
let onKeydown = null;

export function mount(root) {
  root.innerHTML = `
    <div class="stack">
      <h1>${t('heading')}</h1>
      <p class="openings-intro">
        ${t('intro')}
      </p>
      <div id="openings-error"></div>
      <div id="openings-wrap" hidden>
        <div class="cluster" id="line-picker" role="group" aria-label="${t('chooseLineAria')}"></div>
        <div class="openings-layout" id="openings-layout">
          <section class="board-col" aria-label="${t('boardAria')}">
            <div class="board-frame"><div id="board-wrap"><div id="openings-board" class="board-surface"></div></div><div class="board-ranks" aria-hidden="true"></div><div class="board-files" aria-hidden="true"></div></div>
            <p class="openings-subtitle" id="line-subtitle"></p>
            <div class="panel stack note-panel" aria-live="polite">
              <h2>${t('thisMoveHeading')}</h2>
              <div id="ply-note"></div>
            </div>
          </section>
          <aside class="stack" aria-label="${t('movesAndPrinciplesAria')}">
            <div class="panel stack">
              <h2>${t('theLineHeading')}</h2>
              <ol class="opening-moves" id="opening-moves" aria-label="${t('movesAria')}"></ol>
            </div>
            <div class="panel stack">
              <h2>${t('principlesHeading')}</h2>
              <div class="principle-grid" id="principle-grid"></div>
            </div>
          </aside>
        </div>
      </div>
    </div>
  `;
  els = {
    error: root.querySelector('#openings-error'),
    layout: root.querySelector('#openings-wrap'),
    linePicker: root.querySelector('#line-picker'),
    subtitle: root.querySelector('#line-subtitle'),
    moves: root.querySelector('#opening-moves'),
    principleGrid: root.querySelector('#principle-grid'),
    note: root.querySelector('#ply-note'),
  };
  board = createBoard(root.querySelector('#openings-board'), { orientation: 'white' });

  onKeydown = handleKeydown;
  window.addEventListener('keydown', onKeydown);

  load();
}

export function unmount() {
  if (onKeydown) window.removeEventListener('keydown', onKeydown);
  onKeydown = null;
  board?.destroy();
  board = null;
  principles = [];
  lines = [];
  currentLine = null;
  currentPly = 0;
  highlightPrincipleId = null;
  lastMoveByPly = [];
}

async function load() {
  try {
    const primer = await api.get('/openings/primer');
    principles = primer.principles || [];
    lines = primer.lines || [];
  } catch (err) {
    els.error.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'inline-error';
    p.textContent = t('loadError', { message: err.message });
    els.error.appendChild(p);
    return;
  }
  els.layout.hidden = false;
  renderPrinciples();
  renderLinePicker();
  if (lines.length) selectLine(lines[0].id);
}

function renderPrinciples() {
  els.principleGrid.innerHTML = '';
  for (const principle of principles) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'principle-card';
    card.dataset.principleId = principle.id;
    card.setAttribute('aria-pressed', 'false');
    const title = document.createElement('span');
    title.className = 'principle-title';
    title.textContent = principle.title;
    const body = document.createElement('span');
    body.className = 'principle-body';
    body.textContent = principle.body;
    card.append(title, body);
    card.addEventListener('click', () => togglePrinciple(principle.id));
    els.principleGrid.appendChild(card);
  }
}

function togglePrinciple(id) {
  highlightPrincipleId = highlightPrincipleId === id ? null : id;
  for (const card of els.principleGrid.querySelectorAll('.principle-card')) {
    const active = card.dataset.principleId === highlightPrincipleId;
    card.classList.toggle('active', active);
    card.setAttribute('aria-pressed', String(active));
  }
  renderMoveList();
}

function renderLinePicker() {
  els.linePicker.innerHTML = '';
  for (const line of lines) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = line.title;
    btn.dataset.lineId = line.id;
    btn.setAttribute('aria-pressed', 'false');
    btn.addEventListener('click', () => selectLine(line.id));
    els.linePicker.appendChild(btn);
  }
}

// Replays the line's SAN through chess.js purely to recover from/to squares
// for chessground's lastMove highlight — the API only carries SAN + FEN
// (see openings.py's docstring on why FENs aren't hand-authored either).
// A move that chess.js fails to replay just loses its highlight, nothing else.
function computeLastMoves(line) {
  const chess = new Chess(line.start_fen);
  return line.plies.map((ply) => {
    const mv = chess.move(ply.san);
    return mv ? [mv.from, mv.to] : undefined;
  });
}

function selectLine(lineId) {
  currentLine = lines.find((l) => l.id === lineId) || null;
  if (!currentLine) return;
  lastMoveByPly = computeLastMoves(currentLine);
  currentPly = 0;
  board.setOrientation(currentLine.perspective);
  for (const btn of els.linePicker.querySelectorAll('button')) {
    const active = btn.dataset.lineId === lineId;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  }
  els.subtitle.textContent = currentLine.subtitle;
  renderMoveList();
  goToPly(0);
}

function turnColorFromFen(fen) {
  return fen.split(' ')[1] === 'w' ? 'white' : 'black';
}

function renderMoveList() {
  els.moves.innerHTML = '';
  if (!currentLine) return;

  const start = document.createElement('li');
  start.className = 'opening-move-row start-row';
  const startBtn = document.createElement('button');
  startBtn.type = 'button';
  startBtn.className = 'opening-ply';
  startBtn.textContent = t('startingPosition');
  if (currentPly === 0) startBtn.classList.add('current');
  startBtn.addEventListener('click', () => goToPly(0));
  start.appendChild(startBtn);
  els.moves.appendChild(start);

  const plies = currentLine.plies;
  for (let i = 0; i < plies.length; i += 2) {
    const row = document.createElement('li');
    row.className = 'opening-move-row';
    row.value = i / 2 + 1;
    const num = document.createElement('span');
    num.className = 'opening-move-num';
    num.textContent = `${i / 2 + 1}.`;
    row.appendChild(num);
    row.appendChild(plyButton(plies[i], i + 1));
    if (plies[i + 1]) row.appendChild(plyButton(plies[i + 1], i + 2));
    els.moves.appendChild(row);
  }
}

function plyButton(ply, plyNumber) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'opening-ply';
  btn.textContent = ply.san;
  if (ply.side !== currentLine.perspective) btn.classList.add('opponent');
  if (plyNumber === currentPly) btn.classList.add('current');
  if (highlightPrincipleId && ply.principle_id === highlightPrincipleId) {
    btn.classList.add('principle-lit');
  }
  btn.addEventListener('click', () => goToPly(plyNumber));
  return btn;
}

function renderNote() {
  els.note.innerHTML = '';
  if (!currentLine) return;
  if (currentPly === 0) {
    const p = document.createElement('p');
    p.textContent = t('startingNote');
    els.note.appendChild(p);
    return;
  }
  const ply = currentLine.plies[currentPly - 1];
  if (ply.principle_id) {
    const principle = principles.find((p) => p.id === ply.principle_id);
    if (principle) {
      const label = document.createElement('span');
      label.className = 'note-principle-label';
      label.textContent = principle.title;
      els.note.appendChild(label);
    }
  }
  const p = document.createElement('p');
  p.textContent = ply.note || t('naturalReply');
  els.note.appendChild(p);
}

function goToPly(target) {
  if (!currentLine) return;
  const max = currentLine.plies.length;
  currentPly = Math.max(0, Math.min(max, target));
  const fen = currentPly === 0 ? currentLine.start_fen : currentLine.plies[currentPly - 1].fen;
  const lastMove = currentPly === 0 ? undefined : lastMoveByPly[currentPly - 1];
  board.setPosition({
    fen,
    turnColor: turnColorFromFen(fen),
    movableColor: undefined,
    dests: new Map(),
    lastMove,
  });
  renderMoveList();
  renderNote();
}

function handleKeydown(e) {
  if (!currentLine) return;
  // Don't hijack arrow keys while the user is typing somewhere else on the
  // page (e.g. the chat dock, which persists across routes — see main.js).
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  if (e.key === 'ArrowRight') { goToPly(currentPly + 1); e.preventDefault(); }
  else if (e.key === 'ArrowLeft') { goToPly(currentPly - 1); e.preventDefault(); }
  else if (e.key === 'Home') { goToPly(0); e.preventDefault(); }
  else if (e.key === 'End') { goToPly(currentLine.plies.length); e.preventDefault(); }
}
