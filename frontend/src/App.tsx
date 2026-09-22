import { useEffect, useState } from "react";

/**
 * Placeholder UI. This file exists only so `npm run build` produces a
 * `dist/` the Python server can serve at `/` (see api.py's static
 * mount), and so the dev proxy to the real API can be exercised end to
 * end. The real UI (sidebar, Generate/Library/Designer/Audio/Timeline
 * views described in specs/prospero.md) replaces this component.
 */
type Health = { service: string; name: string; version: string; status: string };

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <main
      style={{
        fontFamily: "system-ui, sans-serif",
        background: "#141018",
        color: "#eee8f0",
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "1rem",
      }}
    >
      <h1 style={{ color: "#ff4d8d" }}>Prospero&apos;s Hoard</h1>
      <p>Frontend placeholder - the real studio UI is not built yet.</p>
      {health && (
        <pre style={{ background: "#1c1624", padding: "1rem", borderRadius: 8 }}>
          {JSON.stringify(health, null, 2)}
        </pre>
      )}
      {error && <p style={{ color: "#f66" }}>API not reachable: {error}</p>}
      <p>
        API docs: <a style={{ color: "#f5c26b" }} href="/api/health">/api/health</a>
      </p>
    </main>
  );
}
