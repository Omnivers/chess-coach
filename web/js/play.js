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
import { makeT } from './i18n.js';
import { strings } from './strings/play.js';

const t = makeT(strings);

// `key` names the human-readable half of the label; `prefix` (when present)
// is the literal numeric time-control string that must never be translated.
const TIME_CONTROLS = [
  { value: '15+10', prefix: '15+10 · ', key: 'tcRebuild' },
  { value: '10+5', prefix: '10+5 · ', key: 'tcPractice' },
  { value: '5+3', prefix: '5+3 · ', key: 'tcTest' },
  { value: '3+2', prefix: '3+2 · ', key: 'tcBlitz' },
  { value: 'unlimited', prefix: '', key: 'tcUnlimited' },
];
function tcLabel(tc) {
  return tc.prefix + t(tc.key);
}
// Mirrors `chess_coach/strength.LADDER`. The rungs below 1320 are not
// Stockfish's own calibration — UCI_Elo refuses to go there — so they are
// Skill Level + a depth cap, tuned to be beatable rather than measured.
// Offering a value between two rungs would silently round down and make
// two buttons play identically, so this list must stay in step with the
// server's.
// `key` names the descriptive word after the Elo number; a null key falls
// back to a plain "<value> Elo" label.
const ELOS = [
  { value: 600, key: 'elo600' },
  { value: 800, key: 'elo800' },
  { value: 1000, key: 'elo1000' },
  { value: 1200, key: 'elo1200' },
  { value: 1400, key: null },
  { value: 1600, key: null },
  { value: 1800, key: null },
  { value: 2000, key: null },
  { value: 2400, key: null },
  { value: 2800, key: 'elo2800' },
];
function eloLabel(e) {
  return e.key ? `${e.value} · ${t(e.key)}` : `${e.value} ${t('eloSuffix')}`;
}
const DEFAULT_ELO = 1000;

let board = null;
let clockTicker = null;
let rail = null;
let chess = null; // chess.js mirror, resynced from server FEN after every reply
let lastSynced = null; // { fen, turnColor, movableColor, dests }
let turnStartedAt = 0;
let pausedAt = 0; // wall clock when the pause began, 0 when running
let pendingUci = null;
let els = {};

