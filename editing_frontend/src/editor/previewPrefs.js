import { useSyncExternalStore } from "react";
import { glSupported } from "./gl/compositor.js";

// Preview preferences kept in this browser (they describe the screen, not the project). "Vista previa acelerada (WebGL)":
// on by default, and the editor falls back to the CSS preview by itself when the browser has no WebGL2.
const KEY = "lumiere.preview.gl";
const listeners = new Set();
let supported;

function read() {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
}

export function webglAvailable() {
  if (supported === undefined) supported = glSupported();
  return supported;
}

export function setGlPreview(on) {
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    // private window: the choice only lasts until the page is reloaded
  }
  current = on;
  listeners.forEach((fn) => fn());
}

let current = read();
const subscribe = (fn) => {
  listeners.add(fn);
  return () => listeners.delete(fn);
};

export function useGlPreview() {
  return useSyncExternalStore(subscribe, () => current);
}
