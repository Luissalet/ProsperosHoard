import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { Route } from './ui';
import '../copilot.css';

const faustusMark = new URL('../assets/faustus-mark.svg', import.meta.url).href;

interface Panel extends HTMLElement {
  server: string; locale: string; theme: string; session: string | null;
  fetch: typeof fetch; contextProvider: () => Promise<unknown>;
  onOpenSettings: () => void; loadHistory: () => Promise<void>;
}

type Props = { projectId: string | null; route: Route; assetId?: string;
  lang: string; theme: string; onChanged: () => void; overlayLayer?: number;
  onOpenChanged?: (open: boolean) => void };

/** Thin host adapter. Faustus owns the panel, conversations, drafts and agent. */
export default function FaustusCopilot(props: Props) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const container = useRef<HTMLDivElement>(null);
  const current = useRef(props);
  current.current = props;
  useEffect(() => {
    const show = () => setOpen(true);
    window.addEventListener('prospero:copilot-open', show);
    return () => window.removeEventListener('prospero:copilot-open', show);
  }, []);
  useEffect(() => { props.onOpenChanged?.(open); }, [open,props.onOpenChanged]);
  useEffect(() => {
    if (!open) return;
    let disposed = false;
    let panel: Panel | undefined;
    async function mount() {
      try {
        const response = await fetch('/api/copilot/info');
        if (!response.ok) throw new Error(await response.text());
        const info = await response.json();
        await import(/* @vite-ignore */ info.panel_url);
        if (disposed) return;
        panel = document.createElement('faustus-chat') as Panel;
        panel.server = window.location.origin;
        panel.locale = current.current.lang;
        panel.theme = current.current.theme;
        panel.onOpenSettings = () => { window.open(info.settings_url, '_blank', 'noopener'); };
        panel.fetch = async (input, init = {}) => {
          const request = new Request(input, init);
          const url = new URL(request.url);
          const envelope: { path:string; method:string; headers:Record<string,string>; body?:unknown; raw_body?:string; content_type?:string } =
            {path:url.pathname + url.search, method:request.method, headers:{}};
          for (const name of ['X-Faustus-Client-Version', 'X-Odysseus-Run-Id']) {
            const value = request.headers.get(name);
            if (value) envelope.headers[name] = value;
          }
          if (request.method !== 'GET') {
            const bytes = new Uint8Array(await request.arrayBuffer());
            const type = request.headers.get('Content-Type') || '';
            if (type.startsWith('application/json')) envelope.body = JSON.parse(new TextDecoder().decode(bytes));
            else { envelope.content_type = type; envelope.raw_body = btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join('')); }
          }
          return fetch('/api/copilot/proxy', {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify(envelope),signal:request.signal});
        };
        panel.contextProvider = async () => {
          const {projectId, route, assetId, lang} = current.current;
          const project = projectId ? await api.project(projectId) : null;
          const selection = window.getSelection()?.toString();
          return {origin:{app_id:info.app_id,app_name:info.app_name,data_space_id:info.data_space_id,surface:'editor',tool_server_id:info.tool_server_id},
            ...(project ? {project:{external_project_id:project.id,title:project.name}} : {}),
            active_objects: assetId ? [{kind:'asset',object_id:assetId}] : route.arg ? [{kind:route.section,object_id:route.arg}] : [],
            selection:selection ? {kind:'text',text:selection} : null,
            view:{section:route.section,object_id:route.arg,locale:lang}};
        };
        const key = 'prospero-faustus-' + info.data_space_id + '-' + (props.projectId || 'app');
        panel.session = localStorage.getItem(key);
        panel.addEventListener('faustus:session-change', event => {
          const sid = (event as CustomEvent).detail.sessionId;
          if (sid) localStorage.setItem(key,sid); else localStorage.removeItem(key);
        });
        panel.addEventListener('faustus:turn-end', () => current.current.onChanged());
        container.current?.replaceChildren(panel);
        await panel.loadHistory();
        setError('');
      } catch (problem) { if (!disposed) setError(((current.current.lang) === 'es' ? 'Faustus no está disponible ahora mismo. Prospero sigue funcionando y tu borrador se conserva; cierra y vuelve a abrir el panel cuando Faustus esté en marcha.' : 'Faustus is not available right now. Prospero keeps working and your draft is kept; close and reopen the panel once Faustus is running.') + ' (' + (problem instanceof Error ? problem.message : String(problem)) + ')'); }
    }
    void mount();
    return () => { disposed = true; panel?.remove(); };
  }, [open, props.projectId]);
  useEffect(() => {
    const panel = container.current?.querySelector('faustus-chat') as Panel | null;
    if (panel) { panel.locale = props.lang; panel.theme = props.theme; }
  }, [props.lang,props.theme]);
  return <>
    <button type="button" className="prospero-copilot-toggle" style={{zIndex:props.overlayLayer}} aria-label="Faustus" aria-expanded={open} onClick={() => setOpen(!open)}><img src={faustusMark} alt="" aria-hidden="true" /></button>
    {open && <aside className="prospero-copilot" style={{zIndex:props.overlayLayer}} aria-label="Faustus">
      <header><strong>Faustus · Prospero</strong><button type="button" className="btn sm" onClick={() => setOpen(false)} aria-label={props.lang === 'es' ? 'Cerrar Faustus' : 'Close Faustus'}>×</button></header>
      {error && <p role="alert">{error}</p>}
      <div ref={container} className="prospero-copilot-content" />
    </aside>}
  </>;
}
