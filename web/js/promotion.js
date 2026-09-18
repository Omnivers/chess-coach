// Shared inline promotion picker for play.js and drills.js. chessground only
// hands back orig/dest on a user move, so a pawn reaching the last rank has
// to pause here for a piece choice before the UCI is built.

import { makeT } from './i18n.js';
import { strings } from './strings/promotion.js';

const t = makeT(strings);

export function askPromotion(pickerEl, color) {
  return new Promise((resolve) => {
    const glyphs = color === 'w'
      ? { q: '♕', r: '♖', b: '♗', n: '♘' }
      : { q: '♛', r: '♜', b: '♝', n: '♞' };
    pickerEl.hidden = false;
    pickerEl.innerHTML = '';
    pickerEl.style.cssText = 'display:flex;gap:.5em;justify-content:center;margin-top:.5em;';
    const finish = (choice) => {
      pickerEl.hidden = true;
      pickerEl.innerHTML = '';
      resolve(choice);
    };
    for (const p of Object.keys(glyphs)) {
      const btn = document.createElement('button');
      btn.textContent = glyphs[p];
      btn.setAttribute('aria-label', t('promoteTo', { piece: t('piece_' + p) }));
      btn.addEventListener('click', () => finish(p));
      pickerEl.appendChild(btn);
    }
    const cancel = document.createElement('button');
    cancel.textContent = t('cancel');
    cancel.addEventListener('click', () => finish(null));
    pickerEl.appendChild(cancel);
  });
}
