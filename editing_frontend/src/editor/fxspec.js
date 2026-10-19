// Parameter ranges of the effects (mirrors the render's filter specs): [min, max, step] or "color" / "text".
export const FX_RANGES = {
  eq: { brightness: [-1, 1, 0.01], contrast: [0, 3, 0.01], saturation: [0, 3, 0.01], gamma: [0.1, 5, 0.01] },
  vignette: { strength: [0, 1, 0.01] },
  blur: { radius: [0.5, 60, 0.5] },
  sharpen: { amount: [0, 3, 0.01] },
  denoise: { strength: [0, 20, 0.5] },
  chromakey: { color: "color", similarity: [0.01, 0.6, 0.01], blend: [0, 0.5, 0.01] },
  warm: { amount: [0, 1, 0.01] },
  cool: { amount: [0, 1, 0.01] },
  contrast_pop: { amount: [0, 1, 0.01] },
  pixelate: { size: [2, 128, 1] },
  lut: { file: "text" },
  audio_denoise: { strength: [1, 40, 1] },
  highpass: { hz: [20, 2000, 10] },
  lowpass: { hz: [500, 20000, 100] },
  compressor: { threshold_db: [-60, 0, 1], ratio: [1, 20, 0.5] },
  pitch: { semitones: [-12, 12, 0.5] },
  echo: { delay_ms: [20, 2000, 10], decay: [0, 0.9, 0.01] },
};

// Defaults of the picture effects (mirrors render/filters.py SPECS): the GL preview fills the parameters a clip does not carry.
export const FX_DEFAULTS = {
  eq: { brightness: 0, contrast: 1, saturation: 1, gamma: 1 },
  lut: { file: "" },
  grayscale: {},
  sepia: {},
  vignette: { strength: 0.5 },
  blur: { radius: 6 },
  sharpen: { amount: 1 },
  denoise: { strength: 4 },
  hflip: {},
  vflip: {},
  chromakey: { color: "#00FF00", similarity: 0.12, blend: 0.05 },
  vintage: {},
  warm: { amount: 0.5 },
  cool: { amount: 0.5 },
  contrast_pop: { amount: 0.5 },
  pixelate: { size: 16 },
};

export const AUDIO_FX = new Set(["audio_denoise", "voice_enhance", "highpass", "lowpass", "compressor", "pitch", "echo"]);

export const FONTS = ["Arial", "Arial Black", "Verdana", "Tahoma", "Trebuchet MS", "Georgia", "Times New Roman", "Impact", "Courier New", "Comic Sans MS", "Segoe UI"];

export const TEXT_PRESETS = {
  title: { size: 120, bold: true, position: "middle", animation: "pop", outline_width: 5, margin: 80 },
  subtitle: { size: 64, bold: false, position: "middle", animation: "fade", outline_width: 3, margin: 80 },
  lower: { size: 52, bold: true, position: "bottom", align: "left", animation: "slide_up", box: "#000000B3", outline_width: 0, margin: 120 },
  cta: { size: 76, bold: true, position: "bottom", animation: "pop", color: "#0A0A0A", box: "#FFD400", outline_width: 0, margin: 160 },
};
