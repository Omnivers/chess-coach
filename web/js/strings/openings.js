// Openings primer strings: chrome only. Opening/line/principle titles, ply
// notes and SAN all arrive from `GET /openings/primer` and are rendered
// as-is — see i18n-contract for why those stay untranslated.
export const strings = {
  fr: {
    heading: 'Comment débuter une partie',
    intro: 'Voici les quelques principes à connaître avant de jouer un coup, ainsi qu’une ligne modèle par situation pour les voir en action. Ce n’est pas un répertoire à mémoriser — une fois que vous aurez des parties réelles enregistrées, votre travail d’ouverture personnel sera construit à partir de celles-ci.',
    chooseLineAria: 'Choisir une ligne',
    boardAria: 'Échiquier de la ligne d’ouverture',
    thisMoveHeading: 'Ce coup',
    theLineHeading: 'La ligne',
    movesAria: 'Coups',
    principlesHeading: 'Principes',
    movesAndPrinciplesAria: 'Coups et principes',
    loadError: (vars) => `Impossible de charger le guide d’ouvertures : ${vars.message}`,
    startingPosition: 'Position de départ',
    startingNote: 'La position de départ. Choisissez une ligne ci-dessus, puis avancez coup par coup.',
    naturalReply: 'Une réponse naturelle.',
  },
  en: {
    heading: 'How to start a game',
    intro: "These are the handful of principles worth knowing before you play a move, plus one model line per situation so you can see them in action. This isn't a repertoire to memorise — once you have real games logged, your own opening work will be built from those instead.",
    chooseLineAria: 'Choose a line',
    boardAria: 'Opening line board',
    thisMoveHeading: 'This move',
    theLineHeading: 'The line',
    movesAria: 'Moves',
    principlesHeading: 'Principles',
    movesAndPrinciplesAria: 'Moves and principles',
    loadError: (vars) => `Could not load the opening primer: ${vars.message}`,
    startingPosition: 'Starting position',
    startingNote: 'The starting position. Pick a line above, then step through it.',
    naturalReply: 'A natural reply.',
  },
};
