import React, { useEffect, useMemo, useRef, useState } from "react";

export function groupWords(words, cap) {
  const groups = [];
  let cur = [];
  let chars = 0;
  for (const w of words) {
    const text = String(w.text || "");
    const gap = cur.length ? w.t - cur[cur.length - 1].t1 : 0;
    const endsSentence = cur.length && /[.?!…]$/.test(cur[cur.length - 1].text);
    const newLen = chars + text.length + (cur.length ? 1 : 0);
    const speakerChange = cur.length && cap.speaker_labels && cap.speaker_labels !== "off" && cur[cur.length - 1].speaker_name !== w.speaker_name;
    if (cur.length && (cur.length >= cap.max_words || newLen > cap.max_chars || endsSentence || gap > 800 || speakerChange)) {
      groups.push(cur);
      cur = [];
      chars = 0;
    }
    chars = cur.length ? chars + text.length + 1 : text.length;
    cur.push(w);
  }
  if (cur.length) groups.push(cur);
  return groups;
}

const BASE = { clean: 0.045, bold: 0.058, karaoke: 0.055, pop: 0.062, boxed: 0.045, minimal: 0.034 };

// The caption line under the playhead, drawn like the render does (group of words, active word highlighted).
export default function Captions({ doc, words, box, k, pb }) {
  const cap = doc.captions;
  const groups = useMemo(() => groupWords(words || [], cap), [words, cap.max_words, cap.max_chars, cap.speaker_labels]); // eslint-disable-line react-hooks/exhaustive-deps
  const manySpeakers = useMemo(() => new Set((words || []).map((w) => w.speaker_name).filter(Boolean)).size > 1, [words]);
  const [pos, setPos] = useState({ g: -1, w: -1 });
  const ref = useRef({ groups });
  ref.current = { groups };

  useEffect(() => {
    const find = (t) => {
      const gs = ref.current.groups;
      let lo = 0;
      let hi = gs.length - 1;
      let g = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (gs[mid][0].t <= t) { g = mid; lo = mid + 1; } else hi = mid - 1;
      }
      if (g < 0) return { g: -1, w: -1 };
      const grp = gs[g];
      const nextStart = gs[g + 1] ? gs[g + 1][0].t : Infinity;
      if (t > Math.min(grp[grp.length - 1].t1 + 250, nextStart)) return { g: -1, w: -1 };
      let w = 0;
      for (let i = 0; i < grp.length; i++) if (grp[i].t <= t) w = i;
      return { g, w };
    };
    const onTick = (t) => setPos((prev) => {
      const n = find(t);
      return n.g === prev.g && n.w === prev.w ? prev : n;
    });
    onTick(pb.t);
    return pb.subscribe(onTick);
  }, [pb, groups]);

  if (!cap.enabled || pos.g < 0 || !groups[pos.g]) return null;
  const grp = groups[pos.g];
  const W = doc.canvas.width;
  const H = doc.canvas.height;
  const portrait = H > W;
  const size = (cap.size || Math.round(Math.min(H, W * 1.78) * BASE[cap.style] * (portrait ? 1 : 1.05))) * k;
  const place = cap.position === "top" ? { top: box.h * 0.08 }
    : cap.position === "middle" ? { top: "50%", transform: "translateY(-50%)" }
      : cap.position === "bottom" ? { bottom: box.h * 0.06 }
        : { bottom: box.h * (portrait ? 0.22 : 0.11) };
  const boxed = cap.style === "boxed";
  const labels = cap.speaker_labels || "off";
  const speaker = grp[0].speaker_name;
  const tint = (labels === "color" || labels === "both") && grp[0].speaker_color ? grp[0].speaker_color : null;
  const prefix = (labels === "prefix" || labels === "both") && manySpeakers && speaker ? `${speaker}: ` : "";
  return (
    <div style={{ position: "absolute", left: box.w * 0.06, right: box.w * 0.06, zIndex: 9000, textAlign: "center", pointerEvents: "none", ...place }}>
      <span
        style={{
          display: "inline-block",
          fontFamily: `"${cap.font}", Arial, sans-serif`,
          fontSize: size,
          lineHeight: 1.15,
          fontWeight: cap.style === "clean" || cap.style === "minimal" ? 500 : 800,
          textTransform: cap.uppercase ? "uppercase" : "none",
          color: tint || cap.color,
          background: boxed ? "rgba(0,0,0,0.68)" : undefined,
          padding: boxed ? `${size * 0.18}px ${size * 0.4}px` : undefined,
          borderRadius: boxed ? size * 0.25 : undefined,
          WebkitTextStroke: !boxed && cap.style !== "clean" && cap.style !== "minimal" ? `${Math.max(1, size * 0.09)}px ${cap.outline}` : undefined,
          paintOrder: "stroke fill",
          textShadow: cap.style === "clean" || cap.style === "minimal" ? `0 ${size * 0.05}px ${size * 0.18}px #000c` : undefined,
        }}
      >
        {prefix ? <span style={{ marginRight: "0.28em" }}>{cap.uppercase ? prefix.toUpperCase() : prefix}</span> : null}
        {grp.map((w, i) => {
          const active = i === pos.w;
          const spoken = i <= pos.w;
          const hl = (cap.style === "pop" && active) || (cap.style === "karaoke" && spoken);
          return (
            <span key={w.id + i} style={{ color: hl ? cap.highlight : tint || cap.color, display: "inline-block", transform: cap.style === "pop" && active ? "scale(1.14)" : undefined, marginRight: i < grp.length - 1 ? "0.28em" : 0, transition: "transform 0.08s" }}>
              {w.text}
            </span>
          );
        })}
      </span>
    </div>
  );
}