export function mount(root) {
  root.innerHTML = '';
  const layout = document.createElement('div');
  layout.className = 'play-layout';
  layout.innerHTML = `
    <section class="info-col stack" aria-label="${t('clockAndMoveList')}">
      <div class="panel stack">
        <h2>${t('opponent')}</h2>
        <div class="mono" id="clock-opp" style="font-size:1.6rem;">—:—</div>
      </div>
      <div class="panel stack">
        <h2>${t('moves')}</h2>
        <div class="move-list" id="move-list"></div>
      </div>
      <div class="panel stack">
        <h2>${t('you')}</h2>
        <div class="mono" id="clock-user" style="font-size:1.6rem;">—:—</div>
      </div>
      <div class="panel game-controls" id="game-controls"></div>
    </section>
    <section class="board-col" aria-label="${t('board')}">
      <div class="board-frame"><div id="board-wrap"><div id="board" class="board-surface"></div></div><div class="board-ranks" aria-hidden="true"></div><div class="board-files" aria-hidden="true"></div></div>
      <div class="board-message" id="board-message" role="status" aria-live="polite"></div>
      <div id="promotion-picker" hidden></div>
      <div id="guard-slot"></div>
      <div id="result-slot"></div>
    </section>
    <aside class="rail-col panel feature rail" id="rail" aria-label="${t('coachRail')}"></aside>
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
    boardCol: layout.querySelector('.board-col'),
  };

  board = createBoard(layout.querySelector('#board'), { onUserMove: handleUserMove });
  clockTicker = new ClockTicker(renderClocks);
  rail = createRail(layout.querySelector('#rail'), { onHintResult: handleHintResult });

  renderControls();
  syncSlice({});
  rail.render(getState().play);

  // Resume an in-progress game if one exists in the store already — a route
  // switch, or the language toggle, which remounts whatever view is on.
  // Everything above built a *fresh* board sitting at the start position, so
  // the stored game has to be painted back onto it; without this the view
  // came back mid-game showing the opening position, a dead clock and an
  // enabled Resign button for a game that might already be over.
  const resumed = getState().play;
  if (resumed.gameId) {
    lastSynced = null;
    els.controls.querySelector('#color-select').value = resumed.userColor;
    board.setOrientation(resumed.userColor);
    if (resumed.fen) {
      chess = new Chess(resumed.fen);
      // Paused or finished games are not movable, whoever's turn it is.
      applyPosition(resumed.fen, resumed.isUserTurn && !resumed.paused && !resumed.terminated);
    }
    renderMoveList();
    const asOfNow = clockAsOfNow(resumed.clock);
    if (!resumed.paused && !resumed.terminated) startClock(asOfNow);
    else syncClock(asOfNow);
    if (resumed.terminated && resumed.result) showResult(resumed.result);
    els.controls.querySelector('#resign-btn').disabled = !!resumed.terminated;
    renderPauseButton();
    if (resumed.paused) setMessage(t('pausedMessage'));
  }
}

export function unmount() {
  clockTicker?.stop();
  board?.destroy();
  board = null;
  clockTicker = null;
  rail = null;
  // `pausedAt` deliberately survives an unmount. A pause that started before
  // a route switch has to keep its start time, or resuming afterwards credits
  // back only the part of the pause that happened after the remount and the
  // rest is charged to the player's clock.
}

function playSlice() {
  return getState().play;
}

function syncSlice(patch) {
  setState({ play: { ...playSlice(), ...patch } });
}

function renderControls() {
  els.controls.innerHTML = `
    <label class="visually-hidden" for="color-select">${t('yourColour')}</label>
    <select id="color-select">
      <option value="white">${t('colorWhite')}</option>
      <option value="black">${t('colorBlack')}</option>
    </select>
    <label class="visually-hidden" for="elo-select">${t('engineStrength')}</label>
    <select id="elo-select">
      ${ELOS.map((e) => `<option value="${e.value}" ${e.value === DEFAULT_ELO ? 'selected' : ''}>${eloLabel(e)}</option>`).join('')}
    </select>
    <label class="visually-hidden" for="tc-select">${t('timeControl')}</label>
    <select id="tc-select">
      ${TIME_CONTROLS.map((tc) => `<option value="${tc.value}" ${tc.value === '15+10' ? 'selected' : ''}>${tcLabel(tc)}</option>`).join('')}
    </select>
    <button id="new-game-btn" class="primary">${t('newGame')}</button>
    <button id="pause-btn" disabled>${t('pause')}</button>
    <button id="resign-btn" disabled>${t('resign')}</button>
  `;
  els.controls.querySelector('#new-game-btn').addEventListener('click', newGame);
  els.controls.querySelector('#pause-btn').addEventListener('click', togglePause);
  els.controls.querySelector('#resign-btn').addEventListener('click', resign);
  els.controls.querySelector('#color-select').addEventListener('change', (e) => {
    board.setOrientation(e.target.value);
  });
}

async function newGame() {
  const userColor = els.controls.querySelector('#color-select').value;
  const engineElo = parseInt(els.controls.querySelector('#elo-select').value, 10);
  const timeControl = els.controls.querySelector('#tc-select').value;
  setMessage(t('startingGame'));
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
      paused: false,
      result: null,
      clock: resp.clock,
      hintCredits: resp.hint_credits,
      moveHistorySan: openingSan ? [openingSan] : [],
      lastFeedback: null,
      guard: null,
      fen: resp.fen,
    });
    applyPosition(resp.fen, resp.is_user_turn);
    board.setShapes([]);
    startClock(resp.clock);
    turnStartedAt = Date.now();
    pausedAt = 0;
    els.controls.querySelector('#resign-btn').disabled = false;
    renderPauseButton();
    setMessage('');
  } catch (err) {
    setMessage(t('couldNotStartGame', { message: err.message }), true);
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

// `play.clock` is the server's snapshot from the last reply, and the ticker
// has been counting down from it locally ever since. Re-priming a remounted
// view straight from the snapshot would hand those seconds back and make the
// clock jump visibly backwards on every language toggle, so deduct the time
// that has really passed on the side to move. `turnStartedAt` is set in the
// same breath as every `startClock()` call, which makes it the moment the
// snapshot was taken; while paused the elapsed time is measured to
// `pausedAt` instead, so time spent away is never charged.
function clockAsOfNow(clock) {
  if (!clock || !turnStartedAt) return clock;
  const gone = Math.max(0, (pausedAt || Date.now()) - turnStartedAt);
  const moving = chess?.turn() === 'w' ? 'white_ms' : 'black_ms';
  if (typeof clock[moving] !== 'number') return clock;
  return { ...clock, [moving]: Math.max(0, clock[moving] - gone) };
}

function startClock(clock) {
  if (!syncClock(clock)) return;
  clockTicker.start();
}

// Paint the clock from a server snapshot *without* starting the countdown,
// and report whether there was a clock to paint. A paused or finished game
// needs this rather than `startClock`: the ticker still has to be primed, or
// a later Resume starts an empty ticker and the clock reads "—:—" for the
// rest of the game. That is exactly what a remount used to cause — the new
// view builds a new ClockTicker, and only `sync()` gives it its numbers.
function syncClock(clock) {
  if (!clock) {
    clockTicker.stop();
    renderClocks({ white_ms: null, black_ms: null });
    return false;
  }
  clockTicker.sync({ white_ms: clock.white_ms, black_ms: clock.black_ms, turn: chess.turn() === 'w' ? 'white' : 'black' });
  return true;
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
  if (!s.gameId || s.terminated || !s.isUserTurn || s.paused) {
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
  // The position the move produces, captured before undoing. chessground has
  // already painted the drag optimistically, but its idea of the move is
  // orig->dest plus a couple of special cases; chess.js is exact, so a
  // promotion shows the new queen instead of a pawn stranded on the 8th.
  const optimistic = { fen: chess.fen(), check: chess.in_check() };
  chess.undo(); // server is authoritative; only used chess.js to validate/derive UCI
  const uci = orig + dest + (promotion || '');
  board.setShapes([]);
  showOptimistic(orig, dest, optimistic);
  await considerGuard(uci);
}

// Leaves the user's move standing on the board, unmovable, while the guard
// check and the move round-trip run. This used to repaint `lastSynced` — the
// position *before* the move — so every single move visibly snapped back to
// its starting square and sat there for two network round-trips before the
// real position arrived. `lastSynced` is still the revert target; it is just
// no longer painted speculatively.
function showOptimistic(orig, dest, { fen, check }) {
  board.setPosition({
    fen,
    turnColor: fen.split(' ')[1] === 'w' ? 'white' : 'black',
    movableColor: undefined,
    dests: new Map(),
    lastMove: [orig, dest],
    check,
  });
}

async function considerGuard(uci) {
  const s = playSlice();
  setMessage(t('checking'));
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
    setMessage(t('guardUnavailable', { message: err.message }), true);
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
  p.textContent = t('guardConfirmText');
  const actions = document.createElement('div');
  actions.className = 'actions';
  const look = document.createElement('button');
  look.className = 'look-again';
  look.textContent = t('lookAgain');
  look.addEventListener('click', () => {
    clearGuard();
    revertBoard();
  });
  const play = document.createElement('button');
  play.className = 'play-anyway';
  play.textContent = t('playAnyway');
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
  setMessage(t('opponentThinking'));
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
      // Kept in the store, not just in the chess.js mirror, so a remount can
      // repaint the board from state alone.
      fen: resp.fen,
    });
    applyPosition(resp.fen, resp.is_user_turn, lastSquares ? [lastSquares.from, lastSquares.to] : undefined);
    startClock(resp.clock);
    turnStartedAt = Date.now();
    setMessage('');
    if (resp.terminated) {
      clockTicker.stop();
      showResult(resp.result);
      els.controls.querySelector('#resign-btn').disabled = true;
      syncSlice({ paused: false });
      pausedAt = 0;
      renderPauseButton();
    }
    rail.resetScan();
  } catch (err) {
    setMessage(t('moveRejected', { message: err.message }), true);
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

// Read through t() at render time (not frozen at import) so a language
// switch mid-session never leaves a stale-language result banner on screen.
const RESULT_KEYS = { win: 'resultWin', loss: 'resultLoss', draw: 'resultDraw', abandoned: 'resultAbandoned' };
function resultLabel(result) {
  const key = RESULT_KEYS[result];
  return key ? t(key) : result;
}

function showResult(result) {
  const banner = document.createElement('div');
  banner.className = RESULT_KEYS[result] ? `result-banner ${result}` : 'result-banner';
  banner.textContent = `${resultLabel(result)} — ${t('savedToJournal')}`;
  els.resultSlot.innerHTML = '';
  els.resultSlot.appendChild(banner);
}

async function resign() {
  const s = playSlice();
  if (!s.gameId || s.terminated) return;
  try {
    const resp = await api.post(`/play/${s.gameId}/resign`, {});
    clockTicker.stop();
    syncSlice({ terminated: true, result: resp.result, paused: false });
    pausedAt = 0;
    showResult(resp.result);
    els.controls.querySelector('#resign-btn').disabled = true;
    renderPauseButton();
  } catch (err) {
    setMessage(t('resignFailed', { message: err.message }), true);
  }
}

// Pause stops the clock, locks the board, and — crucially — moves
// `turnStartedAt` forward by however long the pause lasted, so the time
// spent away is never included in the `elapsed_ms` the next move reports.
// The server is told too (POST /play/{id}/pause): it refuses moves, guards
// and hints with a 409 while the flag is set, so a pause survives a reload
// instead of being a purely cosmetic freeze.
async function togglePause() {
  const s = playSlice();
  if (!s.gameId || s.terminated) return;
  const next = !s.paused;
  const btn = els.controls.querySelector('#pause-btn');
  btn.disabled = true;
  try {
    const resp = await api.post(`/play/${s.gameId}/pause`, { paused: next });
    syncSlice({ paused: resp.paused });
    if (resp.paused) {
      pausedAt = Date.now();
      clockTicker.stop();
      if (lastSynced) board.setPosition({ ...lastSynced, movableColor: undefined });
      setMessage(t('pausedMessage'));
    } else {
      // Hand back every millisecond the pause took.
      if (pausedAt) turnStartedAt += Date.now() - pausedAt;
      pausedAt = 0;
      if (lastSynced) board.setPosition(lastSynced);
      if (playSlice().clock) clockTicker.start();
      setMessage('');
    }
  } catch (err) {
    setMessage(t('couldNotPause', { message: err.message }), true);
  } finally {
    renderPauseButton();
  }
  rail.render(playSlice());
}

// Label and enabled-ness both follow the store, so a re-render after a move
// or a resignation can't leave the button saying "Resume" on a live game.
function renderPauseButton() {
  const s = playSlice();
  const btn = els.controls.querySelector('#pause-btn');
  if (!btn) return;
  btn.disabled = !s.gameId || s.terminated;
  btn.textContent = s.paused ? t('resume') : t('pause');
  btn.classList.toggle('primary', !!s.paused);
  // Drives the desaturated board treatment in app.css.
  els.boardCol?.classList.toggle('is-paused', !!s.paused);
}
