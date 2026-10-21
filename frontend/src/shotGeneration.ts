/** The shot's saved outputs decide readiness; a production job may finish
 * without producing anything for this particular shot. */
export function shotGenerationStatus(frame: string | undefined, clip: string | undefined, wantsClip: boolean) {
  if (clip) return frame ? "ready_clip" : "missing_frame";
  if (!frame) return wantsClip ? "missing_frame_and_clip" : "missing_frame";
  return wantsClip ? "missing_clip" : "ready_still";
}
