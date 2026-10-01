import { useState } from "react";
import { Download, Loader2 } from "lucide-react";
import { api, type Asset } from "../api";
import { useT } from "../i18n";
import { useApp } from "./ui";

const seconds = (text: string): number | null => {
  const v = text.trim();
  if (!v) return null;
  const parts = v.split(":").map(Number);
  if (parts.some((n) => Number.isNaN(n))) return null;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
};

/** Paste a YouTube / X / Instagram link: the video (or a section of it)
 * downloads into the project's library as an mp4. */
export function LinkDownload({ projectId, onDone, compact = false }: {
  projectId: string; onDone: (asset: Asset) => void; compact?: boolean;
}) {
  const { t } = useT();
  const app = useApp();
  const [url, setUrl] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [audio, setAudio] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const go = async () => {
    const start = seconds(from);
    const end = seconds(to);
    setBusy(t("dlQueued"));
    try {
      const { job } = await api.downloadMedia(projectId, { url: url.trim(), audio_only: audio, start_s: start, end_s: end });
      app.refreshJobs();
      for (;;) {
        await new Promise((r) => setTimeout(r, 1500));
        const j = await api.job(job.id);
        if (j.state === "done") {
          const id = (j.outputs?.asset_id as string) || (j.outputs?.asset_ids || [])[0];
          const asset = await api.asset(id);
          app.bump();
          app.toast(`${t("saved")}: ${asset.name}`, "ok");
          setUrl(""); setFrom(""); setTo("");
          onDone(asset);
          break;
        }
        if (j.state === "failed" || j.state === "cancelled") throw new Error(j.message || j.state);
        setBusy(j.message || t("dlWorking"));
      }
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="row wrap" style={{ gap: 6 }}>
        <input className="grow" style={{ minWidth: 220 }} value={url} onChange={(e) => setUrl(e.target.value)} placeholder={t("dlPlaceholder")}
          disabled={!!busy} onKeyDown={(e) => { if (e.key === "Enter" && url.trim() && !busy) go(); }} />
        <input style={{ width: 74 }} value={from} onChange={(e) => setFrom(e.target.value)} placeholder={t("dlFrom")} disabled={!!busy} title={t("dlRangeHint")} />
        <input style={{ width: 74 }} value={to} onChange={(e) => setTo(e.target.value)} placeholder={t("dlTo")} disabled={!!busy} title={t("dlRangeHint")} />
        {!compact && <label className="check"><input type="checkbox" checked={audio} onChange={(e) => setAudio(e.target.checked)} disabled={!!busy} /> {t("dlAudio")}</label>}
        <button type="button" className="btn sm primary" disabled={!url.trim() || !!busy} onClick={go}>
          {busy ? <Loader2 size={13} className="spin" /> : <Download size={13} />} {t("dlGo")}</button>
      </div>
      {busy && <span className="hint">{busy}</span>}
    </div>
  );
}
