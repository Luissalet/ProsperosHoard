// .cube LUT files -> 8-bit RGBA data for a 3D texture (red varies fastest, then green, then blue: the file's own order).
export function parseCube(text) {
  let size = 0;
  let dmin = [0, 0, 0];
  let dmax = [1, 1, 1];
  const values = [];
  for (const raw of String(text || "").split(/\r?\n/)) {
    const line = raw.replace(/#.*/, "").trim();
    if (!line) continue;
    const parts = line.split(/\s+/);
    const key = parts[0].toUpperCase();
    if (key === "LUT_3D_SIZE") size = parseInt(parts[1], 10);
    else if (key === "LUT_1D_SIZE") return null; // 1D tables are not drawn by the preview
    else if (key === "DOMAIN_MIN") dmin = parts.slice(1, 4).map(Number);
    else if (key === "DOMAIN_MAX") dmax = parts.slice(1, 4).map(Number);
    else if (/^[-+]?[\d.]/.test(parts[0]) && parts.length >= 3) values.push(Number(parts[0]), Number(parts[1]), Number(parts[2]));
  }
  if (!size || size < 2 || size > 128 || values.length !== size * size * size * 3) return null;
  const data = new Uint8Array(size * size * size * 4);
  for (let i = 0, n = size * size * size; i < n; i++) {
    for (let c = 0; c < 3; c++) data[i * 4 + c] = Math.max(0, Math.min(255, Math.round(values[i * 3 + c] * 255)));
    data[i * 4 + 3] = 255;
  }
  return { size, dmin, dmax, data };
}

// Fetched once per path; resolves to the parsed LUT or null when the file cannot be used.
const cache = new Map();
export function lutFor(path, fetchText) {
  if (!cache.has(path)) {
    const entry = { state: "loading", lut: null, waiters: new Set() };
    cache.set(path, entry);
    Promise.resolve()
      .then(() => fetchText(path))
      .then((text) => { entry.lut = parseCube(text); entry.state = entry.lut ? "ready" : "unsupported"; })
      .catch(() => { entry.state = "error"; })
      .finally(() => { for (const fn of entry.waiters) fn(); entry.waiters.clear(); });
  }
  return cache.get(path);
}
