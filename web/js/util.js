// Formatters and small pure helpers shared across views. No DOM, no state.

export function formatClock(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—:—';
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function cpToPawns(cp) {
  if (cp === null || cp === undefined) return null;
  const pawns = cp / 100;
  return (pawns >= 0 ? '+' : '') + pawns.toFixed(2);
}

// Rough client-side phase estimate used only to choose which static process
// prompt to show. It is not a coaching claim and never reaches the server.
export function phaseForPly(ply) {
  if (ply < 20) return 'opening';
  if (ply < 60) return 'middlegame';
  return 'endgame';
}

// Pairs a flat SAN move list into numbered rows for the two-column list.
export function sanPairs(sanList) {
  const rows = [];
  for (let i = 0; i < sanList.length; i += 2) {
    rows.push({ num: i / 2 + 1, white: sanList[i] || '', black: sanList[i + 1] || '' });
  }
  return rows;
}

// severity ('inaccuracy'|'mistake'|'blunder') or a highlight kind (truthy,
// any string) -> a badge class + short label. Never leaks a move/square.
export function feedbackBadge(feedback) {
  if (!feedback) return null;
  if (feedback.highlight) return { cls: 'good', label: prettyHighlight(feedback.highlight) };
  if (feedback.severity) return { cls: severityClass(feedback.severity), label: feedback.severity };
  return null;
}

export function severityClass(severity) {
  if (severity === 'blunder') return 'bad';
  if (severity === 'mistake' || severity === 'inaccuracy') return 'warn';
  return 'good';
}

export function prettyHighlight(kind) {
  return String(kind).replace(/_/g, ' ');
}

// Renders a value that may be null as an em dash — NEVER as 0. Coaching
// stats must not fabricate progress from missing data.
export function naOr(value, fmt) {
  if (value === null || value === undefined) return '—';
  return fmt ? fmt(value) : String(value);
}

export function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

export function squareToXY(square) {
  const file = square.charCodeAt(0) - 97;
  const rank = parseInt(square[1], 10) - 1;
  return { file, rank };
}

export function uciToSquares(uci) {
  if (!uci || uci.length < 4) return null;
  return { from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci.slice(4) || undefined };
}

export function debounce(fn, wait) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), wait);
  };
}
