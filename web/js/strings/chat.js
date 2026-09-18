// Strings for chat.js (the docked Hermes coach chat). Message content that
// comes back from the server is data and is never routed through here —
// only the panel chrome around it is translated.
export const strings = {
  fr: {
    logAriaLabel: 'Discussion avec Hermes',
    inputLabel: 'Message à Hermes',
    inputPlaceholder: 'Posez une question à Hermes…',
    send: 'Envoyer',
    unavailable: 'Hermes n’est pas disponible pour le moment.',
    unreachable: 'Impossible de joindre le service du coach : {message}',
    noResponse: '(pas de réponse)',
    sendFailed: 'Échec de l’envoi : {message}',
    roleYou: 'vous',
    roleCoach: 'hermes',
  },
  en: {
    logAriaLabel: 'Chat with Hermes',
    inputLabel: 'Message Hermes',
    inputPlaceholder: 'Ask Hermes…',
    send: 'Send',
    unavailable: 'Hermes is not available right now.',
    unreachable: 'Could not reach the coach service: {message}',
    noResponse: '(no response)',
    sendFailed: 'Message failed: {message}',
    roleYou: 'you',
    roleCoach: 'hermes',
  },
};
