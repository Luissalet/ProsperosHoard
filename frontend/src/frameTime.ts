/** Keep the playhead's precision; an ended player presents the final interval. */
export function frameTimeAtPlayhead(time: number, duration: number): number {
  if (!Number.isFinite(time) || time < 0) return 0;
  if (!Number.isFinite(duration) || duration <= 0 || time < duration) return time;
  // Adjacent IEEE-754 value below the end, independent of the video's FPS.
  const bits = new DataView(new ArrayBuffer(8));
  bits.setFloat64(0, duration);
  bits.setBigUint64(0, bits.getBigUint64(0) - 1n);
  return bits.getFloat64(0);
}
