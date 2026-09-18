// The eval scrubber — the signature component of #/review. One canvas draws
// the eval curve with mistake (amber/red) and highlight (green) markers and
// doubles as a timeline (click/drag to seek); a second thin canvas underneath
// draws a time-per-move track on the same ply x-scale. No-names rule does
// NOT apply here — review is post-game, full disclosure.

import { clamp } from './util.js';
import { makeT } from './i18n.js';
import { strings } from './strings/scrubber.js';

const t = makeT(strings);

const CP_CLAMP = 500; // +/- 5 pawns fills the band; beyond that we saturate
const PAD = 8;

// Best-effort field access: the API contract doesn't pin down exact move /
// mistake / highlight shapes, so this tries the plausible names in order
// rather than throwing on a slightly different backend response.
function plyOf(obj, index) {
  return obj.ply ?? obj.move_ply ?? obj.index ?? index;
}
function evalOf(move) {
  if (move.eval_mate !== undefined && move.eval_mate !== null) {
    return move.eval_mate > 0 ? CP_CLAMP : -CP_CLAMP;
  }
  const cp = move.eval_cp ?? move.eval ?? null;
  return cp === null ? null : clamp(cp, -CP_CLAMP, CP_CLAMP);
}
function timeOf(move) {
  return move.time_ms ?? move.elapsed_ms ?? null;
}

export function createScrubber(root, { onSeek } = {}) {
  root.innerHTML = `
    <canvas id="scrubber-canvas" tabindex="0" role="slider"
      aria-label="${t('timelineAria')}"
      aria-valuemin="0"></canvas>
    <canvas class="scrubber-time-canvas" aria-hidden="true"></canvas>
    <p class="scrubber-hint">${t('hint')}</p>
  `;
  const evalCanvas = root.querySelector('#scrubber-canvas');
  const timeCanvas = root.querySelector('.scrubber-time-canvas');
  const evalCtx = evalCanvas.getContext('2d');
  const timeCtx = timeCanvas.getContext('2d');

  let moves = [];
  let mistakeByPly = new Map();
  let highlightByPly = new Map();
  let ply = 0;
  let dragging = false;

  function sizeCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    return dpr;
  }

  function render(data) {
    moves = data.moves || [];
    mistakeByPly = new Map((data.mistakes || []).map((m, i) => [plyOf(m, i), m]));
    highlightByPly = new Map((data.highlights || []).map((h, i) => [plyOf(h, i), h]));
    ply = clamp(ply, 0, Math.max(0, moves.length - 1));
    evalCanvas.setAttribute('aria-valuemax', String(Math.max(0, moves.length - 1)));
    draw();
  }

  function setPly(nextPly) {
    ply = clamp(nextPly, 0, Math.max(0, moves.length - 1));
    evalCanvas.setAttribute('aria-valuenow', String(ply));
    draw();
  }

  function draw() {
    drawEval();
    drawTime();
  }

  function drawEval() {
    const dpr = sizeCanvas(evalCanvas);
    const w = evalCanvas.width, h = evalCanvas.height;
    evalCtx.clearRect(0, 0, w, h);
    if (!moves.length) return;

    const innerW = w - PAD * 2 * dpr;
    const midY = h / 2;
    const scaleY = (h / 2 - PAD * dpr) / CP_CLAMP;
    const xFor = (i) => PAD * dpr + (moves.length <= 1 ? 0 : (i / (moves.length - 1)) * innerW);

    // zero line
    evalCtx.strokeStyle = 'rgba(120,120,120,0.35)';
    evalCtx.lineWidth = 1;
    evalCtx.beginPath();
    evalCtx.moveTo(0, midY);
    evalCtx.lineTo(w, midY);
    evalCtx.stroke();

    // eval curve, filled area
    evalCtx.beginPath();
    let started = false;
    for (let i = 0; i < moves.length; i++) {
      const e = evalOf(moves[i]);
      if (e === null) continue;
      const x = xFor(i);
      const y = midY - e * scaleY;
      if (!started) { evalCtx.moveTo(x, y); started = true; } else evalCtx.lineTo(x, y);
    }
    evalCtx.strokeStyle = getVar('--ink-soft', '#555');
    evalCtx.lineWidth = 2 * dpr;
    evalCtx.stroke();

    // mistake / highlight markers
    for (let i = 0; i < moves.length; i++) {
      const x = xFor(i);
      const mistake = mistakeByPly.get(plyOf(moves[i], i));
      const highlight = highlightByPly.get(plyOf(moves[i], i));
      if (highlight) drawMarker(x, midY, getVar('--good', '#3a7d44'));
      if (mistake) {
        const sev = mistake.severity || 'inaccuracy';
        const color = sev === 'blunder' ? getVar('--bad', '#a33') : getVar('--warn', '#b58900');
        drawMarker(x, midY, color, sev === 'blunder' ? 5 * dpr : 3.5 * dpr);
      }
    }

    // playhead
    const px = xFor(ply);
    evalCtx.strokeStyle = getVar('--accent', '#345');
    evalCtx.lineWidth = 2 * dpr;
    evalCtx.beginPath();
    evalCtx.moveTo(px, 0);
    evalCtx.lineTo(px, h);
    evalCtx.stroke();
  }

  function drawMarker(x, y, color, r = 4) {
    evalCtx.beginPath();
    evalCtx.fillStyle = color;
    evalCtx.arc(x, y, r, 0, Math.PI * 2);
    evalCtx.fill();
  }

  function drawTime() {
    const dpr = sizeCanvas(timeCanvas);
    const w = timeCanvas.width, h = timeCanvas.height;
    timeCtx.clearRect(0, 0, w, h);
    if (!moves.length) return;
    const times = moves.map(timeOf);
    const max = Math.max(1, ...times.filter((t) => t !== null));
    const barW = w / moves.length;
    timeCtx.fillStyle = getVar('--ink-faint', '#999');
    for (let i = 0; i < moves.length; i++) {
      const t = times[i];
      if (t === null) continue;
      const barH = (t / max) * h;
      timeCtx.fillRect(i * barW, h - barH, Math.max(1, barW - 1 * dpr), barH);
    }
  }

  function getVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return v ? v.trim() : fallback;
  }

  function seekFromEvent(clientX) {
    const rect = evalCanvas.getBoundingClientRect();
    const frac = clamp((clientX - rect.left) / rect.width, 0, 1);
    const nextPly = Math.round(frac * Math.max(0, moves.length - 1));
    setPly(nextPly);
    onSeek?.(ply);
  }

  evalCanvas.addEventListener('pointerdown', (e) => {
    dragging = true;
    evalCanvas.setPointerCapture(e.pointerId);
    seekFromEvent(e.clientX);
  });
  evalCanvas.addEventListener('pointermove', (e) => {
    if (dragging) seekFromEvent(e.clientX);
  });
  evalCanvas.addEventListener('pointerup', () => { dragging = false; });
  evalCanvas.addEventListener('pointercancel', () => { dragging = false; });

  evalCanvas.addEventListener('keydown', (e) => {
    if (!moves.length) return;
    if (e.key === 'ArrowLeft') { setPly(ply - 1); onSeek?.(ply); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { setPly(ply + 1); onSeek?.(ply); e.preventDefault(); }
    else if (e.key === 'Home') { setPly(0); onSeek?.(ply); e.preventDefault(); }
    else if (e.key === 'End') { setPly(moves.length - 1); onSeek?.(ply); e.preventDefault(); }
  });

  const ro = new ResizeObserver(() => draw());
  ro.observe(root);

  return { render, setPly, destroy: () => ro.disconnect() };
}
