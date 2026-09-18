// Labels for the "kind" values util.js turns into readable text:
// highlight kinds (chess_coach/highlights.py), mistake motifs
// (chess_coach/analysis.py DETECTABLE_MOTIFS), and severity keys
// ('blunder' | 'mistake' | 'inaccuracy'). Keyed by the raw snake_case value
// so prettyHighlight can look a kind up directly with t(kind).
export const strings = {
  fr: {
    // highlight kinds
    only_move: 'seul coup',
    found_tactic: 'tactique repérée',
    resisted: 'résistance',
    converted: 'conversion réussie',
    best_under_pressure: 'sang-froid',
    // mistake motifs
    hung_piece: 'pièce en prise',
    fork: 'fourchette',
    pin: 'clouage',
    skewer: 'enfilade',
    back_rank: 'rangée arrière',
    discovered_attack: 'attaque à la découverte',
    missed_fork: 'fourchette manquée',
    missed_pin: 'clouage manqué',
    // severity keys (label only — the key itself stays untranslated elsewhere)
    blunder: 'gaffe',
    mistake: 'erreur',
    inaccuracy: 'imprécision',
  },
  en: {
    only_move: 'only move',
    found_tactic: 'found tactic',
    resisted: 'resisted',
    converted: 'converted',
    best_under_pressure: 'best under pressure',
    hung_piece: 'hung piece',
    fork: 'fork',
    pin: 'pin',
    skewer: 'skewer',
    back_rank: 'back rank',
    discovered_attack: 'discovered attack',
    missed_fork: 'missed fork',
    missed_pin: 'missed pin',
    blunder: 'blunder',
    mistake: 'mistake',
    inaccuracy: 'inaccuracy',
  },
};
