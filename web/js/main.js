// Bootstrap + hash router. The chat panel mounts once and persists across
// every route; only the view-root content is swapped.

import { getState, setState, storeTheme } from './state.js';
import * as playView from './play.js';
import * as reviewView from './review.js';
import * as drillsView from './drills.js';
import * as studyView from './study.js';
import * as openingsView from './openings.js';
import * as chat from './chat.js';

const THEME_CYCLE = ['system', 'light', 'dark'];
const THEME_ICON = { system: '◐', light: '☀', dark: '☾' };

const ROUTES = [
  { pattern: /^#\/review\/(.+)$/, name: 'review', view: reviewView, params: (m) => ({ id: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/openings$/, name: 'openings', view: openingsView, params: () => ({}) },
  { pattern: /^#\/drills$/, name: 'drills', view: drillsView, params: () => ({}) },
  { pattern: /^#\/study$/, name: 'study', view: studyView, params: () => ({}) },
  { pattern: /^#\/play$/, name: 'play', view: playView, params: () => ({}) },
];

let currentView = null;
let viewRoot = null;

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') root.dataset.theme = theme;
  else delete root.dataset.theme;
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = THEME_ICON[theme] || THEME_ICON.system;
}

function initTheme() {
  const stored = getState().theme;
  const theme = THEME_CYCLE.includes(stored) ? stored : 'system';
  applyTheme(theme);
  const btn = document.getElementById('theme-toggle');
  btn?.addEventListener('click', () => {
    const current = THEME_CYCLE.includes(getState().theme) ? getState().theme : 'system';
    const next = THEME_CYCLE[(THEME_CYCLE.indexOf(current) + 1) % THEME_CYCLE.length];
    setState({ theme: next });
    storeTheme(next === 'system' ? null : next);
    applyTheme(next);
  });
}

function updateNav(routeName) {
  for (const a of document.querySelectorAll('header nav a')) {
    if (a.dataset.route === routeName) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
}

async function route() {
  const hash = window.location.hash || '#/play';
  // The fallback has to have the same { r, m } shape as a match. It used to
  // be a bare `ROUTES.find(...)`, i.e. a route object with no `r` on it — so
  // any hash outside the table (a typo, an old bookmark, `#/review` without
  // an id) destructured `r` as undefined, threw on `r.view`, and left the
  // previous view's DOM on screen with the nav still pointing at it.
  const match = ROUTES.map((r) => ({ r, m: hash.match(r.pattern) })).find((x) => x.m);
  const { r, m } = match || { r: ROUTES.find((x) => x.name === 'play'), m: null };

  currentView?.unmount?.();
  currentView = r.view;
  setState({ route: { name: r.name, params: m ? r.params(m) : {} } });
  updateNav(r.name);
  viewRoot.innerHTML = '';
  await r.view.mount(viewRoot, m ? r.params(m) : {});
}

function boot() {
  viewRoot = document.getElementById('view-root');
  initTheme();
  chat.mount(document.getElementById('chat-panel'));
  window.addEventListener('hashchange', route);
  route();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
