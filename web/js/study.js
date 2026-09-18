// #/study — the three-step spine (drills -> game -> review) plus the
// long-run profile. Any stat that comes back null renders as "—" with a
// caption, never as 0 — a fabricated streak or rating is the one failure
// mode this view cannot afford.

import { api } from './api.js';
import { naOr, prettyHighlight } from './util.js';
import { makeT } from './i18n.js';
import { strings } from './strings/study.js';

const t = makeT(strings);

// Called at render time (never frozen at import) so a mid-session language
// switch — which remounts this view — picks up the new dictionary. Returns
// null for a key it doesn't know, so the caller can fall back to whatever
// the server called the step.
function stepLabel(key) {
  if (key === 'drills') return t('stepDrills');
  if (key === 'game') return t('stepGame');
  if (key === 'review') return t('stepReview');
  return null;
}

export async function mount(root) {
  root.innerHTML = `
    <div class="stack">
      <h1>${t('pageTitle')}</h1>
      <div class="panel" id="session-panel">
        <div class="session-path" id="session-path"></div>
      </div>
      <div class="panel spread">
        <div>
          <div class="streak-badge" id="streak-value">—</div>
          <div class="streak-label">${t('streakLabel')}</div>
        </div>
        <div id="session-summary" class="soft"></div>
      </div>
      <div class="panel stack">
        <h2>${t('profileHeading')}</h2>
        <div class="stat-grid" id="profile-stats"></div>
      </div>
      <div class="panel stack">
        <h2>${t('weakestHeading')}</h2>
        <ul class="motif-list" id="weakest-motifs"></ul>
      </div>
    </div>
  `;
  await Promise.all([loadSession(root), loadProfile(root)]);
}

export function unmount() {}

async function loadSession(root) {
  const path = root.querySelector('#session-path');
  const streak = root.querySelector('#streak-value');
  const summary = root.querySelector('#session-summary');
  let session;
  try {
    session = await api.get('/session/today');
  } catch {
    try {
      session = await api.post('/session/today', {});
    } catch (err) {
      path.innerHTML = '';
      const p = document.createElement('p');
      p.className = 'inline-error';
      p.textContent = t('sessionLoadError', { message: err.message });
      path.appendChild(p);
      return;
    }
  }
  streak.textContent = naOr(session.streak);
  summary.textContent = `${session.drills_done}/${session.drills_total} ${t('drillsWord')} · ${session.reviewed ? t('reviewedWord') : t('notYetReviewedWord')}`;

  path.innerHTML = '';
  const steps = session.steps && session.steps.length
    ? session.steps
    : ['drills', 'game', 'review'].map((key) => ({ key, label: stepLabel(key), done: false }));

  steps.forEach((step, i) => {
    const el = document.createElement('a');
    el.className = `session-step${step.done ? ' done' : ''}`;
    el.href = hrefForStep(step.key, session);
    const label = document.createElement('span');
    label.className = 'step-label';
    // /session/today names each step in English. The three keys it can send
    // are known here, so the local translation wins and `step.label` is the
    // fallback for a step this build hasn't heard of yet.
    label.textContent = stepLabel(step.key) || step.label || step.key;
    const state = document.createElement('span');
    state.className = 'step-state';
    state.textContent = step.done ? t('stepDone') : t('stepNotYet');
    el.append(label, state);
    path.appendChild(el);
    if (i < steps.length - 1) {
      const connector = document.createElement('div');
      connector.className = 'session-connector';
      path.appendChild(connector);
    }
  });
}

function hrefForStep(key, session) {
  if (key === 'drills') return '#/drills';
  if (key === 'game') return '#/play';
  if (key === 'review') return session.game_external_id ? `#/review/${session.game_external_id}` : '#/play';
  return '#/study';
}

async function loadProfile(root) {
  const grid = root.querySelector('#profile-stats');
  const motifList = root.querySelector('#weakest-motifs');
  let profile;
  try {
    profile = await api.get('/stats/profile');
  } catch (err) {
    grid.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'inline-error';
    p.textContent = t('profileLoadError', { message: err.message });
    grid.appendChild(p);
    return;
  }

  const tiles = [
    [t('statGamesPlayed'), String(profile.games_played)],
    [t('statRatingEstimate'), naOr(profile.rating_estimate, (v) => Math.round(v))],
    [t('statAcplOverall'), naOr(profile.acpl_overall, (v) => v.toFixed(0))],
    [t('statAcplOpening'), naOr(profile.acpl_by_phase?.opening, (v) => v.toFixed(0))],
    [t('statAcplMiddlegame'), naOr(profile.acpl_by_phase?.middlegame, (v) => v.toFixed(0))],
    [t('statAcplEndgame'), naOr(profile.acpl_by_phase?.endgame, (v) => v.toFixed(0))],
    [t('statHintsPerGame'), naOr(profile.hints_per_game, (v) => v.toFixed(1))],
    [t('statGuardFireRate'), naOr(profile.guard_fire_rate, (v) => `${(v * 100).toFixed(0)}%`)],
    [t('statBlunderUnder30'), naOr(profile.blunder_rate_under_30s, (v) => `${(v * 100).toFixed(0)}%`)],
    [t('statBlunderOver120'), naOr(profile.blunder_rate_over_120s, (v) => `${(v * 100).toFixed(0)}%`)],
    [t('statDrillsDue'), String(profile.drills_due)],
  ];
  grid.innerHTML = '';
  for (const [label, value] of tiles) {
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
      cap.textContent = t('naCaption');
      tile.appendChild(cap);
    }
    grid.appendChild(tile);
  }

  motifList.innerHTML = '';
  const motifs = profile.weakest_motifs || [];
  if (!motifs.length) {
    const li = document.createElement('li');
    li.innerHTML = '';
    li.textContent = t('noMotifsYet');
    motifList.appendChild(li);
    return;
  }
  for (const m of motifs) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    // Raw snake_case off /stats/profile ("hung_piece"); prettyHighlight is
    // the same lookup review.js uses, so the two lists name a motif alike.
    name.textContent = prettyHighlight(m.motif);
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = String(m.count);
    li.append(name, count);
    motifList.appendChild(li);
  }
}
