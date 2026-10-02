// Little pictures for the film-language guide (cinema.py on the server):
// one drawn scene, cropped like each shot size, side views for the angles,
// animated viewfinders for the camera moves (SVG animation, so they play
// like a looping gif), and variants of the same frame for lenses, light and
// composition. All drawn here: no image files.
import { useId, type ReactNode } from "react";

const SKIN = "#e9b896";
const HAIR = "#2b2030";
const JACKET = "#2a2f45";
const SHIRT = "#c8455a";
const PANTS = "#1d2233";
const MIC = "#9aa0a8";

/** A singer drawn with the feet at (0, 0) and the head top at y = -48. */
function PersonUnit({ shirt = SHIRT, jacket = JACKET, skin = SKIN, hair = HAIR, face = true, mic = true, flat }: {
  shirt?: string; jacket?: string; skin?: string; hair?: string; face?: boolean; mic?: boolean; flat?: string;
}) {
  const c = (x: string) => flat || x;
  return (
    <g>
      {/* legs */}
      <rect x={-4.6} y={-22} width={4.2} height={22} rx={1.4} fill={c(PANTS)} />
      <rect x={0.4} y={-22} width={4.2} height={22} rx={1.4} fill={c(PANTS)} />
      <ellipse cx={-2.8} cy={-0.6} rx={3} ry={1.2} fill={c("#111")} />
      <ellipse cx={2.8} cy={-0.6} rx={3} ry={1.2} fill={c("#111")} />
      {/* torso: jacket over shirt */}
      <path d="M-6 -39 Q0 -41.5 6 -39 L6.6 -21 L-6.6 -21 Z" fill={c(jacket)} />
      <path d="M-2.2 -39.6 L2.2 -39.6 L1.6 -21.5 L-1.6 -21.5 Z" fill={c(shirt)} />
      {/* left arm down, right arm up holding a microphone */}
      <path d="M-6 -38.6 Q-8.4 -31 -7.6 -23 L-5.4 -23 Q-5.6 -31 -4.4 -36 Z" fill={c(jacket)} />
      <circle cx={-6.5} cy={-22.4} r={1.3} fill={c(skin)} />
      <path d="M6 -38.6 Q9.4 -34 6.8 -30.4 L4.6 -31.6 Q6.6 -34 4.4 -36.4 Z" fill={c(jacket)} />
      <circle cx={4.1} cy={-31.6} r={1.3} fill={c(skin)} />
      {mic && <>
        <rect x={3.2} y={-36.4} width={1.1} height={5} rx={0.4} fill={c("#333")} transform="rotate(-18 3.7 -33.9)" />
        <circle cx={2.7} cy={-37} r={1.15} fill={c(MIC)} />
      </>}
      {/* neck and head */}
      <rect x={-1.3} y={-42} width={2.6} height={3} fill={c(skin)} />
      <ellipse cx={0} cy={-44.5} rx={3.4} ry={3.9} fill={c(skin)} />
      <path d="M-3.6 -44.6 Q-3.8 -49.2 0 -49 Q3.9 -49.2 3.6 -44.4 Q2.6 -47 0 -46.9 Q-2.2 -47 -3.6 -44.6 Z" fill={c(hair)} />
      {face && !flat && <>
        <ellipse cx={-1.25} cy={-44.6} rx={0.42} ry={0.5} fill="#1b1420" />
        <ellipse cx={1.25} cy={-44.6} rx={0.42} ry={0.5} fill="#1b1420" />
        <circle cx={-1.12} cy={-44.75} r={0.12} fill="#fff" />
        <circle cx={1.38} cy={-44.75} r={0.12} fill="#fff" />
        <path d="M-1.6 -45.6 Q-1.2 -45.9 -0.8 -45.6" stroke="#2b2030" strokeWidth={0.2} fill="none" />
        <path d="M0.8 -45.6 Q1.2 -45.9 1.6 -45.6" stroke="#2b2030" strokeWidth={0.2} fill="none" />
        <ellipse cx={0} cy={-42.6} rx={0.75} ry={0.45} fill="#7a2b36" />
      </>}
    </g>
  );
}

function Person({ x, feet, h = 48, ...rest }: { x: number; feet: number; h?: number } & Parameters<typeof PersonUnit>[0]) {
  return <g transform={`translate(${x} ${feet}) scale(${h / 48})`}><PersonUnit {...rest} /></g>;
}

