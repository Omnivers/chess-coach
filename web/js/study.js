// #/study — the three-step spine (drills -> game -> review) plus the
// long-run profile. Any stat that comes back null renders as "—" with a
// caption, never as 0 — a fabricated streak or rating is the one failure
// mode this view cannot afford.

import { api } from './api.js';
import { naOr } from './util.js';

const STEP_LABEL_FALLBACK = { drills: 'Drills', game: 'Game', review: 'Review' };

export async function mount(root) {
  root.innerHTML = `
    <div class="stack">
      <h1>Today</h1>
      <div class="panel" id="session-panel">
        <div class="session-path" id="session-path"></div>
      </div>
      <div class="panel spread">
        <div>
          <div class="streak-badge" id="streak-value">—</div>
          <div class="streak-label">Day streak</div>
        </div>
        <div id="session-summary" class="soft"></div>
      </div>
      <div class="panel stack">
        <h2>Your profile</h2>
        <div class="stat-grid" id="profile-stats"></div>
      </div>
      <div class="panel stack">
        <h2>Weakest motifs</h2>
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
      p.textContent = `Could not load today's session: ${err.message}`;
      path.appendChild(p);
      return;
    }
  }
  streak.textContent = naOr(session.streak);
  summary.textContent = `${session.drills_done}/${session.drills_total} drills · ${session.reviewed ? 'reviewed' : 'not yet reviewed'}`;

  path.innerHTML = '';
  const steps = session.steps && session.steps.length
    ? session.steps
    : ['drills', 'game', 'review'].map((key) => ({ key, label: STEP_LABEL_FALLBACK[key], done: false }));

  steps.forEach((step, i) => {
    const el = document.createElement('a');
    el.className = `session-step${step.done ? ' done' : ''}`;
    el.href = hrefForStep(step.key, session);
    const label = document.createElement('span');
    label.className = 'step-label';
    label.textContent = step.label || STEP_LABEL_FALLBACK[step.key] || step.key;
    const state = document.createElement('span');
    state.className = 'step-state';
    state.textContent = step.done ? 'Done' : 'Not yet';
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
    p.textContent = `Could not load your profile: ${err.message}`;
    grid.appendChild(p);
    return;
  }

  const tiles = [
    ['Games played', String(profile.games_played)],
    ['Rating estimate', naOr(profile.rating_estimate, (v) => Math.round(v))],
    ['ACPL (overall)', naOr(profile.acpl_overall, (v) => v.toFixed(0))],
    ['ACPL · opening', naOr(profile.acpl_by_phase?.opening, (v) => v.toFixed(0))],
    ['ACPL · middlegame', naOr(profile.acpl_by_phase?.middlegame, (v) => v.toFixed(0))],
    ['ACPL · endgame', naOr(profile.acpl_by_phase?.endgame, (v) => v.toFixed(0))],
    ['Hints per game', naOr(profile.hints_per_game, (v) => v.toFixed(1))],
    ['Guard fire rate', naOr(profile.guard_fire_rate, (v) => `${(v * 100).toFixed(0)}%`)],
    ['Blunders <30s', naOr(profile.blunder_rate_under_30s, (v) => `${(v * 100).toFixed(0)}%`)],
    ['Blunders >120s', naOr(profile.blunder_rate_over_120s, (v) => `${(v * 100).toFixed(0)}%`)],
    ['Drills due', String(profile.drills_due)],
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
      cap.textContent = 'not enough games yet';
      tile.appendChild(cap);
    }
    grid.appendChild(tile);
  }

  motifList.innerHTML = '';
  const motifs = profile.weakest_motifs || [];
  if (!motifs.length) {
    const li = document.createElement('li');
    li.innerHTML = '';
    li.textContent = 'Not enough games yet to identify a pattern.';
    motifList.appendChild(li);
    return;
  }
  for (const m of motifs) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = m.motif;
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = String(m.count);
    li.append(name, count);
    motifList.appendChild(li);
  }
}
