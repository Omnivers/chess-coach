// chessground lifecycle. Every board.set() call passes turnColor explicitly
// (chessground only reads pieces from a FEN, never the side to move, and it
// flips turnColor itself after a user move — omitting this locks the board
// after one move). movable.free is always false, so movable.dests must
// always be supplied or every drag is silently refused.

import { Chessground } from 'https://cdn.jsdelivr.net/npm/chessground@9.1.1/dist/chessground.min.js';

const HINT_BRUSH = { key: 'hint', color: 'oklch(70% 0.14 75)', opacity: 0.85, lineWidth: 8 };
const SOLUTION_BRUSH = { key: 'solution', color: 'oklch(48% 0.10 250)', opacity: 0.9, lineWidth: 10 };
const REVIEW_USER_BRUSH = { key: 'reviewUser', color: 'oklch(22% 0.02 60)', opacity: 0.8, lineWidth: 9 };
const REVIEW_BEST_BRUSH = { key: 'reviewBest', color: 'oklch(48% 0.10 250)', opacity: 0.8, lineWidth: 9 };

// Computes chessground's movable.dests Map from a chess.js instance
// (0.10.3 API: .moves({verbose:true})).
export function computeDests(chess) {
  const dests = new Map();
  for (const m of chess.moves({ verbose: true })) {
    if (!dests.has(m.from)) dests.set(m.from, []);
    dests.get(m.from).push(m.to);
  }
  return dests;
}

export function createBoard(el, { orientation = 'white', onUserMove } = {}) {
  const board = Chessground(el, {
    // Inert until a game exists — a drag before that would POST to
    // /play/null/move and 404.
    fen: '8/8/8/8/8/8/8/8 w - - 0 1',
    orientation,
    turnColor: 'white',
    viewOnly: true,
    movable: { free: false, color: undefined, dests: new Map() },
    draggable: { enabled: true },
    selectable: { enabled: true },
    highlight: { lastMove: true, check: true },
    animation: { enabled: true, duration: 200 },
    drawable: {
      enabled: true,
      brushes: {
        hint: HINT_BRUSH,
        solution: SOLUTION_BRUSH,
        reviewUser: REVIEW_USER_BRUSH,
        reviewBest: REVIEW_BEST_BRUSH,
      },
    },
  });

  function setInert() {
    board.set({ viewOnly: true, movable: { color: undefined, dests: new Map() } });
    board.setShapes([]);
  }

  // fen is guarded — a falsy fen is silently ignored by chessground, and a
  // previous version of this app left a rejected move stuck on the board
  // because of exactly that.
  function setPosition({ fen, turnColor, movableColor, dests, lastMove, check }) {
    if (!fen) return;
    board.set({
      fen,
      turnColor,
      viewOnly: false,
      check: !!check,
      lastMove,
      movable: {
        free: false,
        color: movableColor,
        dests: dests || new Map(),
        events: onUserMove ? { after: onUserMove } : undefined,
      },
    });
  }

  function setShapes(shapes) {
    board.setShapes(shapes || []);
  }

  function setOrientation(orientation) {
    board.set({ orientation });
  }

  function destroy() {
    board.destroy();
  }

  return { raw: board, setInert, setPosition, setShapes, setOrientation, destroy };
}

// A square-only highlight (hint tiers 2/3) or an arrow (tier 4 / review).
export function squareShape(square, brush = 'hint') {
  return { orig: square, brush };
}

export function arrowShape(fromSquare, toSquare, brush = 'solution') {
  return { orig: fromSquare, dest: toSquare, brush };
}

// Detects a pawn reaching the last rank so play.js can show a promotion
// picker before appending the UCI suffix. chessground only hands back
// orig/dest on a user move — promotion has to be inferred here.
export function isPromotion(chess, orig, dest) {
  const piece = chess.get(orig);
  if (!piece || piece.type !== 'p') return false;
  const destRank = dest[1];
  return (piece.color === 'w' && destRank === '8') || (piece.color === 'b' && destRank === '1');
}