/** The world every shot size is cut from: hills, a town, trees, a stage
 * lamp and the singer at (480, 470). */
function World({ id }: { id: string }) {
  return (
    <g>
      <defs>
        <linearGradient id={`${id}sky`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#253a6b" /><stop offset="0.7" stopColor="#d9826b" /><stop offset="1" stopColor="#f2c48f" />
        </linearGradient>
      </defs>
      <rect x={-200} y={-200} width={1360} height={600} fill={`url(#${id}sky)`} />
      <circle cx={700} cy={300} r={34} fill="#fbe2a6" opacity={0.9} />
      <path d="M-200 380 L60 250 L230 330 L390 220 L560 320 L720 240 L900 330 L1160 260 L1160 400 L-200 400 Z" fill="#46415f" />
      <path d="M-200 400 L120 330 L300 380 L520 340 L760 390 L1160 340 L1160 420 L-200 420 Z" fill="#363350" />
      {[0, 1, 2, 3, 4, 5, 6].map((i) => (
        <rect key={i} x={160 + i * 22} y={360 - (i % 3) * 14} width={18} height={60 + (i % 3) * 14} fill="#2a2840" />
      ))}
      {[0, 1, 2, 3, 4, 5, 6].map((i) => <rect key={`w${i}`} x={165 + i * 22} y={372 - (i % 3) * 14} width={3} height={3} fill="#f6d27a" />)}
      <rect x={-200} y={400} width={1360} height={300} fill="#2f4a3a" />
      <path d="M400 540 L470 400 L490 400 L560 540 Z" fill="#6b5b4a" opacity={0.7} />
      {[90, 250, 700, 860].map((tx, i) => (
        <g key={tx} transform={`translate(${tx} ${430 + i * 6})`}>
          <rect x={-2.5} y={-10} width={5} height={14} fill="#3b2a22" />
          <circle cx={0} cy={-22} r={14} fill="#24412f" /><circle cx={-8} cy={-14} r={9} fill="#2b4d38" />
        </g>
      ))}
      <rect x={526} y={410} width={1.6} height={60} fill="#222" />
      <circle cx={527} cy={409} r={3.4} fill="#ffe7a0" />
      <circle cx={527} cy={409} r={9} fill="#ffe7a0" opacity={0.25} />
      <Person x={480} feet={470} />
    </g>
  );
}

// viewBox crops of World for each shot size (16:9: h = w * 9 / 16)
const CROP: Record<string, [number, number, number]> = {
  // [centre x, top y, width]
  extreme_wide: [480, 0, 960],
  wide: [480, 360, 220],
  full: [480, 416, 104],
  cowboy: [480, 419, 78],
  medium: [480, 419.5, 53.3],
  medium_close: [480, 420, 37.3],
  close_up: [480, 420.6, 21.3],
  extreme_close_up: [480, 423.4, 6.4],
  detail: [483.4, 431.5, 13],
};

function Frame({ children, viewBox = "0 0 160 90", label }: { children: ReactNode; viewBox?: string; label?: string }) {
  return (
    <svg className="cine-art" viewBox={viewBox} preserveAspectRatio="xMidYMid slice" role="img" aria-label={label}>
      {children}
    </svg>
  );
}

function Camera({ x, y, toX, toY, scale = 1 }: { x: number; y: number; toX: number; toY: number; scale?: number }) {
  const deg = (Math.atan2(toY - y, toX - x) * 180) / Math.PI;
  return (
    <g transform={`translate(${x} ${y}) rotate(${deg}) scale(${scale})`}>
      <rect x={-9} y={-4.5} width={12} height={9} rx={1.5} fill="#e2b24c" />
      <path d="M3 -2.6 L8 -4.5 L8 4.5 L3 2.6 Z" fill="#c9952f" />
      <circle cx={-6} cy={-6.5} r={2.6} fill="#e2b24c" /><circle cx={-0.5} cy={-6.5} r={2.6} fill="#e2b24c" />
    </g>
  );
}

function AngleArt({ id, kind }: { id: string; kind: string }) {
  const face = { x: 104, y: 33 };
  const cams: Record<string, [number, number]> = {
    eye_level: [40, 33], low_angle: [46, 74], high_angle: [44, 6], overhead: [104, 6], worms_eye: [92, 82],
  };
  if (kind === "dutch") {
    return (
      <Frame label={kind}>
        <rect width={160} height={90} fill="#1b1622" />
        <g transform="rotate(-14 80 45)">
          <svg x={14} y={6} width={132} height={78} viewBox="459 419.5 44 24.75" preserveAspectRatio="xMidYMid slice"><World id={id} /></svg>
          <rect x={14} y={6} width={132} height={78} fill="none" stroke="#e2b24c" strokeWidth={1.2} />
        </g>
      </Frame>
    );
  }
  const [cx, cy] = cams[kind] || cams.eye_level;
  return (
    <Frame label={kind}>
      <rect width={160} height={90} fill="#1b1622" />
      <rect y={84} width={160} height={6} fill="#2f4a3a" />
      <Person x={104} feet={84} h={58} />
      <line x1={cx} y1={cy} x2={face.x} y2={kind === "overhead" ? 26 : face.y} stroke="#e2b24c" strokeWidth={0.8} strokeDasharray="2 2" />
      <Camera x={cx} y={cy} toX={face.x} toY={kind === "overhead" ? 40 : face.y} />
    </Frame>
  );
}

/** The scene for the moves: three depth layers so parallax reads. */
function MoveScene({ id, bg, mid, fg, person, personAnim, blur }: {
  id: string; bg?: ReactNode; mid?: ReactNode; fg?: ReactNode; person?: ReactNode; personAnim?: ReactNode; blur?: boolean;
}) {
  return (
    <Frame label="move">
      <defs>
        <linearGradient id={`${id}msky`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#2b3f73" /><stop offset="1" stopColor="#e39a74" /></linearGradient>
        <filter id={`${id}mb`}><feGaussianBlur stdDeviation="2.4 0" /></filter>
      </defs>
      <rect width={160} height={90} fill={`url(#${id}msky)`} />
      <g filter={blur ? `url(#${id}mb)` : undefined}>
        <g>{bg}<path d="M-200 60 L-140 40 L-90 52 L-40 30 L10 50 L60 34 L110 52 L160 36 L210 50 L260 32 L320 52 L360 40 L360 90 L-200 90 Z" fill="#4a4463" /></g>
        <g>{mid}<rect x={-200} y={66} width={560} height={30} fill="#2f4a3a" />
          {[-160, -100, -40, 20, 80, 140, 200, 260, 320].map((x) => <g key={x}><rect x={x} y={44} width={2} height={24} fill="#222" /><circle cx={x + 1} cy={43} r={2.6} fill="#ffe7a0" /></g>)}
        </g>
        <g>{person || <Person x={80} feet={80} h={40} />}{personAnim}</g>
        <g>{fg}</g>
      </g>
    </Frame>
  );
}

const anim = (type: "translate" | "scale" | "rotate", values: string, dur = 4, extra: Record<string, string> = {}) => (
  <animateTransform attributeName="transform" type={type} values={values} dur={`${dur}s`} repeatCount="indefinite" additive="sum" {...extra} />
);

function MoveArt({ id, kind }: { id: string; kind: string }) {
  switch (kind) {
    case "static":
      return <MoveScene id={id} />;
    case "pan":
      return <Pan id={id} values="0 0; -70 0; 0 0" />;
    case "tilt":
      return <Pan id={id} values="0 -14; 0 26; 0 -14" />;
    case "whip_pan":
      return <Pan id={id} values="0 0; 0 0; -140 0; -140 0; 0 0" keyTimes="0;0.4;0.5;0.9;1" dur={3} blur />;
    case "dolly_in":
    case "dolly_out":
    case "zoom_in":
    case "dolly_zoom": {
      const fwd = kind !== "dolly_out";
      const s = (a: number, b: number) => (fwd ? `${a};${b};${a}` : `${b};${a};${b}`);
      const layer = (from: number, to: number) => ({ scale: s(from, to) });
      const bg = kind === "dolly_zoom" ? layer(1, 1.9) : kind === "zoom_in" ? layer(1, 1.8) : layer(1, 1.15);
      const person = kind === "dolly_zoom" ? layer(1, 1) : layer(1, 1.8);
      return (
        <Frame label={kind}>
          <defs><linearGradient id={`${id}msky`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#2b3f73" /><stop offset="1" stopColor="#e39a74" /></linearGradient></defs>
          <rect width={160} height={90} fill={`url(#${id}msky)`} />
          <ZoomLayer values={bg.scale}>
            <path d="M-40 60 L0 40 L30 52 L60 30 L90 50 L120 34 L160 52 L200 40 L200 90 L-40 90 Z" fill="#4a4463" />
            <rect x={-40} y={66} width={240} height={30} fill="#2f4a3a" />
            {[10, 40, 120, 150].map((x) => <g key={x}><rect x={x} y={44} width={2} height={24} fill="#222" /><circle cx={x + 1} cy={43} r={2.6} fill="#ffe7a0" /></g>)}
          </ZoomLayer>
          <ZoomLayer values={person.scale}><Person x={80} feet={80} h={40} /></ZoomLayer>
        </Frame>
      );
    }
    case "truck":
    case "orbit":
      return (
        <MoveScene id={id}
          bg={anim("translate", kind === "orbit" ? "0 0; 40 0; 0 0" : "0 0; -20 0; 0 0")}
          mid={anim("translate", kind === "orbit" ? "0 0; 10 0; 0 0" : "0 0; -60 0; 0 0")}
          personAnim={kind === "truck" ? anim("translate", "0 0; -40 0; 0 0") : undefined}
          fg={<g><rect x={kind === "orbit" ? 150 : 30} y={0} width={10} height={90} fill="#141018" opacity={0.85} />
            {anim("translate", kind === "orbit" ? "0 0; -150 0; 0 0" : "0 0; -110 0; 0 0")}</g>} />
      );
    case "tracking":
      return (
        <MoveScene id={id}
          bg={anim("translate", "0 0; -60 0", 4)}
          mid={anim("translate", "0 0; -120 0", 4)}
          person={<g><Person x={80} feet={80} h={40} />{anim("translate", "0 0; 0 -0.8; 0 0", 0.5)}</g>} />
      );
    case "crane":
      return <Pan id={id} values="0 30; 0 -10; 0 30" scale="1.2; 0.85; 1.2" />;
    case "handheld":
      return <Pan id={id} values="0 0; 1.2 -0.8; -0.8 0.6; 1 1; -1 -0.6; 0.4 0.9; 0 0" dur={1.4} />;
    case "drone":
      return (
        <Frame label={kind}>
          <rect width={160} height={90} fill="#2f4a3a" />
          <g>
            {anim("translate", "0 -90; 0 0", 3)}
            {[0, 90].map((oy) => (
              <g key={oy} transform={`translate(0 ${oy})`}>
                <rect x={0} y={0} width={60} height={40} fill="#3e5e44" /><rect x={100} y={10} width={60} height={50} fill="#5a6e3a" />
                <rect x={70} y={0} width={20} height={90} fill="#6b5b4a" />
                <rect x={10} y={50} width={30} height={30} fill="#a27a4a" /><rect x={110} y={66} width={40} height={20} fill="#3b3550" />
              </g>
            ))}
          </g>
          <path d="M70 90 L80 76 L90 90" fill="none" stroke="#e2b24c" strokeWidth={1} />
        </Frame>
      );
    case "rack_focus":
      return <FocusArt id={id} rack />;
    default:
      return <MoveScene id={id} />;
  }
}

function ZoomLayer({ values, children }: { values: string; children: ReactNode }) {
  // scale about the frame centre: translate(c) scale(s) translate(-c)
  return (
    <g transform="translate(80 45)">
      <g>
        <animateTransform attributeName="transform" type="scale" values={values} dur="4s" repeatCount="indefinite" />
        <g transform="translate(-80 -45)">{children}</g>
      </g>
    </g>
  );
}

function Pan({ id, values, dur = 4, keyTimes, blur, scale }: { id: string; values: string; dur?: number; keyTimes?: string; blur?: boolean; scale?: string }) {
  return (
    <Frame label="move">
      <defs>
        <linearGradient id={`${id}psky`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#2b3f73" /><stop offset="1" stopColor="#e39a74" /></linearGradient>
        <filter id={`${id}pb`}><feGaussianBlur stdDeviation="3 0" /></filter>
      </defs>
      <rect width={160} height={90} fill="#2b3f73" />
      <g filter={blur ? `url(#${id}pb)` : undefined}>
        <g>
          <animateTransform attributeName="transform" type="translate" values={values} dur={`${dur}s`} repeatCount="indefinite" {...(keyTimes ? { keyTimes } : {})} />
          {scale && <animateTransform attributeName="transform" type="scale" values={scale} dur={`${dur}s`} repeatCount="indefinite" additive="sum" />}
          <rect x={-100} y={-60} width={400} height={110} fill={`url(#${id}psky)`} />
          <circle cx={190} cy={20} r={10} fill="#fbe2a6" />
          <path d="M-100 60 L-40 40 L10 52 L60 30 L110 50 L160 34 L210 52 L260 38 L300 50 L300 140 L-100 140 Z" fill="#4a4463" />
          <rect x={-100} y={66} width={400} height={80} fill="#2f4a3a" />
          {[-60, 20, 140, 220].map((x) => <g key={x}><rect x={x} y={44} width={2} height={24} fill="#222" /><circle cx={x + 1} cy={43} r={2.6} fill="#ffe7a0" /></g>)}
          <Person x={80} feet={80} h={40} />
          <Person x={190} feet={82} h={36} shirt="#3a7ca5" />
        </g>
      </g>
    </Frame>
  );
}

function FocusArt({ id, mode, rack }: { id: string; mode?: "shallow" | "deep"; rack?: boolean }) {
  const bgBlur = mode === "shallow" ? 2.2 : 0;
  return (
    <Frame label="focus">
      <defs>
        <filter id={`${id}fbg`} x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation={bgBlur}>
            {rack && <animate attributeName="stdDeviation" values="0;0;2.4;2.4;0" keyTimes="0;0.4;0.5;0.9;1" dur="4s" repeatCount="indefinite" />}
          </feGaussianBlur>
        </filter>
        <filter id={`${id}ffg`} x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation={rack ? 2.4 : 0}>
            {rack && <animate attributeName="stdDeviation" values="2.4;2.4;0;0;2.4" keyTimes="0;0.4;0.5;0.9;1" dur="4s" repeatCount="indefinite" />}
          </feGaussianBlur>
        </filter>
      </defs>
      <rect width={160} height={90} fill="#2b2440" />
      <g filter={`url(#${id}fbg)`}>
        <rect y={52} width={160} height={38} fill="#3a3352" />
        {[18, 46, 120, 146].map((x, i) => <circle key={x} cx={x} cy={22 + (i % 2) * 12} r={mode === "shallow" ? 7 : 2.5} fill="#ffd27a" opacity={0.75} />)}
        <Person x={128} feet={86} h={30} shirt="#3a7ca5" face={false} />
        <rect x={6} y={30} width={22} height={26} fill="#4a4060" />
      </g>
      <g filter={rack ? `url(#${id}ffg)` : undefined}><Person x={62} feet={150} h={130} /></g>
    </Frame>
  );
}

function LensArt({ id, kind }: { id: string; kind: string }) {
  if (kind === "shallow_focus") return <FocusArt id={id} mode="shallow" />;
  if (kind === "deep_focus") return <FocusArt id={id} mode="deep" />;
  if (kind === "rack_focus") return <FocusArt id={id} rack />;
  if (kind === "macro") {
    return (
      <Frame label={kind}>
        <rect width={160} height={90} fill="#1f3a2a" />
        <path d="M-10 80 Q60 20 170 40 L170 100 L-10 100 Z" fill="#3f7a46" />
        <path d="M-10 80 Q60 20 170 40" stroke="#5aa060" strokeWidth={1.4} fill="none" />
        <circle cx={78} cy={44} r={20} fill="#9fd3e8" opacity={0.55} />
        <ellipse cx={72} cy={36} rx={6} ry={4} fill="#fff" opacity={0.85} />
        <circle cx={78} cy={44} r={20} fill="none" stroke="#d8f2fb" strokeWidth={1} opacity={0.7} />
      </Frame>
    );
  }
  if (kind === "fisheye") {
    return (
      <Frame label={kind}>
        <rect width={160} height={90} fill="#0d0b12" />
        <clipPath id={`${id}fc`}><circle cx={80} cy={45} r={44} /></clipPath>
        <g clipPath={`url(#${id}fc)`}>
          <rect width={160} height={90} fill="#5a7cc0" />
          <path d="M20 60 Q80 30 140 60 L160 90 L0 90 Z" fill="#3c4a3c" />
          <path d="M36 90 Q46 40 62 20 M124 90 Q114 40 98 20" stroke="#8a8aa0" strokeWidth={3} fill="none" />
          <Person x={80} feet={86} h={52} />
        </g>
      </Frame>
    );
  }
  if (kind === "anamorphic") {
    return (
      <Frame label={kind}>
        <rect width={160} height={90} fill="#0a0810" />
        <g>
          <rect y={12} width={160} height={66} fill="#16203a" />
          {[[24, 30], [40, 52], [130, 28], [146, 50]].map(([x, y]) => <ellipse key={x} cx={x} cy={y} rx={3.5} ry={6.5} fill="#ffd27a" opacity={0.6} />)}
          <Person x={80} feet={118} h={96} />
          <rect x={0} y={30} width={160} height={1.2} fill="#7ab8ff" opacity={0.85} />
          <circle cx={112} cy={30.6} r={3} fill="#cfe6ff" />
        </g>
        <rect width={160} height={12} fill="#000" /><rect y={78} width={160} height={12} fill="#000" />
      </Frame>
    );
  }
  // ultra_wide / normal / telephoto: the same singer, the background shrinks or grows
  const bg = kind === "ultra_wide" ? 0.55 : kind === "telephoto" ? 1.9 : 1;
  return (
    <Frame label={kind}>
      <rect width={160} height={90} fill="#2b3f73" />
      <g transform={`translate(80 60) scale(${bg}) translate(-80 -60)`}>
        <rect x={-200} y={0} width={560} height={60} fill="#d9826b" opacity={0.5} />
        <circle cx={104} cy={38} r={10} fill="#fbe2a6" />
        <path d="M-200 60 L20 34 L60 50 L100 30 L140 50 L360 40 L360 120 L-200 120 Z" fill="#4a4463" />
        <rect x={-200} y={58} width={560} height={60} fill="#2f4a3a" />
      </g>
      {kind === "ultra_wide" && <path d="M0 90 L60 64 M160 90 L100 64" stroke="#8a8aa0" strokeWidth={1.4} />}
      <Person x={80} feet={kind === "ultra_wide" ? 120 : 112} h={kind === "ultra_wide" ? 88 : 80} />
    </Frame>
  );
}

function LightArt({ id, kind }: { id: string; kind: string }) {
  const presets: Record<string, { bg: ReactNode; person: Parameters<typeof PersonUnit>[0]; extra?: ReactNode; over?: ReactNode }> = {
    high_key: { bg: <rect width={160} height={90} fill="#f4efe9" />, person: { jacket: "#c9c4d6", shirt: "#f08a9a" } },
    low_key: {
      bg: <rect width={160} height={90} fill="#07060a" />, person: { jacket: "#15141c", shirt: "#3a1218", skin: "#6d4a3a", hair: "#050406" },
      over: <rect width={160} height={90} fill={`url(#${id}lk)`} />,
    },
    chiaroscuro: {
      bg: <rect width={160} height={90} fill="#120f16" />, person: {},
      over: <rect x={80} width={80} height={90} fill="#000" opacity={0.72} />,
    },
    rim_light: {
      bg: <rect width={160} height={90} fill="#07060c" />,
      person: { flat: "#0c0b10" },
      extra: <g filter={`url(#${id}glow)`} opacity={0.95}><g transform="translate(80 132) scale(2.3)"><PersonUnit flat="#ffe1a8" /></g></g>,
    },
    silhouette: { bg: <rect width={160} height={90} fill={`url(#${id}sun)`} />, person: { flat: "#09070c" } },
    golden_hour: { bg: <><rect width={160} height={90} fill={`url(#${id}gold)`} /><circle cx={130} cy={60} r={12} fill="#ffd36e" /></>, person: { jacket: "#4a3550", shirt: "#e07a4a" } },
    blue_hour: {
      bg: <><rect width={160} height={90} fill={`url(#${id}blue)`} />{[10, 26, 120, 140].map((x, i) => <rect key={x} x={x} y={40 - i * 4} width={14} height={60} fill="#141a33" />)}
        {[14, 30, 124, 144].map((x) => <rect key={x} x={x} y={48} width={3} height={3} fill="#ffd27a" />)}</>,
      person: { jacket: "#1e2440", shirt: "#5a6ab0", skin: "#b9a0a8" },
    },
    neon: {
      bg: <><rect width={160} height={90} fill="#0b0816" /><rect x={10} y={14} width={34} height={8} rx={4} fill="#ff3ea5" filter={`url(#${id}glow)`} />
        <rect x={116} y={20} width={30} height={8} rx={4} fill="#2ee6ff" filter={`url(#${id}glow)`} /></>,
      person: { jacket: "#2a1a40", shirt: "#ff3ea5", skin: "#d39ad8" },
      over: <><rect width={80} height={90} fill="#ff3ea5" opacity={0.12} /><rect x={80} width={80} height={90} fill="#2ee6ff" opacity={0.12} /></>,
    },
    practical: {
      bg: <><rect width={160} height={90} fill="#120d0a" /><rect x={128} y={36} width={2} height={40} fill="#3a2a20" />
        <path d="M120 36 L138 36 L134 26 L124 26 Z" fill="#ffcf7a" /><circle cx={129} cy={34} r={18} fill="#ffcf7a" opacity={0.18} /></>,
      person: { skin: "#c08a64" },
      over: <rect width={160} height={90} fill={`url(#${id}prac)`} />,
    },
    soft_light: {
      bg: <><rect width={160} height={90} fill="#3b3445" /><rect x={6} y={10} width={30} height={42} rx={2} fill="#fff6ea" opacity={0.9} /></>,
      person: {},
      over: <rect width={160} height={90} fill={`url(#${id}soft)`} />,
    },
  };
  const p = presets[kind] || presets.high_key;
  return (
    <Frame label={kind}>
      <defs>
        <radialGradient id={`${id}lk`} cx="0.3" cy="0.35" r="0.6"><stop offset="0" stopColor="#000" stopOpacity="0" /><stop offset="1" stopColor="#000" stopOpacity="0.85" /></radialGradient>
        <linearGradient id={`${id}sun`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#f7b05a" /><stop offset="1" stopColor="#ffe6a8" /></linearGradient>
        <linearGradient id={`${id}gold`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#f08a4a" /><stop offset="1" stopColor="#ffd7a0" /></linearGradient>
        <linearGradient id={`${id}blue`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#0d1a4a" /><stop offset="1" stopColor="#3a5aa8" /></linearGradient>
        <radialGradient id={`${id}prac`} cx="0.8" cy="0.4" r="0.8"><stop offset="0" stopColor="#ffb050" stopOpacity="0.15" /><stop offset="1" stopColor="#000" stopOpacity="0.75" /></radialGradient>
        <linearGradient id={`${id}soft`} x1="0" y1="0" x2="1" y2="0"><stop offset="0" stopColor="#fff6ea" stopOpacity="0.18" /><stop offset="1" stopColor="#000" stopOpacity="0.25" /></linearGradient>
        <filter id={`${id}glow`} x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="1.6" /></filter>
      </defs>
      {p.bg}
      {p.extra}
      <g transform="translate(80 132) scale(2.3)"><PersonUnit {...p.person} /></g>
      {p.over}
    </Frame>
  );
}

function CompositionArt({ id, kind }: { id: string; kind: string }) {
  const grid = (
    <g stroke="#e2b24c" strokeWidth={0.5} opacity={0.8}>
      <line x1={53.3} y1={0} x2={53.3} y2={90} /><line x1={106.7} y1={0} x2={106.7} y2={90} />
      <line x1={0} y1={30} x2={160} y2={30} /><line x1={0} y1={60} x2={160} y2={60} />
    </g>
  );
  const sky = <><rect width={160} height={90} fill="#3b5590" /><rect y={64} width={160} height={26} fill="#2f4a3a" /></>;
  switch (kind) {
    case "rule_of_thirds":
      return <Frame label={kind}>{sky}<Person x={53.3} feet={112} h={86} />{grid}<circle cx={53.3} cy={30} r={2} fill="#e2b24c" /></Frame>;
    case "symmetry":
      return (
        <Frame label={kind}>
          <rect width={160} height={90} fill="#3a2f4a" />
          <path d="M20 90 L20 30 Q50 6 80 6 Q110 6 140 30 L140 90" fill="none" stroke="#c9a6ff" strokeWidth={3} />
          <path d="M40 90 L40 40 Q60 22 80 22 Q100 22 120 40 L120 90" fill="none" stroke="#8a6ac0" strokeWidth={2} />
          <Person x={80} feet={88} h={54} />
          <line x1={80} y1={0} x2={80} y2={90} stroke="#e2b24c" strokeWidth={0.5} strokeDasharray="2 2" />
        </Frame>
      );
    case "leading_lines":
      return (
        <Frame label={kind}>
          <rect width={160} height={90} fill="#3b5590" /><rect y={44} width={160} height={46} fill="#2f4a3a" />
          <path d="M20 90 L76 44 L84 44 L140 90 Z" fill="#555" />
          <path d="M80 90 L80 46" stroke="#f2d07a" strokeWidth={1.2} strokeDasharray="4 3" />
          <path d="M0 70 L76 44 M160 70 L84 44" stroke="#e2b24c" strokeWidth={0.6} opacity={0.8} />
          {[0, 1, 2, 3, 4, 5].map((i) => <rect key={i} x={2 + i * 11} y={63 - i * 3.6} width={1.6} height={10 - i * 1.4} fill="#8a7a6a" />)}
          <Person x={80} feet={46} h={12} />
        </Frame>
      );
    case "negative_space":
      return <Frame label={kind}><rect width={160} height={90} fill="#6e86b8" /><rect y={80} width={160} height={10} fill="#4a5a7a" /><Person x={128} feet={82} h={14} /></Frame>;
    case "frame_in_frame":
      return (
        <Frame label={kind}>
          <rect width={160} height={90} fill="#1b1622" />
          <rect x={52} y={8} width={56} height={82} fill="#d9826b" />
          <Person x={80} feet={86} h={56} />
          <rect x={46} y={4} width={68} height={86} fill="none" stroke="#5a4636" strokeWidth={8} />
        </Frame>
      );
    case "foreground_layers":
      return (
        <Frame label={kind}>
          <defs><filter id={`${id}fl`}><feGaussianBlur stdDeviation="3" /></filter></defs>
          {sky}
          <Person x={92} feet={120} h={90} />
          <g filter={`url(#${id}fl)`}>
            <ellipse cx={14} cy={20} rx={22} ry={12} fill="#1e3a24" /><ellipse cx={10} cy={60} rx={20} ry={18} fill="#244a2c" />
            <ellipse cx={150} cy={80} rx={20} ry={16} fill="#1e3a24" />
          </g>
        </Frame>
      );
    default:
      return <Frame label={kind}>{sky}{grid}</Frame>;
  }
}

function SpecialShot({ id, kind }: { id: string; kind: string }) {
  if (kind === "over_shoulder") {
    return (
      <Frame label={kind}>
        <defs><filter id={`${id}os`}><feGaussianBlur stdDeviation="2" /></filter></defs>
        <rect width={160} height={90} fill="#2a2440" /><rect y={66} width={160} height={24} fill="#3a3352" />
        <Person x={108} feet={180} h={150} shirt="#3a7ca5" />
        <g filter={`url(#${id}os)`}>
          <ellipse cx={34} cy={96} rx={44} ry={26} fill="#16131d" />
          <ellipse cx={36} cy={48} rx={20} ry={24} fill="#1d1824" />
        </g>
      </Frame>
    );
  }
  if (kind === "two_shot") {
    return (
      <Frame label={kind}>
        <rect width={160} height={90} fill="#2b3f73" /><rect y={64} width={160} height={26} fill="#2f4a3a" />
        <Person x={56} feet={140} h={118} />
        <Person x={106} feet={142} h={116} shirt="#3a7ca5" jacket="#3b2a2a" mic={false} />
      </Frame>
    );
  }
  if (kind === "pov") {
    return (
      <Frame label={kind}>
        <rect width={160} height={90} fill="#1a1626" />
        <path d="M0 0 L50 22 L50 70 L0 90 Z M160 0 L110 22 L110 70 L160 90 Z" fill="#2a2440" />
        <rect x={50} y={22} width={60} height={48} fill="#3a3352" />
        <Person x={80} feet={66} h={30} shirt="#3a7ca5" />
        <path d="M18 90 Q24 66 40 62 Q48 62 46 70 L38 90 Z" fill={SKIN} />
        <path d="M142 90 Q136 66 120 62 Q112 62 114 70 L122 90 Z" fill={SKIN} />
        <rect x={34} y={54} width={10} height={14} rx={2} fill="#222" transform="rotate(-12 39 61)" />
      </Frame>
    );
  }
  return null;
}

/** The picture for one guide entry. */
export function CinemaArt({ id: entryId, category }: { id: string; category: string }) {
  const uid = useId().replace(/:/g, "");
  if (category === "shot") {
    const special = SpecialShot({ id: uid, kind: entryId });
    if (special) return special;
    const [cx, top, w] = CROP[entryId] || CROP.wide;
    const h = (w * 9) / 16;
    return <Frame viewBox={`${cx - w / 2} ${top} ${w} ${h}`} label={entryId}><World id={uid} /></Frame>;
  }
  if (category === "angle") return <AngleArt id={uid} kind={entryId} />;
  if (category === "move") return <MoveArt id={uid} kind={entryId} />;
  if (category === "lens") return <LensArt id={uid} kind={entryId} />;
  if (category === "light") return <LightArt id={uid} kind={entryId} />;
  return <CompositionArt id={uid} kind={entryId} />;
}
