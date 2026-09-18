// #/play — the live game. Three columns: clock + move list, board, coach
// rail. Owns the chess.js mirror of server state, the guard confirm flow,
// and the promotion picker. The server is the sole authority on legality;
// chess.js here only computes legal destinations for chessground.

import { api, ApiError } from './api.js';
import { getState, setState } from './state.js';
import { createBoard, computeDests, squareShape, arrowShape, isPromotion } from './board.js';
import { ClockTicker } from './clock.js';
import { createRail } from './rail.js';
import { formatClock, uciToSquares } from './util.js';
import { askPromotion } from './promotion.js';

const TIME_CONTROLS = [
  { value: '15+10', label: '15+10 · Rebuild' },
  { value: '10+5', label: '10+5 · Practice' },
  { value: '5+3', label: '5+3 · Test' },
  { value: '3+2', label: '3+2 · Blitz (measures, does not teach)' },
  { value: 'unlimited', label: 'Unlimited' },
];
const ELOS = [1200, 1400, 1500, 1800, 2000, 2400];

let board = null;
let clockTicker = null;
let rail = null;
let chess = null; // chess.js mirror, resynced from server FEN after every reply
let lastSynced = null; // { fen, turnColor, movableColor, dests }
let turnStartedAt = 0;
let pendingUci = null;
let els = {};

export function mount(root) {
  root.innerHTML = '';
  const layout = document.createElement('div');
  layout.className = 'play-layout';
  layout.innerHTML = `
    <section class="info-col stack" aria-label="Clock and move list">
      <div class="panel stack">
        <h2>Opponent</h2>
        <div class="mono" id="clock-opp" style="font-size:1.6rem;">—:—</div>
      </div>
      <div class="panel stack">
        <h2>Moves</h2>
        <div class="move-list" id="move-list"></div>
      </div>
      <div class="panel stack">
        <h2>You</h2>
        <div class="mono" id="clock-user" style="font-size:1.6rem;">—:—</div>
      </div>
      <div class="panel game-controls" id="game-controls"></div>
    </section>
    <section class="board-col" aria-label="Board">
      <div id="board-wrap"><div id="board" class="board-surface"></div></div>
      <div class="board-message" id="board-message" role="status" aria-live="polite"></div>
      <div id="promotion-picker" hidden></div>
      <div id="guard-slot"></div>
      <div id="result-slot"></div>
    </section>
    <aside class="rail-col panel rail" id="rail" aria-label="Coach rail"></aside>
  `;
  root.appendChild(layout);
  els = {
    clockOpp: layout.querySelector('#clock-opp'),
    clockUser: layout.querySelector('#clock-user'),
    moveList: layout.querySelector('#move-list'),
    controls: layout.querySelector('#game-controls'),
    boardMessage: layout.querySelector('#board-message'),
    promotionPicker: layout.querySelector('#promotion-picker'),
    guardSlot: layout.querySelector('#guard-slot'),
    resultSlot: layout.querySelector('#result-slot'),
  };

  board = createBoard(layout.querySelector('#board'), { onUserMove: handleUserMove });
  clockTicker = new ClockTicker(renderClocks);
  rail = createRail(layout.querySelector('#rail'), { onHintResult: handleHintResult });

  renderControls();
  syncSlice({});
  rail.render(getState().play);

  // Resume an in-progress game if one exists in the store already
  // (e.g. returning from another view without a reload).
  if (getState().play.gameId) {
    lastSynced = null;
    renderMoveList();
  }
}

export function unmount() {
  clockTicker?.stop();
  board?.destroy();
  board = null;
  clockTicker = null;
  rail = null;
}

function playSlice() {
  return getState().play;
}

function syncSlice(patch) {
  setState({ play: { ...playSlice(), ...patch } });
}

function renderControls() {
  els.controls.innerHTML = `
    <label class="visually-hidden" for="color-select">Your colour</label>
    <select id="color-select">
      <option value="white">White</option>
      <option value="black">Black</option>
    </select>
    <label class="visually-hidden" for="elo-select">Engine strength</label>
    <select id="elo-select">
      ${ELOS.map((e) => `<option value="${e}" ${e === 1500 ? 'selected' : ''}>${e} Elo</option>`).join('')}
    </select>
    <label class="visually-hidden" for="tc-select">Time control</label>
    <select id="tc-select">
      ${TIME_CONTROLS.map((t) => `<option value="${t.value}" ${t.value === '15+10' ? 'selected' : ''}>${t.label}</option>`).join('')}
    </select>
    <button id="new-game-btn" class="primary">New game</button>
    <button id="resign-btn" disabled>Resign</button>
  `;
  els.controls.querySelector('#new-game-btn').addEventListener('click', newGame);
  els.controls.querySelector('#resign-btn').addEventListener('click', resign);
  els.controls.querySelector('#color-select').addEventListener('change', (e) => {
    board.setOrientation(e.target.value);
  });
}

