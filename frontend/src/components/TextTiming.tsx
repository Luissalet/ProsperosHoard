import { useEffect, useRef, useState } from 'react';
import { Check, Loader2, Pause, Play, RotateCcw, Save, Timer, Wand2 } from 'lucide-react';
import { fileUrl } from '../api';
import { useT } from '../i18n';
import { Modal } from './ui';
import { stampNext, timingIssue, type TimingRow } from '../lyricTiming';
import './TextTiming.css';

type Alignment = { lines: { text: string; time_s: number | null; end_s: number | null; needs_review?: boolean }[]; note?: string };
const clock = (time: number) => `${Math.floor(time / 60)}:${(time % 60).toFixed(2).padStart(5, '0')}`;

export function TextTiming({ title, audioId, duration: initialDuration, initialRows, draftKey, disabled = false, onSave, onAlign, onClose }: {
  title: string; audioId: string; duration: number; initialRows: TimingRow[]; draftKey: string; disabled?: boolean;
  onSave: (rows: TimingRow[]) => Promise<void>; onAlign?: () => Promise<Alignment>; onClose: () => void;
}) {
  const { lang } = useT(), es = lang === 'es';
  const signature = JSON.stringify(initialRows.map(row => row.text));
  const storageKey = `prospero.timing.${draftKey}.${audioId}`;
  const [rows, setRows] = useState<TimingRow[]>(() => {
    try { const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); return saved?.text === signature ? saved.rows : initialRows; }
    catch { return initialRows; }
  });
  const [cursor, setCursor] = useState(0), [armed, setArmed] = useState(false);
  const [playing, setPlaying] = useState(false), [now, setNow] = useState(0), [duration, setDuration] = useState(initialDuration);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [dirty, setDirty] = useState(false);
  const audio = useRef<HTMLAudioElement>(null), surface = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!dirty) return;
    try { sessionStorage.setItem(storageKey, JSON.stringify({ text: signature, rows })); } catch { /* The open editor still retains the draft. */ }
  }, [dirty, rows, signature, storageKey]);
  const change = (index: number, patch: Partial<TimingRow>) => {
    setRows(value => value.map((row, i) => i === index ? { ...row, ...patch, needs_review: false } : row)); setDirty(true); setError('');
  };
  const mark = () => {
    if (!audio.current || !armed || busy || disabled) return;
    try {
      const result = stampNext(rows, cursor, audio.current.currentTime);
      setRows(result.rows); setCursor(result.next); setDirty(true); setError('');
      if (result.finished) { setArmed(false); audio.current.pause(); }
    } catch { setError(es ? 'La siguiente marca debe ir después del inicio de la frase anterior.' : 'The next mark must follow the preceding line start.'); }
  };
  const listen = async () => {
    if (!audio.current) return;
    if (playing) { audio.current.pause(); return; }
    setArmed(true); setError('');
    try { await audio.current.play(); surface.current?.focus(); }
    catch { setError(es ? 'No se pudo reproducir el audio. Prueba con sus controles.' : 'Audio could not play. Try its playback controls.'); }
  };
  const align = async () => {
    if (!onAlign) return;
    audio.current?.pause(); setArmed(false); setBusy(true); setError('');
    try {
      const result = await onAlign();
      if (result.lines.length !== rows.length) throw new Error(es ? 'El texto cambió durante la alineación. Vuelve a abrir el editor.' : 'Text changed during alignment. Reopen the editor.');
      const aligned = rows.map((row, index) => {
        const match = result.lines[index];
        if (!match || match.text !== row.text) throw new Error(es ? 'El texto cambió durante la alineación. Vuelve a abrir el editor.' : 'Text changed during alignment. Reopen the editor.');
        return { ...row, start_s: match.time_s, end_s: match.end_s, needs_review: match.needs_review };
      });
      setRows(aligned);
      setDirty(true); setCursor(0);
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  const save = async () => {
    const issue = timingIssue(rows, duration);
    if (issue) {
      const reason = issue.kind === 'incomplete' ? (es ? 'falta inicio o fin' : 'start or end is missing')
        : issue.kind === 'overlap' ? (es ? 'se solapa con la anterior' : 'overlaps the preceding line')
        : (es ? 'los tiempos deben estar ordenados y dentro del audio' : 'times must be ordered and inside the audio');
      setError(`${es ? 'Frase' : 'Line'} ${issue.index + 1}: ${reason}.`); return;
    }
    setBusy(true); setError(''); audio.current?.pause();
    try {
      await onSave(rows);
      try { sessionStorage.removeItem(storageKey); } catch { /* storage unavailable */ }
      onClose();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  const marked = rows.filter(row => row.start_s != null && row.end_s != null).length;
  return <Modal title={title} wide onClose={onClose} footer={<>
    <span className="grow small muted">{marked}/{rows.length} {es ? 'frases con tiempos' : 'timed lines'}</span>
    <button className="btn primary" disabled={busy || disabled || !marked} onClick={save}>
      {busy ? <Loader2 size={15} className="spin" /> : <Save size={15} />}{es ? 'Guardar tiempos y vincular' : 'Save and link timings'}
    </button></>}>
    <div className="text-timing" tabIndex={0} ref={surface} onKeyDown={event => {
      const target = event.target as HTMLElement;
      if (event.code === 'Space' && armed && !event.repeat && !target.closest('input,textarea,select,button,audio,[contenteditable=true]')) {
        event.preventDefault(); mark();
      }
    }}>
      <p className="small muted">{es ? 'Escucha y pulsa Espacio al empezar cada frase; pulsa una vez más al terminar la última. Puedes seleccionar una frase para rehacer sus marcas o escribir los segundos directamente.'
        : 'Listen and press Space as each line begins; press once more at the end of the final line. Select a line to redo its marks, or edit seconds directly.'}</p>
      <div className="timing-player">
        <audio ref={audio} src={fileUrl(audioId)} controls preload="metadata" onTimeUpdate={e => setNow(e.currentTarget.currentTime)}
          onLoadedMetadata={e => { if (Number.isFinite(e.currentTarget.duration)) setDuration(e.currentTarget.duration); }}
          onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => { setPlaying(false); setArmed(false); }} />
        <span className="mono small">{clock(now)} / {clock(duration)}</span>
        <label className="small">{es ? 'Velocidad' : 'Speed'} <select defaultValue="1" onChange={e => { if (audio.current) audio.current.playbackRate = Number(e.target.value); }}>
          <option value="0.5">0,5×</option><option value="0.75">0,75×</option><option value="1">1×</option>
        </select></label>
      </div>
      <div className="row wrap timing-actions">
        <button className="btn" disabled={busy || disabled} onClick={listen}>{playing ? <Pause size={15} /> : <Play size={15} />}{playing ? (es ? 'Pausar' : 'Pause') : (es ? 'Escuchar y marcar' : 'Listen and mark')}</button>
        <button className="btn primary" disabled={!armed || busy || disabled} onClick={mark}><Timer size={15} />{cursor < rows.length ? `${es ? 'Inicio de frase' : 'Start line'} ${cursor + 1}` : (es ? 'Fin de la última frase' : 'End final line')} <kbd>Espacio</kbd></button>
        <button className="btn" disabled={busy} onClick={() => { if (audio.current) audio.current.currentTime = Math.max(0, audio.current.currentTime - 3); }}><RotateCcw size={14} />−3 s</button>
        {onAlign && <button className="btn" disabled={busy || disabled} onClick={align}>{busy ? <Loader2 size={15} className="spin" /> : <Wand2 size={15} />}{es ? 'Enlazar con las palabras del audio' : 'Match the words in the audio'}</button>}
      </div>
      {busy && <p role="status" className="small muted">{es ? 'Procesando. La letra y el audio originales se conservan.' : 'Processing. The original text and audio are retained.'}</p>}
      <div className="timing-lines" role="list">
        {rows.map((row, index) => <div key={row.id} className={`timing-line${index === cursor ? ' current' : ''}`} role="listitem">
          <button className="timing-line-text" disabled={busy || disabled} onClick={() => { setCursor(index); setArmed(false); audio.current?.pause(); if (audio.current && row.start_s != null) audio.current.currentTime = row.start_s; }}>
            <span className="timing-line-number">{index + 1}</span><span>{row.section && <span className="small muted">{row.section} · </span>}{row.text}</span>
            {row.start_s == null ? <span className="pill warn">{es ? 'Sin tiempo' : 'Untimed'}</span> : row.needs_review ? <span className="pill warn">{es ? 'Revisar' : 'Review'}</span> : <Check size={14} />}
          </button>
          <label>{es ? 'Inicio · s' : 'Start · s'}<input aria-label={`${es ? 'Inicio frase' : 'Line start'} ${index + 1}`} type="number" min="0" step="0.01" value={row.start_s ?? ''} disabled={busy || disabled}
            onChange={e => change(index, { start_s: e.target.value === '' ? null : Number(e.target.value) })} /></label>
          <label>{es ? 'Fin · s' : 'End · s'}<input aria-label={`${es ? 'Fin frase' : 'Line end'} ${index + 1}`} type="number" min="0" step="0.01" value={row.end_s ?? ''} disabled={busy || disabled}
            onChange={e => change(index, { end_s: e.target.value === '' ? null : Number(e.target.value) })} /></label>
        </div>)}
      </div>
      {error && <div className="studio-error" role="alert">{error}</div>}
    </div>
  </Modal>;
}
