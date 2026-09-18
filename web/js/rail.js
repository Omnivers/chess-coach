// The coach rail: process prompt -> hint ladder -> post-move feedback, in
// that fixed order (ROADMAP §2). This is the component that enforces the
// no-names rule on the client: tiers 0-3 render prose only; only a tier-4
// response is allowed to carry move_uci/pv, and the eval number / best_uci
// from a move's feedback object are never rendered here, ever, during a
// live game.

import { api, ApiError } from './api.js';
import { phaseForPly, prettyHighlight, severityClass } from './util.js';
import { makeT } from './i18n.js';
import { strings } from './strings/rail.js';

const t = makeT(strings);

// Phase-specific questions, in the words a beginner actually uses. The
// previous set was written in club vocabulary ("is every minor piece
// heading somewhere useful?", "whose pawns control more central squares?")
// and the user's verdict on it was "i don't understand wtf is this" — fair,
// since nothing on the page ever said what a minor piece was or why the
// centre mattered. Every prompt here names the concrete thing to look at.
//
// Read through t() at render time rather than frozen at import, so a
// language switch mid-session repaints these instead of leaving stale text.
function processPrompts(phase) {
  if (phase === 'opening') return [t('promptOpening0'), t('promptOpening1'), t('promptOpening2')];
  if (phase === 'middlegame') return [t('promptMiddlegame0'), t('promptMiddlegame1'), t('promptMiddlegame2')];
  return [t('promptEndgame0'), t('promptEndgame1'), t('promptEndgame2')];
}

function phaseLabel(phase) {
  if (phase === 'opening') return t('phaseOpening');
  if (phase === 'middlegame') return t('phaseMiddlegame');
  return t('phaseEndgame');
}

function scanItems() {
  return [
    { key: 'checks', label: t('scanChecksLabel'), question: t('scanChecksQuestion') },
    { key: 'captures', label: t('scanCapturesLabel'), question: t('scanCapturesQuestion') },
    { key: 'threats', label: t('scanThreatsLabel'), question: t('scanThreatsQuestion') },
  ];
}

// `cost` mirrors HINT_COSTS in chess_coach/api_play.py ({1:1, 2:1, 3:2,
// 4:2}) and the wording of each `desc` mirrors what build_hint() in
// coach_text.py actually returns for that tier. Both were wrong in the
// previous version — it advertised "free / 1 / 2 / 3 credits" against a
// backend that charges 1/1/2/2, so the dots on screen disagreed with the
// dots the server spent. If either table moves, this one moves with it.
function tierMeta() {
  return [
    { tier: 1, cost: 1, name: t('tier1Name'), desc: t('tier1Desc') },
    { tier: 2, cost: 1, name: t('tier2Name'), desc: t('tier2Desc') },
    { tier: 3, cost: 2, name: t('tier3Name'), desc: t('tier3Desc') },
    { tier: 4, cost: 2, name: t('tier4Name'), desc: t('tier4Desc') },
  ];
}

function nudgeCost(cost) {
  return t('hintCostLabel', { cost });
}

