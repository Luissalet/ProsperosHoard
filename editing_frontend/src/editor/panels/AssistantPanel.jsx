import React, { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { useApp } from "../../App.jsx";
import { Bar, Collapsible, Icon, Spinner, Toggle } from "../../components/ui.jsx";
import { useEd } from "../EditorContext.js";
import { PanelHead } from "./Shared.jsx";
import { useCopilotForm, useCopilotRefresh } from "../../copilot-hooks.js";

const EXAMPLES = ["ex_silences", "ex_vertical", "ex_fillers", "ex_trim5", "ex_beat"];

function StepList({ plan, steps, setSteps, disabled }) {
  const { t } = useApp();
  return (
    <div>
      {steps.map((s, i) => (
        <div key={s.id || i} className={`step${s.enabled === false ? " off" : ""}`}>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <input type="checkbox" checked={s.enabled !== false} disabled={disabled} aria-label={s.name} onChange={(e) => setSteps(steps.map((x, j) => (j === i ? { ...x, enabled: e.target.checked } : x)))} style={{ marginTop: 3 }} />
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                <b style={{ fontSize: 12.5 }}>{i + 1}. {s.name}</b>
                <span className={`chip ${s.kind === "export" ? "chip-warn" : s.kind === "command" ? "chip-info" : ""}`}>{t(`step_${s.kind}`)}</span>
              </div>
              {s.explain ? <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{s.explain}</div> : null}
              <Collapsible title={t("step_args")}>
                <pre className="mono" style={{ margin: "4px 0 0", padding: 6, background: "var(--field)", borderRadius: 5, overflow: "auto", maxHeight: 140, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{JSON.stringify(s.args, null, 2)}</pre>
              </Collapsible>
            </div>
          </div>
        </div>
      ))}
      {plan.notes ? <div className="muted" style={{ fontSize: 12, margin: "8px 0" }}>{t("plan_notes")}: {plan.notes}</div> : null}
    </div>
  );
}

function brief(v) {
  if (v === null || v === undefined || typeof v === "object") return null;
  return String(v);
}

function StepResult({ s }) {
  if (typeof s === "string") return <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>✓ {s}</div>;
  const facts = Object.entries(s.result || {}).map(([k, v]) => [k, brief(v)]).filter(([, v]) => v !== null && v !== "").slice(0, 6);
  return (
    <div style={{ fontSize: 12, marginTop: 5 }}>
      <div>✓ {s.explain || s.step || s.name}</div>
      {facts.length ? <div className="muted num" style={{ fontSize: 11, marginTop: 1 }}>{facts.map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ")}</div> : null}
    </div>
  );
}

export default function AssistantPanel() {
  const { t, fail, notify, jobs } = useApp();
  const ed = useEd();
  const [text, setText] = useState("");
  const [useModel, setUseModel] = useState(false);
  const [plan, setPlan] = useState(null);
  const [steps, setSteps] = useState([]);
  const [history, setHistory] = useState([]);
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState(null);
  const [result, setResult] = useState(null);
  useCopilotForm("edit-plan-draft", {
    initial: { instruction: "", use_model: false, steps: plan?.steps || [] },
    current: { instruction: text, use_model: useModel, steps },
  });

  const loadHistory = useCallback(async () => {
    try { setHistory((await api.plans(ed.projectId)).plans); } catch { /* the list is optional */ }
  }, [ed.projectId]);
  useEffect(() => { loadHistory(); }, [loadHistory]);
  useCopilotRefresh(loadHistory);

  const open = (p) => { setPlan(p); setSteps(p.steps); setResult(null); };

  const create = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setResult(null);
    try {
      const p = await api.planCreate(ed.projectId, { instruction: text.trim(), use_model: useModel });
      open(p);
      loadHistory();
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const apply = async () => {
    setBusy(true);
    try {
      await api.planUpdate(plan.id, steps);
      const res = await api.planApply(plan.id);
      jobs.poke();
      setRunning({ progress: 0, detail: "" });
      const job = await jobs.watch(res.job, (j) => setRunning({ progress: j.progress, detail: j.detail }));
      setRunning(null);
      await ed.reload();
      if (job.state === "done") {
        const r = job.result || {};
        setResult(r);
        if (r.applied === false) notify(r.message || t("plan_not_applied"), "error");
        else notify(t("plan_applied", { n: (r.steps || []).length }), "ok");
        setPlan(null);
      } else fail(new Error(job.error || job.state));
      loadHistory();
    } catch (e) { fail(e); setRunning(null); } finally { setBusy(false); }
  };

  const discard = async () => {
    try { await api.planDiscard(plan.id); setPlan(null); loadHistory(); } catch (e) { fail(e); }
  };

  const enabled = steps.filter((s) => s.enabled !== false).length;

  return (
    <div className="ed-panel">
      <PanelHead title={t("tab_assistant")} />
      <div className="ed-panel-body">
        <textarea className="field" rows={4} value={text} placeholder={t("assistant_ph")} aria-label={t("assistant_ph")} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") create(); e.stopPropagation(); }} disabled={busy} />
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, margin: "8px 0" }}>
          {EXAMPLES.map((k) => <button key={k} type="button" className="chip" style={{ cursor: "pointer", height: "auto", padding: "3px 9px", whiteSpace: "normal", textAlign: "left" }} onClick={() => setText(t(k))}>{t(k)}</button>)}
        </div>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: 14 }}>
          <Toggle checked={useModel} onChange={setUseModel} label={t("use_model")} />
          <button type="button" className="btn btn-primary" disabled={busy || !text.trim()} onClick={create}>{busy && !plan ? <Spinner /> : <Icon name="sparkles" size={14} />}{t("plan_create")}</button>
        </div>

        {plan ? (
          <div className="panel" style={{ padding: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <h4 style={{ fontSize: 13 }}>{t("plan_steps")}</h4>
              <span className={`chip ${plan.source === "model" ? "chip-info" : ""}`}>{plan.source === "model" ? t("plan_src_model") : t("plan_src_rules")}</span>
            </div>
            <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>«{plan.instruction}»</div>
            {steps.length ? <StepList plan={plan} steps={steps} setSteps={setSteps} disabled={busy} /> : <div className="muted" style={{ padding: "8px 0", fontSize: 12.5 }}>{t("plan_empty")}{plan.notes ? ` ${plan.notes}` : ""}</div>}
            {running ? (
              <div style={{ margin: "8px 0" }}>
                <Bar value={running.progress} />
                <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>{t("plan_running")} {Math.round(running.progress * 100)}% {running.detail}</div>
              </div>
            ) : null}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 8 }}>
              <button type="button" className="btn" disabled={busy} onClick={discard}>{t("discard")}</button>
              <button type="button" className="btn btn-primary" disabled={busy || !enabled} onClick={apply}>{busy ? <Spinner /> : <Icon name="check" size={14} />}{t("apply")} ({enabled})</button>
            </div>
          </div>
        ) : null}

        {result ? (
          <div className="panel" style={{ padding: 10, marginTop: 10 }}>
            <b style={{ fontSize: 12.5 }}>{result.applied === false ? t("plan_not_applied") : t("plan_done")}</b>
            {result.message ? <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{result.message}</div> : null}
            {(result.steps || []).map((s, i) => <StepResult key={i} s={s} />)}
            {(result.renders || []).length ? <div className="chip chip-info" style={{ marginTop: 6 }}>{t("plan_renders", { n: result.renders.length })}</div> : null}
          </div>
        ) : null}

        {history.length ? (
          <>
            <h4 className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", margin: "16px 0 8px" }}>{t("plan_history")}</h4>
            {history.map((p) => (
              <button key={p.id} type="button" className="tile" style={{ display: "block", width: "100%", marginBottom: 6 }} onClick={() => p.state === "draft" && open(p)} disabled={p.state !== "draft"}>
                <div className="ellipsis" style={{ fontSize: 12.5 }}>{p.instruction}</div>
                <div style={{ display: "flex", gap: 6, marginTop: 3 }}>
                  <span className={`chip ${p.state === "applied" ? "chip-ok" : p.state === "discarded" ? "" : "chip-warn"}`}>{t(`plan_state_${p.state}`)}</span>
                  <span className="muted" style={{ fontSize: 11 }}>{t("n_steps", { n: p.steps.length })}</span>
                </div>
              </button>
            ))}
          </>
        ) : null}
      </div>
    </div>
  );
}
