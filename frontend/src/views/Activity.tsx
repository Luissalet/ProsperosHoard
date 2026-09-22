import { useEffect } from "react";
import { Bot, CheckCircle2, XCircle } from "lucide-react";
import { api } from "../api";
import { useT } from "../i18n";
import { Empty, timeAgo, useAsync } from "../components/ui";

export function ActivityView() {
  const { t, lang } = useT();
  const calls = useAsync(() => api.agentCalls(150), []);
  useEffect(() => {
    const id = setInterval(calls.reload, 4000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const items = calls.data?.items || [];
  return (
    <>
      <div className="page-head"><div><h1>{t("activityTitle")}</h1><p>{t("activityLead")}</p></div></div>
      {items.length === 0 ? <Empty icon={<Bot size={34} />} text={t("noActivity")} /> : (
        <div className="card" style={{ padding: 6 }}>
          <table className="list">
            <thead><tr><th /><th>{t("tool")}</th><th style={{ width: "46%" }}>{t("args")}</th><th>{t("ms")}</th><th>{t("when")}</th></tr></thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id}>
                  <td>{c.ok ? <CheckCircle2 size={16} color="var(--ok)" /> : <XCircle size={16} color="var(--bad)" />}</td>
                  <td className="mono small"><strong>{c.tool}</strong></td>
                  <td className="small">
                    <span className="mono">{c.args_summary}</span>
                    {c.error && <div className="err-text">{c.error}</div>}
                  </td>
                  <td className="mono small">{Math.round(c.duration_ms)}</td>
                  <td className="small muted nowrap" title={c.created_at}>{timeAgo(c.created_at, lang)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
