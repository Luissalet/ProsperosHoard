import { changedFields } from "./copilot-core.js";

export const SETTINGS_KEYS = ["whisper_model", "whisper_device", "transcript_language", "hwdec", "export_folder", "auto_transcribe", "model", "default_export", "face_detector", "notify.via"];

export const settingsFields = (settings) => Object.fromEntries(SETTINGS_KEYS.map((key) => [key, settings?.[key] ?? ""]));

// Refresh server values while retaining everything typed since the baseline.
// After Save, the submitted draft becomes that baseline, so later typing stays dirty.
export function mergeSettingsDraft(baseline, draft, settings) {
  return { ...settingsFields(settings), ...changedFields(baseline, draft) };
}
