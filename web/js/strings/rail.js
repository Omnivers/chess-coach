// Strings for rail.js (the coaching rail). Coaching questions are written
// as natural coaching French, not literal translations of the English —
// see i18n-contract.md for the register this follows.
import { plural } from '../i18n.js';

export const strings = {
  fr: {
    processHeading: 'Avant de jouer',
    processLede: 'Reprenez cette liste à chaque tour. C’est l’habitude qui évite les gaffes, pas la théorie.',
    processSubhead: 'À se demander aussi',
    scanNote: 'Toujours dans cet ordre. À ce niveau, la plupart des parties se jouent sur un coup qui a ignoré l’un des trois.',

    scanChecksLabel: '1 · Échecs',
    scanChecksQuestion: 'Puis-je mettre son roi en échec ? Peut-il mettre le mien en échec au prochain coup ?',
    scanCapturesLabel: '2 · Prises',
    scanCapturesQuestion: 'Qu’est-ce qui peut être pris — par moi, et par lui ?',
    scanThreatsLabel: '3 · Menaces',
    scanThreatsQuestion: 'Qu’a attaqué son dernier coup ? Qu’est-ce que j’attaque ?',

    phaseOpening: 'Ouverture',
    phaseMiddlegame: 'Milieu de partie',
    phaseEndgame: 'Finale',

    promptOpening0: 'Ai-je sorti un cavalier ou un fou de la première rangée à ce coup ?',
    promptOpening1: 'Mon roi est-il encore au milieu ? Le roque le met à l’abri.',
    promptOpening2: 'Est-ce que je déplace deux fois la même pièce pendant qu’il développe de nouvelles pièces ?',
    promptMiddlegame0: 'Laquelle de mes pièces fait le moins ? C’est elle qui a besoin d’une meilleure case.',
    promptMiddlegame1: 'Est-ce qu’une de mes pièces est actuellement sans défense ?',
    promptMiddlegame2: 'Si je joue ce coup, qu’est-ce que ça laisse derrière ?',
    promptEndgame0: 'Quel roi est le plus proche de l’action ? Les rois sont des pièces combattantes maintenant.',
    promptEndgame1: 'Un de mes pions peut-il courir vers la dernière rangée et devenir dame ?',
    promptEndgame2: 'Échanger les pièces avantage celui qui mène. Est-ce moi ?',

    hintHeading: 'Bloqué ? Demandez un coup de pouce',
    hintLede: 'Quatre niveaux, d’une indication vague jusqu’au coup lui-même. Commencez en haut — le but est de le trouver seul.',
    hintCreditsLeft: (vars) => `${vars.n} ${plural(vars.n, 'coup de pouce restant', 'coups de pouce restants')} sur 6`,
    hintCreditNote: 'Six par partie, et ils ne reviennent pas — une partie où vous n’en utilisez aucun est une partie que vous avez jouée seul.',
    hintNotEnoughCredits: 'Plus assez de crédits d’indice pour cette partie.',
    hintRequestFailed: 'Échec de la demande d’indice : {message}',
    hintCostLabel: (vars) => `${vars.cost} ${plural(vars.cost, 'crédit', 'crédits')}`,

    tier1Name: 'Y a-t-il quelque chose ici ?',
    tier1Desc: 'Indique s’il y a une vraie idée dans cette position, et de quel côté de l’échiquier elle se trouve.',
    tier2Name: 'Quel genre d’idée ?',
    tier2Desc: 'Nomme le type de tactique à chercher — une fourchette, un clouage, une idée de première rangée.',
    tier3Name: 'Quelle pièce s’en charge ?',
    tier3Desc: 'Montre la pièce qui fait fonctionner l’idée et met en évidence sa case.',
    tier4Name: 'Montrez-moi le coup.',
    tier4Desc: 'Le coup lui-même, et pourquoi il fonctionne.',

    feedbackHeading: 'Votre dernier coup',
    feedbackLede: 'Évalué par le moteur, pas par le coach.',
    feedbackSound: 'solide',
  },
  en: {
    processHeading: 'Before you move',
    processLede: 'Run through this every single turn. The habit is what stops blunders — not knowing more theory.',
    processSubhead: 'Also worth asking',
    scanNote: 'Always in that order. Most games at this level are decided by a move that ignored one of the three.',

    scanChecksLabel: '1 · Checks',
    scanChecksQuestion: 'Can I check their king? Can they check mine next move?',
    scanCapturesLabel: '2 · Captures',
    scanCapturesQuestion: 'What can be taken — by me, and by them?',
    scanThreatsLabel: '3 · Threats',
    scanThreatsQuestion: 'What did their last move attack? What am I attacking?',

    phaseOpening: 'Opening',
    phaseMiddlegame: 'Middlegame',
    phaseEndgame: 'Endgame',

    promptOpening0: 'Have I got a knight or bishop off the back row this move?',
    promptOpening1: 'Is my king still sitting in the middle? Castling gets it out.',
    promptOpening2: "Am I moving the same piece twice while they're bringing out new ones?",
    promptMiddlegame0: 'Which of my pieces is doing the least? That one wants a better square.',
    promptMiddlegame1: 'Is anything of mine sitting undefended right now?',
    promptMiddlegame2: 'If I make this move, what does it leave behind?',
    promptEndgame0: 'Whose king is closer to the action? Kings are fighting pieces now.',
    promptEndgame1: 'Can one of my pawns run for the far end and become a queen?',
    promptEndgame2: "Trading pieces helps whoever's ahead. Is that me?",

    hintHeading: 'Stuck? Ask for a nudge',
    hintLede: 'Four levels, from a vague pointer to the actual move. Start at the top — the point is to find it yourself.',
    hintCreditsLeft: (vars) => `${vars.n} of 6 ${plural(vars.n, 'nudge', 'nudges')} left`,
    hintCreditNote: "Six per game, and they don't come back — so a game where you spend none is a game you played yourself.",
    hintNotEnoughCredits: 'Not enough hint credits left this game.',
    hintRequestFailed: 'Hint request failed: {message}',
    hintCostLabel: (vars) => `${vars.cost} ${plural(vars.cost, 'nudge', 'nudges')}`,

    tier1Name: 'Is there something here?',
    tier1Desc: "Says whether there's a real idea in this position, and which part of the board it's on.",
    tier2Name: 'What kind of idea?',
    tier2Desc: 'Names the type of tactic to hunt for — a fork, a pin, a back-rank idea.',
    tier3Name: 'Which piece does it?',
    tier3Desc: 'Points at the piece that makes it work and lights up its square.',
    tier4Name: 'Show me the move.',
    tier4Desc: 'The move itself, and why it works.',

    feedbackHeading: 'Your last move',
    feedbackLede: 'Scored by the engine, not by the coach.',
    feedbackSound: 'sound',
  },
};