async function newGame() {
  const userColor = els.controls.querySelector('#color-select').value;
  const engineElo = parseInt(els.controls.querySelector('#elo-select').value, 10);
  const timeControl = els.controls.querySelector('#tc-select').value;
  setMessage('Starting game…');
  els.resultSlot.innerHTML = '';
  clearGuard();
  try {
    const resp = await api.post('/play/new', {
      user_color: userColor,
      engine_elo: engineElo,
      user_rating: null,
      time_control: timeControl,
    });
    chess = new Chess(resp.fen);
    rail.resetScan();
    board.setOrientation(userColor);
    const openingSan = reconstructOpeningSan(resp.fen, resp.ply);
    syncSlice({
      gameId: resp.game_id,
      userColor,
      isUserTurn: resp.is_user_turn,
      terminated: false,
      result: null,
      clock: resp.clock,
      hintCredits: resp.hint_credits,
      moveHistorySan: openingSan ? [openingSan] : [],
      lastFeedback: null,
      guard: null,
    });
    applyPosition(resp.fen, resp.is_user_turn);
    board.setShapes([]);
    startClock(resp.clock);
    turnStartedAt = Date.now();
    els.controls.querySelector('#resign-btn').disabled = false;
    setMessage('');
  } catch (err) {
    setMessage(`Could not start a game: ${err.message}`, true);
  }
  rail.render(playSlice());
  renderMoveList();
}

// /play/new omits the engine's opening SAN when the user plays black — only
// FEN + ply come back. At ply 1 exactly one legal candidate move produces
// this board; find it locally rather than leaving the move list blank.
function reconstructOpeningSan(fen, ply) {
  if (ply !== 1) return null;
  const probe = new Chess();
  const targetBoard = fen.split(' ')[0];
  for (const m of probe.moves({ verbose: true })) {
    const trial = new Chess();
    trial.move(m);
    if (trial.fen().split(' ')[0] === targetBoard) return m.san;
  }
  return null;
}

function applyPosition(fen, isUserTurn, lastMoveSquares) {
  const turnColor = fen.split(' ')[1] === 'w' ? 'white' : 'black';
  const dests = computeDests(chess);
  const movableColor = isUserTurn ? playSlice().userColor : undefined;
  lastSynced = { fen, turnColor, movableColor, dests };
  board.setPosition({ fen, turnColor, movableColor, dests, lastMove: lastMoveSquares, check: chess.in_check?.() });
}

function startClock(clock) {
  if (!clock) {
    clockTicker.stop();
    renderClocks({ white_ms: null, black_ms: null });
    return;
  }
  clockTicker.sync({ white_ms: clock.white_ms, black_ms: clock.black_ms, turn: chess.turn() === 'w' ? 'white' : 'black' });
  clockTicker.start();
}

function renderClocks({ white_ms, black_ms }) {
  const userColor = playSlice().userColor;
  const userMs = userColor === 'white' ? white_ms : black_ms;
  const oppMs = userColor === 'white' ? black_ms : white_ms;
  els.clockUser.textContent = formatClock(userMs);
  els.clockOpp.textContent = formatClock(oppMs);
}

function renderMoveList() {
  const sans = playSlice().moveHistorySan;
  els.moveList.innerHTML = '';
  for (let i = 0; i < sans.length; i += 2) {
    const row = document.createElement('div');
    row.className = 'row';
    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = `${i / 2 + 1}.`;
    row.appendChild(num);
    row.appendChild(plyCell(sans[i]));
    row.appendChild(plyCell(sans[i + 1]));
    els.moveList.appendChild(row);
  }
  els.moveList.scrollTop = els.moveList.scrollHeight;
}

function plyCell(san) {
  const cell = document.createElement('span');
  cell.className = 'ply';
  cell.textContent = san || '';
  return cell;
}

function setMessage(text, isError) {
  els.boardMessage.textContent = text || '';
  els.boardMessage.style.color = isError ? 'var(--bad)' : '';
}

async function handleUserMove(orig, dest) {
  const s = playSlice();
  if (!s.gameId || s.terminated || !s.isUserTurn) {
    revertBoard();
    return;
  }
  let promotion;
  if (isPromotion(chess, orig, dest)) {
    promotion = await askPromotion(els.promotionPicker, chess.get(orig).color);
    if (!promotion) {
      revertBoard();
      return;
    }
  }
  const move = chess.move({ from: orig, to: dest, promotion: promotion || 'q' });
  if (!move) {
    revertBoard();
    return;
  }
  chess.undo(); // server is authoritative; only used chess.js to validate/derive UCI
  const uci = orig + dest + (promotion || '');
  board.setShapes([]);
  await considerGuard(uci);
}

