// The embedded Hermes chat — persistent across every view. Renders only via
// textContent (the SSE stream is untrusted model output). When the coach is
// unavailable it says so plainly and disables the composer: no fake replies,
// no fake "thinking" state, no retry loop.

import { api, ApiError, streamChat } from './api.js';
import { getState, setState } from './state.js';

let els = {};
let messages = [];
let pinned = true;
let sending = false;

export function mount(root) {
  root.id = 'chat-panel';
  root.dataset.collapsed = String(getState().chat.collapsed);
  root.innerHTML = `
    <div class="chat-head">
      <span class="chat-title"><span class="dot" id="chat-dot"></span>Hermes</span>
      <button id="chat-toggle" type="button" aria-expanded="true" aria-controls="chat-log">⇕</button>
    </div>
    <div class="chat-log" id="chat-log" role="log" aria-live="polite" aria-label="Chat with Hermes"></div>
    <form class="chat-composer" id="chat-form">
      <label class="visually-hidden" for="chat-input">Message Hermes</label>
      <textarea id="chat-input" rows="1" placeholder="Ask Hermes…" disabled></textarea>
      <button type="submit" id="chat-send" disabled>Send</button>
    </form>
  `;
  els = {
    panel: root,
    dot: root.querySelector('#chat-dot'),
    toggle: root.querySelector('#chat-toggle'),
    log: root.querySelector('#chat-log'),
    form: root.querySelector('#chat-form'),
    input: root.querySelector('#chat-input'),
    send: root.querySelector('#chat-send'),
  };

  els.toggle.addEventListener('click', () => {
    const collapsed = els.panel.dataset.collapsed !== 'true';
    els.panel.dataset.collapsed = String(collapsed);
    els.toggle.setAttribute('aria-expanded', String(!collapsed));
    setState({ chat: { ...getState().chat, collapsed } });
  });

  els.log.addEventListener('scroll', () => {
    pinned = els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 24;
  });

  els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      els.form.requestSubmit();
    }
  });
  els.input.addEventListener('input', () => {
    els.input.style.height = 'auto';
    els.input.style.height = `${Math.min(els.input.scrollHeight, 128)}px`;
  });

  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    send();
  });

  checkStatus();
}

export function unmount() {
  els = {};
  messages = [];
}

async function checkStatus() {
  try {
    const status = await api.get('/coach/status');
    setState({ chat: { ...getState().chat, available: status.available, reason: status.reason || '' } });
    if (status.available) {
      els.dot.classList.add('online');
      els.input.disabled = false;
      els.send.disabled = false;
    } else {
      els.dot.classList.add('offline');
      addNotice(status.reason || 'Hermes is not available right now.');
    }
  } catch (err) {
    els.dot.classList.add('offline');
    addNotice(`Could not reach the coach service: ${err.message}`);
  }
}

function addNotice(text) {
  const notice = document.createElement('div');
  notice.className = 'chat-notice';
  notice.textContent = text;
  els.log.appendChild(notice);
  scrollIfPinned();
}

function addLine(role, text) {
  const line = document.createElement('div');
  line.className = `chat-line ${role}`;
  const roleEl = document.createElement('span');
  roleEl.className = 'role';
  roleEl.textContent = role === 'user' ? 'you' : 'hermes';
  const textEl = document.createElement('span');
  textEl.className = 'text';
  textEl.textContent = text;
  line.append(roleEl, textEl);
  els.log.appendChild(line);
  scrollIfPinned();
  return textEl;
}

function scrollIfPinned() {
  if (pinned) els.log.scrollTop = els.log.scrollHeight;
}

async function send() {
  if (sending) return;
  const text = els.input.value.trim();
  if (!text) return;
  sending = true;
  els.input.value = '';
  els.input.style.height = 'auto';
  els.input.disabled = true;
  els.send.disabled = true;

  messages.push({ role: 'user', content: text });
  addLine('user', text);

  const gameId = getState().play.gameId || null;
  const assistantEl = addLine('assistant', '');
  let acc = '';
  try {
    await streamChat(messages, gameId, (delta) => {
      acc += delta;
      assistantEl.textContent = acc;
      scrollIfPinned();
    });
    if (acc) messages.push({ role: 'assistant', content: acc });
    else assistantEl.textContent = '(no response)';
  } catch (err) {
    const message = err instanceof ApiError ? err.message : String(err.message || err);
    assistantEl.textContent = '';
    assistantEl.parentElement.remove();
    addNotice(`Message failed: ${message}`);
  } finally {
    sending = false;
    els.input.disabled = false;
    els.send.disabled = false;
    els.input.focus();
  }
}
