"""Own editable scene starters: inline Canvas, no external runtime or assets."""
import json
from typing import Literal

from pydantic import BaseModel, Field

from .html_motion_args import HtmlMotionSaveArgs

PROFILES = [
    {"id": "title", "name": "Título cinético", "name_en": "Kinetic title", "description": "Tipografía editorial y revelado geométrico. Editable typography and geometric reveal."},
    {"id": "layered", "name": "Paisaje en capas", "name_en": "Layered landscape", "description": "Profundidad y parallax en planos dibujados. Depth and parallax across drawn layers."},
    {"id": "oscillators", "name": "Osciladores", "name_en": "Oscillators", "description": "Estudio artístico de fases, no simulación científica. Artistic phase study, not a scientific simulation."},
    {"id": "elastic", "name": "Letras elásticas", "name_en": "Elastic letters", "description": "Entrada escalonada con oscilación amortiguada. Staggered lettering with analytical damped motion."},
]


class HtmlStarterArgs(BaseModel):
    profile: Literal['title', 'layered', 'oscillators', 'elastic']
    text: str = Field('Una idea en movimiento', description='Literal text drawn in the scene, never executable markup.')
    name: str | None = None
    width: int = Field(1280, gt=0)
    height: int = Field(720, gt=0)
    fps: float = Field(30, gt=0, allow_inf_nan=False)
    duration: float = Field(4, gt=0, allow_inf_nan=False)
    project_id: str | None = None


def catalogue():
    return {'starters': [dict(p) for p in PROFILES], 'format': 'lumiere-starter/v1',
            'editable': True, 'timing': 'window.seek(t), seconds', 'audio': False}


DRAW = {
    'title': """
ctx.fillStyle='#f1eee5';ctx.fillRect(0,0,W,H);
const reveal=1-Math.pow(1-p,3);
ctx.fillStyle='#b6573e';ctx.fillRect(W*.08,H*.13,W*.84*reveal,H*.025);
ctx.font=(H*.055)+'px Georgia';ctx.fillStyle='#777366';ctx.textAlign='left';
ctx.fillText('ESTUDIO DE MOVIMIENTO',W*.08,H*.27,W*.84);
ctx.save();ctx.beginPath();ctx.rect(W*.08,H*.32,W*.84,H*.34);ctx.clip();
ctx.font='bold '+(H*.15)+'px Georgia';ctx.fillStyle='#262b2c';
ctx.fillText(TEXT,W*.08,H*(.58+.28*(1-reveal)),W*.84);ctx.restore();
ctx.fillStyle='#b6573e';ctx.fillRect(W*.08,H*.78,W*.84*p,H*.004);
ctx.font=(H*.028)+'px Georgia';ctx.fillStyle='#777366';
ctx.fillText('TIEMPO / '+time.toFixed(2)+' s',W*.08,H*.87,W*.84);
""",
    'layered': """
ctx.fillStyle='#cbd7c0';ctx.fillRect(0,0,W,H);
ctx.fillStyle='#eee3bc';ctx.beginPath();ctx.arc(W*(.72+.03*Math.sin(p*TAU)),H*.24,H*.12,0,TAU);ctx.fill();
const inks=['#9aae9c','#6f9388','#46766e','#29594f','#163d39'];
for(let layer=0;layer<5;layer++){
 ctx.fillStyle=inks[layer];ctx.beginPath();ctx.moveTo(-W,H);
 for(let i=0;i<=90;i++){
  const x=W*(i/60-.25),shift=(p-.5)*(.08+layer*.05);
  const y=H*(.39+layer*.095+.075*Math.sin((i/60+shift)*7+layer)+.035*Math.sin((i/60+shift)*19-layer));
  ctx.lineTo(x,y);
 }ctx.lineTo(W*1.25,H);ctx.closePath();ctx.fill();
}
ctx.textAlign='left';ctx.fillStyle='#f0e6ca';ctx.font='italic '+(H*.07)+'px Georgia';
ctx.fillText(TEXT,W*.08,H*.85,W*.84);
""",
    'oscillators': """
ctx.fillStyle='#111b32';ctx.fillRect(0,0,W,H);ctx.strokeStyle='#334466';ctx.lineWidth=Math.max(1,H*.002);
ctx.beginPath();ctx.moveTo(W*.1,H*.24);ctx.lineTo(W*.9,H*.24);ctx.stroke();
for(let i=0;i<24;i++){
 const x=W*(.12+i*.76/23),a=Math.sin(p*TAU*(2+i*.13)+i*.11),y=H*(.55+.17*a);
 ctx.strokeStyle='#344868';ctx.beginPath();ctx.moveTo(x,H*.24);ctx.lineTo(x,y);ctx.stroke();
 ctx.fillStyle='hsl('+(178+i*2)+',60%,'+(57+i*.7)+'%)';ctx.beginPath();ctx.arc(x,y,H*.012,0,TAU);ctx.fill();
}
ctx.textAlign='center';ctx.font=(H*.055)+'px Georgia';ctx.fillStyle='#b6c9e7';ctx.fillText(TEXT,W*.5,H*.86,W*.82);
""",
    'elastic': """
ctx.fillStyle='#f2c860';ctx.fillRect(0,0,W,H);ctx.fillStyle='#363b30';ctx.textAlign='center';
const letters=Array.from(new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(TEXT),part=>part.segment),n=letters.length,unit=W*.8/Math.max(1,n),size=Math.min(H*.2,unit*1.6);
ctx.font='bold '+size+'px Georgia';
for(let i=0;i<n;i++){
 const delay=.2*i/Math.max(1,n-1),age=Math.max(0,p-delay),entered=1-Math.exp(-age*16);
 const wobble=Math.exp(-age*8)*Math.sin(age*26),x=W*.1+unit*(i+.5),y=H*(.57+.45*(1-entered)-.1*wobble);
 ctx.save();ctx.translate(x,y);ctx.rotate(wobble*.15);ctx.fillText(letters[i],0,0,unit*1.5);ctx.restore();
}
ctx.strokeStyle='#8d763b';ctx.lineWidth=H*.002;ctx.beginPath();ctx.moveTo(W*.1,H*.75);ctx.lineTo(W*.9,H*.75);ctx.stroke();
""",
}


def save_args(args: HtmlStarterArgs) -> HtmlMotionSaveArgs:
    # Embed user text as JSON with HTML-significant bytes escaped; it remains literal Canvas text.
    config = json.dumps({'text': args.text, 'duration': args.duration, 'width': args.width, 'height': args.height},
                        ensure_ascii=True, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    source = '''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Composición de Lumiere</title><style>html,body{margin:0;width:100%;height:100%;overflow:hidden}canvas{display:block;width:100%;height:100%}</style></head>
<body><canvas aria-label="Composición animada"></canvas><script>
// lumiere-starter/v1: editable source; every frame is calculated from time.
const CONFIG=''' + config + ''';
const W=CONFIG.width,H=CONFIG.height,TEXT=CONFIG.text,TAU=Math.PI*2;
const canvas=document.querySelector('canvas');canvas.width=W;canvas.height=H;const ctx=canvas.getContext('2d');
window.seek=function(time){const p=Math.max(0,Math.min(1,time/CONFIG.duration));ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,W,H);
''' + DRAW[args.profile] + '''
};window.seek(0);</script></body></html>'''
    name = args.name if args.name is not None else next(p['name'] for p in PROFILES if p['id'] == args.profile)
    return HtmlMotionSaveArgs(html=source, name=name, width=args.width, height=args.height,
                              fps=args.fps, duration=args.duration, project_id=args.project_id)
