export const clampContactSheetValue = (value, low, high) => Math.max(low, Math.min(high, Number(value) || low));

export function contactSheetRequest(mode, count, width) {
  return {
    mode,
    count: Math.round(clampContactSheetValue(count, 2, 16)),
    width: Math.round(clampContactSheetValue(width, 128, 640)),
  };
}
