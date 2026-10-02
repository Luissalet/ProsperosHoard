import { useState } from "react";
import { Loader2, Sparkles } from "lucide-react";
import { api, ApiError } from "../api";
import { useT } from "../i18n";
import { useApp } from "./ui";

/** "Improve" a prompt with the local language model (keeps @Name and <imageN> as written). */
export function Enhance({ text, kind, onDone, className = "btn xs ghost nodrag" }: {
  text: string; kind: "image" | "video" | "music"; onDone: (s: string) => void; className?: string;
}) {
  const { t } = useT();
  const app = useApp();
  const [busy, setBusy] = useState(false);
  return (
    <button className={className} disabled={!text.trim() || busy} title={t("spEnhanceHint")}
      onClick={async () => {
        setBusy(true);
        try { onDone((await api.enhancePrompt(text, kind, app.projectId || undefined)).text); } catch (e) {
          app.toast(e instanceof ApiError && e.code === "llm_unavailable" ? t("spNoLlm") : (e as Error).message, "bad");
        }
        finally { setBusy(false); }
      }}>
      {busy ? <Loader2 size={12} className="spin" /> : <Sparkles size={12} />} {t("spEnhance")}
    </button>
  );
}
