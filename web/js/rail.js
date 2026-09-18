// The coach rail: process prompt -> hint ladder -> post-move feedback, in
// that fixed order (ROADMAP §2). This is the component that enforces the
// no-names rule on the client: tiers 0-3 render prose only; only a tier-4
// response is allowed to carry move_uci/pv, and the eval number / best_uci
// from a move's feedback object are never rendered here, ever, during a
// live game.

import { api, ApiError } from './api.js';
import { phaseForPly, prettyHighlight, severityClass } from './util.js';

const PROCESS_PROMPTS = {
  opening: [
    'Development — is every minor piece heading somewhere useful?',
    'King safety — is castling still on schedule?',
    'Centre — whose pawns control more central squares?',
  ],
  middlegame: [
    "What did their last move attack?",
    'What is my worst-placed piece?',
    'Are all my pieces defended?',
    'Checks, captures, threats — in that order.',
  ],
  endgame: [
    'Which king is more active?',
    'Can any pawn become a passer?',
    'Is there a way to trade into a won endgame?',
  ],
};

const SCAN_ITEMS = ['checks', 'captures', 'threats'];

const TIER_META = [
  { tier: 1, name: 'Temperature', desc: 'worth a closer look?', costLabel: 'free · max 3' },
  { tier: 2, name: 'Category', desc: 'what kind of tactic', costLabel: '1 credit' },
  { tier: 3, name: 'Region', desc: 'which squares', costLabel: '2 credits' },
  { tier: 4, name: 'Solution', desc: 'the move, explained', costLabel: '3 credits' },
];

export function createRail(container, { onHintResult } = {}) {
  let scanChecked = new Set();
  let lastHintRender = null; // { tier, text, motif }
  let pending = false;

  function resetScan() {
    scanChecked = new Set();
  }

  async function requestHint(tier, playState) {
    if (pending || !playState.gameId) return;
    pending = true;
    render(playState);
    try {
      const result = await api.post(`/play/${playState.gameId}/hint`, { tier });
      lastHintRender = { tier: result.tier, text: result.text, motif: result.motif };
      if (onHintResult) onHintResult(result);
    } catch (err) {
      const message = err instanceof ApiError && err.status === 402
        ? 'Not enough hint credits left this game.'
        : `Hint request failed: ${err.message}`;
      lastHintRender = { tier, text: message, motif: null, isError: true };
    } finally {
      pending = false;
      render(playState);
    }
  }

  function render(playState) {
    const phase = phaseForPly(playState.moveHistorySan.length);
    container.textContent = '';

    container.appendChild(renderProcessSection(phase, playState.isUserTurn));
    container.appendChild(renderHintSection(playState, phase));
    if (playState.lastFeedback) container.appendChild(renderFeedbackSection(playState.lastFeedback));
  }

  function renderProcessSection(phase, isUserTurn) {
    const section = document.createElement('section');
    section.className = 'rail-section';
    section.setAttribute('aria-labelledby', 'rail-process-h');
    const h = document.createElement('h2');
    h.id = 'rail-process-h';
    h.textContent = 'Before you move';
    const marker = document.createElement('div');
    marker.className = 'phase-marker';
    marker.textContent = phase;
    const list = document.createElement('ul');
    list.className = 'process-prompt';
    for (const item of PROCESS_PROMPTS[phase]) {
      const li = document.createElement('li');
      li.appendChild(document.createTextNode(item));
      list.appendChild(li);
    }
    const scan = document.createElement('ul');
    scan.className = 'process-prompt';
    for (const key of SCAN_ITEMS) {
      const li = document.createElement('li');
      if (scanChecked.has(key)) li.classList.add('checked');
      const label = document.createElement('label');
      label.style.display = 'flex';
      label.style.gap = '0.5em';
      label.style.alignItems = 'flex-start';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = scanChecked.has(key);
      cb.disabled = !isUserTurn;
      cb.addEventListener('change', () => {
        if (cb.checked) scanChecked.add(key);
        else scanChecked.delete(key);
        li.classList.toggle('checked', cb.checked);
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(key));
      li.appendChild(label);
      scan.appendChild(li);
    }
    const hr = document.createElement('hr');
    hr.className = 'hairline';
    section.append(marker, list, hr, scan);
    return section;
  }

  function renderHintSection(playState, phase) {
    const section = document.createElement('section');
    section.className = 'rail-section hint-ladder';
    section.setAttribute('aria-labelledby', 'rail-hint-h');
    const h = document.createElement('h2');
    h.id = 'rail-hint-h';
    h.textContent = 'Hint ladder';

    const credits = document.createElement('div');
    credits.className = 'hint-credits';
    credits.setAttribute('aria-label', `${playState.hintCredits} hint credits left of 6`);
    for (let i = 0; i < 6; i++) {
      const dot = document.createElement('span');
      dot.className = 'dot' + (i < playState.hintCredits ? '' : ' spent');
      dot.textContent = '●';
      credits.appendChild(dot);
    }

    section.append(h, credits);

    const canAct = playState.isUserTurn && !playState.terminated && !!playState.gameId;
    for (const meta of TIER_META) {
      const btn = document.createElement('button');
      btn.className = 'hint-tier';
      btn.disabled = pending || !canAct || playState.hintCredits <= 0;
      const name = document.createElement('span');
      name.className = 'tier-name';
      name.textContent = `${meta.tier} · ${meta.name}`;
      const cost = document.createElement('span');
      cost.className = 'tier-cost';
      cost.textContent = meta.costLabel;
      btn.append(name, cost);
      btn.title = meta.desc;
      btn.addEventListener('click', () => requestHint(meta.tier, playState));
      section.appendChild(btn);
    }

    if (lastHintRender) {
      const resp = document.createElement('div');
      resp.className = 'hint-response';
      if (lastHintRender.isError) resp.style.borderColor = 'var(--bad)';
      if (lastHintRender.motif) {
        const motif = document.createElement('div');
        motif.className = 'motif';
        motif.textContent = prettyHighlight(lastHintRender.motif);
        resp.appendChild(motif);
      }
      const text = document.createElement('p');
      text.style.margin = '0.35em 0 0';
      text.textContent = lastHintRender.text;
      resp.appendChild(text);
      section.appendChild(resp);
    }

    return section;
  }

  // Feedback rendered here is strictly post-move: severity + motifs + the
  // highlight kind. It never includes best_uci or an eval number — those
  // fields exist on the API response but are deliberately not read here.
  function renderFeedbackSection(feedback) {
    const section = document.createElement('section');
    section.className = 'rail-section feedback-card';
    section.setAttribute('aria-labelledby', 'rail-feedback-h');
    const h = document.createElement('h2');
    h.id = 'rail-feedback-h';
    h.textContent = 'Your last move';

    const badge = document.createElement('span');
    if (feedback.highlight) {
      badge.className = 'badge good';
      badge.textContent = prettyHighlight(feedback.highlight);
    } else if (feedback.severity) {
      badge.className = `badge ${severityClass(feedback.severity)}`;
      badge.textContent = feedback.severity;
    } else {
      badge.className = 'badge good';
      badge.textContent = 'sound';
    }

    section.append(h, badge);

    if (feedback.motifs && feedback.motifs.length) {
      const motifWrap = document.createElement('div');
      motifWrap.className = 'motif-list';
      for (const m of feedback.motifs) {
        const tag = document.createElement('span');
        tag.className = 'badge accent';
        tag.textContent = prettyHighlight(m);
        motifWrap.appendChild(tag);
      }
      section.appendChild(motifWrap);
    }

    return section;
  }

  return { render, resetScan };
}
