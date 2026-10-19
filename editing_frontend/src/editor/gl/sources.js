// The live <video> / <img> elements the GL preview reads from, keyed by clip id. The elements themselves are owned by
// ClipLayer (it keeps them in step with the playback clock); this only remembers them and wakes the renderer up when a
// new picture is ready (a seek landed, a frame was presented, an image loaded).
const EVENTS = ["seeked", "loadeddata", "loadedmetadata", "canplay", "playing", "load"];

export class SourceRegistry {
  constructor() {
    this.els = new Map();
    this.notify = () => {};
    this.set = this.set.bind(this);
  }

  set(id, el) {
    const prev = this.els.get(id);
    if (prev === el || (!el && !prev)) return;
    if (prev) this.off(prev);
    if (el) {
      this.els.set(id, el);
      this.on(el);
    } else this.els.delete(id);
    this.notify();
  }

  get(id) {
    return this.els.get(id);
  }

  on(el) {
    const wake = () => this.notify();
    EVENTS.forEach((e) => el.addEventListener(e, wake));
    let handle = 0;
    if (el.requestVideoFrameCallback) {
      // fires for every frame the decoder presents: that is when a texture upload is worth doing
      const cb = (_now, meta) => {
        el.__glFrame = `${meta.mediaTime}:${meta.presentedFrames}`;
        wake();
        handle = el.requestVideoFrameCallback(cb);
      };
      handle = el.requestVideoFrameCallback(cb);
    }
    el.__glOff = () => {
      EVENTS.forEach((e) => el.removeEventListener(e, wake));
      if (handle && el.cancelVideoFrameCallback) el.cancelVideoFrameCallback(handle);
      delete el.__glFrame;
    };
  }

  off(el) {
    if (el.__glOff) el.__glOff();
    delete el.__glOff;
  }
}