async function considerGuard(uci) {
  const s = playSlice();
  board.setPosition({ ...lastSynced, movableColor: undefined });
  setMessage('Checking…');
  try {
    const guard = await api.post(`/play/${s.gameId}/guard`, { uci });
    if (guard.risky) {
      showGuardConfirm(uci, guard.delta_cp);
      setMessage('');
      return;
    }
  } catch (err) {
    // Guard endpoint unavailable or erroring: fail open rather than stall
    // the game, but surface it so it isn't mistaken for silence.
    setMessage(`Blunder guard unavailable (${err.message}) — sending move.`, true);
  }
  await sendMove(uci);
}

function showGuardConfirm(uci, deltaCp) {
  pendingUci = uci;
  els.guardSlot.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'guard-confirm';
  box.setAttribute('role', 'alert');
  const p = document.createElement('p');
  p.style.margin = '0';
  p.textContent = 'That looks like it drops material. Play it anyway, or take another look?';
  const actions = document.createElement('div');
  actions.className = 'actions';
  const look = document.createElement('button');
  look.className = 'look-again';
  look.textContent = 'Look again';
  look.addEventListener('click', () => {
    clearGuard();
    revertBoard();
  });
  const play = document.createElement('button');
  play.className = 'play-anyway';
  play.textContent = 'Play it';
  play.addEventListener('click', () => {
    clearGuard();
    sendMove(uci);
  });
  actions.append(look, play);
  box.append(p, actions);
  els.guardSlot.appendChild(box);
}

function clearGuard() {
  pendingUci = null;
  els.guardSlot.innerHTML = '';
}

function revertBoard() {
  if (lastSynced) board.setPosition(lastSynced);
  setMessage('');
}

async function sendMove(uci) {
  const s = playSlice();
  const elapsedMs = Date.now() - turnStartedAt;
  syncSlice({ isUserTurn: false });
  setMessage('Opponent thinking…');
  try {
    const resp = await api.post(`/play/${s.gameId}/move`, { uci, elapsed_ms: elapsedMs });
    chess.load(resp.fen);
    const history = [...playSlice().moveHistorySan, resp.user_move_san];
    if (resp.engine_move_san) history.push(resp.engine_move_san);
    const lastMoveUci = resp.engine_move_uci || resp.user_move_uci;
    const lastSquares = uciToSquares(lastMoveUci);
    // eval_cp / eval_mate / feedback.best_uci are intentionally never read
    // here — showing them during a live game would leak the engine's
    // judgement of the position ahead of the hint ladder charging for it.
    syncSlice({
      isUserTurn: resp.is_user_turn,
      terminated: resp.terminated,
      result: resp.result,
      clock: resp.clock,
      moveHistorySan: history,
      lastFeedback: resp.feedback,
    });
    applyPosition(resp.fen, resp.is_user_turn, lastSquares ? [lastSquares.from, lastSquares.to] : undefined);
    startClock(resp.clock);
    turnStartedAt = Date.now();
    setMessage('');
    if (resp.terminated) {
      clockTicker.stop();
      showResult(resp.result);
      els.controls.querySelector('#resign-btn').disabled = true;
    }
    rail.resetScan();
  } catch (err) {
    setMessage(`Move rejected: ${err.message}`, true);
    revertBoard();
    syncSlice({ isUserTurn: true });
  }
  rail.render(playSlice());
  renderMoveList();
}

function handleHintResult(result) {
  syncSlice({ hintCredits: result.credits_left });
  const shapes = (result.squares || []).map((sq) => squareShape(sq, 'hint'));
  if (result.tier === 4 && result.move_uci) {
    const sq = uciToSquares(result.move_uci);
    if (sq) shapes.push(arrowShape(sq.from, sq.to, 'solution'));
  }
  board.setShapes(shapes);
}

const RESULT_LABEL = { win: 'You won', loss: 'You lost', draw: 'Draw', abandoned: 'Abandoned' };

function showResult(result) {
  const banner = document.createElement('div');
  banner.className = RESULT_LABEL[result] ? `result-banner ${result}` : 'result-banner';
  banner.textContent = `${RESULT_LABEL[result] || result} — saved to the journal`;
  els.resultSlot.innerHTML = '';
  els.resultSlot.appendChild(banner);
}

async function resign() {
  const s = playSlice();
  if (!s.gameId || s.terminated) return;
  try {
    const resp = await api.post(`/play/${s.gameId}/resign`, {});
    clockTicker.stop();
    syncSlice({ terminated: true, result: resp.result });
    showResult(resp.result);
    els.controls.querySelector('#resign-btn').disabled = true;
  } catch (err) {
    setMessage(`Resign failed: ${err.message}`, true);
  }
}