function capitalize(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

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
    // `playState` is a snapshot frozen when this button was built, so its
    // hintCredits is the count from *before* this request. Re-rendering
    // straight from it repaints the old number over the new one — which is
    // how the ladder sat at "6 of 6 nudges left" through three spent hints
    // while the server was correctly decrementing. `credits_left` off the
    // response is the server's own figure and the only one worth drawing.
    // The parent's onHintResult writes the same number into the store, so
    // the next render from play.js agrees with this one.
    let state = playState;
    render(state);
    try {
      const result = await api.post(`/play/${playState.gameId}/hint`, { tier });
      state = { ...playState, hintCredits: result.credits_left };
      lastHintRender = { tier: result.tier, text: result.text, motif: result.motif };
      if (onHintResult) onHintResult(result);
    } catch (err) {
      const message = err instanceof ApiError && err.status === 402
        ? t('hintNotEnoughCredits')
        : t('hintRequestFailed', { message: err.message });
      lastHintRender = { tier, text: message, motif: null, isError: true };
    } finally {
      pending = false;
      render(state);
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

    const head = document.createElement('div');
    head.className = 'rail-head';
    const h = document.createElement('h2');
    h.id = 'rail-process-h';
    h.className = 'section-title';
    h.textContent = t('processHeading');
    const marker = document.createElement('span');
    marker.className = 'phase-marker';
    marker.textContent = phaseLabel(phase);
    head.append(h, marker);

    const lede = document.createElement('p');
    lede.className = 'rail-lede';
    lede.textContent = t('processLede');

    const scan = document.createElement('ul');
    scan.className = 'scan-list';
    for (const item of scanItems()) {
      const li = document.createElement('li');
      li.className = 'scan-row';
      if (scanChecked.has(item.key)) li.classList.add('checked');
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = scanChecked.has(item.key);
      cb.disabled = !isUserTurn;
      cb.addEventListener('change', () => {
        if (cb.checked) scanChecked.add(item.key);
        else scanChecked.delete(item.key);
        li.classList.toggle('checked', cb.checked);
      });
      const text = document.createElement('span');
      text.className = 'scan-text';
      const name = document.createElement('span');
      name.className = 'scan-name';
      name.textContent = item.label;
      const question = document.createElement('span');
      question.className = 'scan-question';
      question.textContent = item.question;
      text.append(name, question);
      label.append(cb, text);
      li.appendChild(label);
      scan.appendChild(li);
    }

    const scanNote = document.createElement('p');
    scanNote.className = 'scan-note';
    scanNote.textContent = t('scanNote');

    const hr = document.createElement('hr');
    hr.className = 'hairline';

    const subhead = document.createElement('h3');
    subhead.className = 'rail-subhead';
    subhead.textContent = t('processSubhead');

    const list = document.createElement('ul');
    list.className = 'process-prompt';
    for (const item of processPrompts(phase)) {
      const li = document.createElement('li');
      li.appendChild(document.createTextNode(item));
      list.appendChild(li);
    }

    section.append(head, lede, scan, scanNote, hr, subhead, list);
    return section;
  }

  function renderHintSection(playState, phase) {
    const section = document.createElement('section');
    section.className = 'rail-section hint-ladder';
    section.setAttribute('aria-labelledby', 'rail-hint-h');
    const h = document.createElement('h2');
    h.id = 'rail-hint-h';
    h.className = 'section-title';
    h.textContent = t('hintHeading');

    const lede = document.createElement('p');
    lede.className = 'rail-lede';
    lede.textContent = t('hintLede');

    const credits = document.createElement('div');
    credits.className = 'hint-credits';
    const dots = document.createElement('div');
    dots.className = 'hint-dots';
    // The dots are a picture of the number that is already written out in
    // `creditsLabel` next to them, so a screen reader should hear it once.
    dots.setAttribute('aria-hidden', 'true');
    for (let i = 0; i < 6; i++) {
      const dot = document.createElement('span');
      dot.className = 'dot' + (i < playState.hintCredits ? '' : ' spent');
      dot.textContent = '●';
      dots.appendChild(dot);
    }
    const creditsLabel = document.createElement('span');
    creditsLabel.className = 'hint-credits-label';
    creditsLabel.textContent = t('hintCreditsLeft', { n: playState.hintCredits });
    credits.append(dots, creditsLabel);

    const creditNote = document.createElement('p');
    creditNote.className = 'credit-note';
    creditNote.textContent = t('hintCreditNote');

    section.append(h, lede, credits, creditNote);

    // `paused` too: the server 409s hint requests on a paused game, so
    // offering the buttons would only produce an error card.
    const canAct = playState.isUserTurn && !playState.terminated && !!playState.gameId && !playState.paused;
    for (const meta of tierMeta()) {
      const btn = document.createElement('button');
      btn.className = 'hint-tier';
      // Gated on this tier's own price, not on credits being zero: with one
      // nudge left, levels 1-2 are affordable and 3-4 are not. The blanket
      // check offered all four and let the server answer 402.
      const affordable = playState.hintCredits >= meta.cost;
      btn.disabled = pending || !canAct || !affordable;
      const name = document.createElement('span');
      name.className = 'tier-name';
      name.textContent = `${meta.tier} · ${meta.name}`;
      const cost = document.createElement('span');
      cost.className = 'tier-cost';
      cost.textContent = nudgeCost(meta.cost);
      const desc = document.createElement('span');
      desc.className = 'tier-desc';
      desc.textContent = meta.desc;
      btn.append(name, cost, desc);
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
    h.className = 'section-title';
    h.textContent = t('feedbackHeading');

    const badge = document.createElement('span');
    if (feedback.highlight) {
      badge.className = 'badge good';
      badge.textContent = prettyHighlight(feedback.highlight);
    } else if (feedback.severity) {
      badge.className = `badge ${severityClass(feedback.severity)}`;
      badge.textContent = feedback.severity;
    } else {
      badge.className = 'badge good';
      badge.textContent = t('feedbackSound');
    }

    const lede = document.createElement('p');
    lede.className = 'rail-lede';
    lede.textContent = t('feedbackLede');

    section.append(h, badge, lede);

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
