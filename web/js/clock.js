// Dual clock — display only. The server is authoritative on time; this
// ticker just interpolates locally between server responses so the clock
// doesn't look frozen while the engine thinks, and resyncs on every
// `sync()` call (i.e. every move response).

export class ClockTicker {
  constructor(onTick) {
    this.onTick = onTick;
    this.whiteMs = null;
    this.blackMs = null;
    this.turn = null; // 'white' | 'black' | null (paused / no clock)
    this._timer = null;
    this._last = 0;
  }

  sync({ white_ms, black_ms, turn }) {
    this.whiteMs = white_ms ?? null;
    this.blackMs = black_ms ?? null;
    this.turn = turn ?? null;
    this._last = performance.now();
    this._emit();
  }

  start() {
    if (this._timer) return;
    this._last = performance.now();
    this._timer = setInterval(() => this._tick(), 250);
  }

  stop() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  }

  _tick() {
    const now = performance.now();
    const dt = now - this._last;
    this._last = now;
    if (this.turn === 'white' && this.whiteMs !== null) {
      this.whiteMs = Math.max(0, this.whiteMs - dt);
    } else if (this.turn === 'black' && this.blackMs !== null) {
      this.blackMs = Math.max(0, this.blackMs - dt);
    }
    this._emit();
  }

  _emit() {
    this.onTick({ white_ms: this.whiteMs, black_ms: this.blackMs });
  }
}
