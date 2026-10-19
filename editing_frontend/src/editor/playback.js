import { useEffect, useState, useSyncExternalStore } from "react";

// The playback clock. Lives outside React state so playing at 60 fps does not re-render the editor:
// components subscribe to ticks and update the DOM they own directly.
export class Playback {
  constructor() {
    this.t = 0;
    this.playing = false;
    this.rate = 1;
    this.duration = 0;
    this.fps = 30;
    this.muted = false;
    this.tickers = new Set();
    this.stateListeners = new Set();
    this._raf = 0;
    this._last = 0;
    this._snapshot = { playing: false, rate: 1, muted: false, v: 0 };
    this._loop = this._loop.bind(this);
  }

  subscribe(fn) {
    this.tickers.add(fn);
    return () => this.tickers.delete(fn);
  }

  subscribeState = (fn) => {
    this.stateListeners.add(fn);
    return () => this.stateListeners.delete(fn);
  };

  getSnapshot = () => this._snapshot;

  _emitState() {
    this._snapshot = { playing: this.playing, rate: this.rate, muted: this.muted, v: this._snapshot.v + 1 };
    for (const fn of this.stateListeners) fn();
  }

  _emit() {
    for (const fn of this.tickers) fn(this.t, this.playing);
  }

  configure(duration, fps) {
    this.duration = Math.max(0, duration);
    this.fps = fps || 30;
    if (this.t > this.duration) this.seek(this.duration);
  }

  seek(t) {
    const next = Math.max(0, Math.min(this.duration || 0, t));
    this.t = next;
    this._emit();
  }

  play(rate = 1) {
    if (this.duration <= 0) return;
    if (this.t >= this.duration - 1) this.t = 0;
    this.rate = rate;
    if (!this.playing) {
      this.playing = true;
      this._last = performance.now();
      this._raf = requestAnimationFrame(this._loop);
    }
    this._emitState();
    this._emit();
  }

  pause() {
    if (!this.playing && this.rate === 1) return;
    this.playing = false;
    this.rate = 1;
    cancelAnimationFrame(this._raf);
    this._emitState();
    this._emit();
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play(1);
  }

  setMuted(v) {
    this.muted = !!v;
    this._emitState();
    this._emit();
  }

  step(frames) {
    this.pause();
    const f = 1000 / this.fps;
    const idx = Math.round(this.t / f) + frames;
    this.seek(Math.round(idx * f));
  }

  _loop(now) {
    if (!this.playing) return;
    const dt = Math.min(100, now - this._last);
    this._last = now;
    this.t += dt * this.rate;
    if (this.t >= this.duration) {
      this.t = this.duration;
      this.playing = false;
      this.rate = 1;
      this._emitState();
      this._emit();
      return;
    }
    this._emit();
    this._raf = requestAnimationFrame(this._loop);
  }

  destroy() {
    cancelAnimationFrame(this._raf);
    this.playing = false;
    this.tickers.clear();
    this.stateListeners.clear();
  }
}

export function usePlaybackState(pb) {
  return useSyncExternalStore(pb.subscribeState, pb.getSnapshot);
}

// Current time as React state, throttled (for timecode readouts, inspector fields, the text view).
export function useTime(pb, intervalMs = 66) {
  const [t, setT] = useState(pb.t);
  useEffect(() => {
    let last = 0;
    let pending = 0;
    const unsub = pb.subscribe((time, playing) => {
      const now = performance.now();
      if (!playing || now - last >= intervalMs) {
        last = now;
        clearTimeout(pending);
        setT(time);
      } else {
        clearTimeout(pending);
        pending = setTimeout(() => { last = performance.now(); setT(pb.t); }, intervalMs);
      }
    });
    setT(pb.t);
    return () => { unsub(); clearTimeout(pending); };
  }, [pb, intervalMs]);
  return t;
}
