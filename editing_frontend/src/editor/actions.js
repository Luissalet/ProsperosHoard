import { useCallback, useMemo } from "react";
import { api } from "../api.js";
import { clipEnd, findClip, frameMs } from "./time.js";

// The clips copied with Ctrl+C / Ctrl+X: their full JSON, the kind of track they came from and that track's id.
let clipboard = null;
export const getClipboard = () => clipboard;

// Timeline actions shared by the toolbar, the inspector and the keyboard.
export function useActions(ed, { notify, fail, t, jobs }) {
  const { doc, edit, selection, setSelection, pb, reload, projectId } = ed;

  const selectedClips = useMemo(
    () => selection.ids.map((id) => findClip(doc, id)).filter(Boolean),
    [doc, selection.ids],
  );

  const addMedia = useCallback(async (mediaId, opts = {}) => {
    const op = { op: "add_media", media: mediaId };
    if (opts.track) op.track = opts.track;
    if (opts.at !== undefined) { op.at = Math.round(opts.at); op.mode = opts.mode || "overwrite"; }
    const res = await edit([op], t("lbl_add_clip"));
    const clip = res?.results?.[0]?.clip;
    if (clip) setSelection({ ids: [clip], track: null });
    return res;
  }, [edit, setSelection, t]);

  const split = useCallback(async () => {
    const at = Math.round(pb.t);
    let ops;
    if (selectedClips.length) {
      ops = selectedClips.filter(({ clip, track }) => !track.locked && at > clip.start + 20 && at < clipEnd(clip) - 20).map(({ clip }) => ({ op: "split", at, clip: clip.id }));
      if (!ops.length) { notify(t("split_outside"), "error"); return null; }
    } else {
      ops = [{ op: "split", at }];
    }
    return edit(ops, t("lbl_split"));
  }, [edit, pb, selectedClips, notify, t]);

  const remove = useCallback(async (ripple = true) => {
    if (!selection.ids.length) return null;
    const res = await edit([{ op: "delete", clips: selection.ids, ripple }], ripple ? t("lbl_delete") : t("lbl_delete_gap"));
    setSelection({ ids: [], track: null });
    return res;
  }, [edit, selection.ids, setSelection, t]);

  const duplicate = useCallback(async () => {
    if (!selection.ids.length) return null;
    const res = await edit(selection.ids.map((id) => ({ op: "duplicate", clip: id })), t("lbl_duplicate"));
    const ids = (res?.results || []).map((r) => r.clip).filter(Boolean);
    if (ids.length) setSelection({ ids, track: null });
    return res;
  }, [edit, selection.ids, setSelection, t]);

  const closeGaps = useCallback(() => edit([{ op: "close_gaps", ...(selection.track ? { track: selection.track } : {}) }], t("lbl_close_gaps")), [edit, selection.track, t]);

  const addMarker = useCallback(() => edit([{ op: "marker_add", t: Math.round(pb.t), label: "" }], t("lbl_marker")), [edit, pb, t]);

  const detachAudio = useCallback(() => (selectedClips[0] ? edit([{ op: "detach_audio", clip: selectedClips[0].clip.id }], t("lbl_detach")) : null), [edit, selectedClips, t]);

  const freeze = useCallback(async () => {
    const sc = selectedClips[0];
    if (!sc) return;
    const at = Math.round(pb.t);
    if (!(at > sc.clip.start && at < clipEnd(sc.clip))) { notify(t("split_outside"), "error"); return; }
    try {
      await api.freeze(projectId, { clip: sc.clip.id, at: Math.round(pb.t), length: 2000 });
      await reload();
    } catch (e) { fail(e); }
  }, [selectedClips, projectId, pb, reload, fail, notify, t]);

  const stabilize = useCallback(async () => {
    const sc = selectedClips[0];
    if (!sc) return;
    try {
      const job = await api.stabilize(projectId, { clip: sc.clip.id });
      notify(t("stabilize_started"));
      jobs.poke();
      const jid = job?.job || job?.id;
      if (jid) jobs.watch(jid).then(() => reload());
    } catch (e) { fail(e); }
  }, [selectedClips, projectId, notify, t, jobs, reload, fail]);

  const copy = useCallback(() => {
    if (!selectedClips.length) return false;
    const sorted = [...selectedClips].sort((a, b) => a.clip.start - b.clip.start);
    clipboard = { clips: JSON.parse(JSON.stringify(sorted.map((x) => x.clip))), kind: sorted[0].track.kind, track: sorted[0].track.id };
    notify(t("copied_n", { n: clipboard.clips.length }), "ok");
    return true;
  }, [selectedClips, notify, t]);

  const cut = useCallback(async () => {
    if (!copy()) return null;
    return remove(true);
  }, [copy, remove]);

  const paste = useCallback(async () => {
    if (!clipboard) { notify(t("paste_empty"), "error"); return null; }
    const sel = selection.track ? doc.tracks.find((tr) => tr.id === selection.track) : null;
    const same = (tr) => tr && !tr.locked && tr.kind === clipboard.kind;
    const target = same(sel) ? sel : same(doc.tracks.find((tr) => tr.id === clipboard.track)) ? doc.tracks.find((tr) => tr.id === clipboard.track) : doc.tracks.find((tr) => same(tr));
    const op = { op: "insert_clips", clips: clipboard.clips, at: Math.round(pb.t), mode: "overwrite", ...(target ? { track: target.id } : {}) };
    const res = await edit([op], t("lbl_paste"));
    const ids = res?.results?.[0]?.clips;
    if (ids?.length) { setSelection({ ids, track: null }); notify(t("pasted_n", { n: ids.length }), "ok"); }
    return res;
  }, [doc.tracks, selection.track, pb, edit, setSelection, notify, t]);

  const nudge = useCallback((frames) => pb.seek(pb.t + frames * frameMs(doc.canvas.fps)), [pb, doc.canvas.fps]);

  return { selectedClips, addMedia, split, remove, duplicate, closeGaps, addMarker, detachAudio, freeze, stabilize, nudge, copy, cut, paste };
}
