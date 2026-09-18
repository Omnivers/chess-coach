// Language layer. FR is the default; EN is kept because the engine, the
// opening names and half the chess literature the user will meet are in it.
//
// Strings live in one dictionary per module under `strings/`, not in one
// global table. A single table would be the file every future change has to
// touch at once, and the views here are genuinely independent — play.js has
// no business knowing what the drills view calls a streak.
//
// A module does:
//   import { makeT } from './i18n.js';
//   import { strings } from './strings/play.js';
//   const t = makeT(strings);
//   t('newGame')                  -> "Nouvelle partie"
//   t('nudgesLeft', { n: 4 })     -> "4 indices restants sur 6"

const STORE_KEY = 'chess-coach-lang';

export const LANGS = ['fr', 'en'];
const DEFAULT_LANG = 'fr';

let current = readStored();
const listeners = new Set();

function readStored() {
  try {
    const stored = localStorage.getItem(STORE_KEY);
    if (LANGS.includes(stored)) return stored;
  } catch (e) {
    // Private mode / storage disabled — fall through to the default.
  }
  return DEFAULT_LANG;
}

export function getLang() {
  return current;
}

/** Sets <html lang>. Called at boot as well as on every change, because the
 *  attribute drives hyphenation, quotes and screen-reader pronunciation. */
export function applyLangAttribute() {
  document.documentElement.lang = current;
}

export function setLang(next) {
  if (!LANGS.includes(next) || next === current) return;
  current = next;
  try {
    localStorage.setItem(STORE_KEY, next);
  } catch (e) {
    // Not fatal: the choice just won't survive a reload.
  }
  applyLangAttribute();
  for (const fn of [...listeners]) fn(next);
}

export function toggleLang() {
  setLang(current === 'fr' ? 'en' : 'fr');
}

export function onLangChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// `{name}` placeholders. Deliberately not a template-literal eval: these
// strings are data, and some of them will eventually come from a file.
function interpolate(str, vars) {
  if (!vars) return str;
  return String(str).replace(/\{(\w+)\}/g, (whole, key) =>
    Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : whole,
  );
}

/** Builds a lookup bound to one module's dictionary.
 *
 *  Missing keys fall back to English and then to the key itself rather than
 *  rendering `undefined` — a missing translation should look like an
 *  untranslated label, not like a bug in the game.
 */
export function makeT(dict) {
  return function t(key, vars) {
    const table = dict[current] || dict.en || {};
    const raw = Object.prototype.hasOwnProperty.call(table, key)
      ? table[key]
      : (dict.en && dict.en[key]) !== undefined
        ? dict.en[key]
        : key;
    if (typeof raw === 'function') return raw(vars || {});
    return interpolate(raw, vars);
  };
}

/** FR treats 0 as singular ("0 indice"), EN does not ("0 nudges"). */
export function plural(n, one, other) {
  const isSingular = current === 'fr' ? Math.abs(n) < 2 : Math.abs(n) === 1;
  return isSingular ? one : other;
}
