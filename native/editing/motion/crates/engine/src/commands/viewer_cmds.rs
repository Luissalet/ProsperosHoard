//! Composition viewer state that agents and the CLI can drive too: snapping, Show Channel and
//! exposure, snapshots, Fast Previews, guides dragged out of the rulers, and the Pen tool for
//! shape layers (`shape.newPath`).

use lumiere_motion_keyframe::ShapePath;
use lumiere_motion_project::LayerSource;
use lumiere_motion_project::build::{self, Ids};
use lumiere_motion_render::RenderOpts;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::{CommandSpec, always, b_p, bad, comp_id, f_p, has_comp, merge_p, str_p};
use crate::viewer::{Channel, CustomRgb, SimProfile, Simulation, Snapshot};
use crate::{EngineError, Result, Session, VertexRef, cmd, query};

/// Composition ▸ Preview ▸ Fast Previews modes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum FastPreviews {
    #[default]
    Off,
    AdaptiveResolution,
    Draft,
    FastDraft,
    Wireframe,
}

impl FastPreviews {
    pub const ALL: [FastPreviews; 5] =
        [FastPreviews::Off, FastPreviews::AdaptiveResolution, FastPreviews::Draft, FastPreviews::FastDraft, FastPreviews::Wireframe];
    pub fn id(self) -> &'static str {
        match self {
            FastPreviews::Off => "off",
            FastPreviews::AdaptiveResolution => "adaptive",
            FastPreviews::Draft => "draft",
            FastPreviews::FastDraft => "fastDraft",
            FastPreviews::Wireframe => "wireframe",
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            FastPreviews::Off => "Off (Final Quality)",
            FastPreviews::AdaptiveResolution => "Adaptive Resolution",
            FastPreviews::Draft => "Draft",
            FastPreviews::FastDraft => "Fast Draft",
            FastPreviews::Wireframe => "Wireframe",
        }
    }
    pub fn from_name(s: &str) -> Option<FastPreviews> {
        let n = s.to_ascii_lowercase().replace([' ', '_', '-'], "");
        FastPreviews::ALL.into_iter().find(|m| m.id().to_ascii_lowercase() == n || m.label().to_ascii_lowercase().replace(' ', "").starts_with(&n))
    }
    /// Render options for the viewer: (draft quality, resolution factor while interacting).
    pub fn render(self, interacting: bool) -> (bool, f64) {
        match self {
            FastPreviews::Off => (false, 1.0),
            FastPreviews::AdaptiveResolution => (false, if interacting { 0.25 } else { 1.0 }),
            FastPreviews::Draft => (true, 1.0),
            FastPreviews::FastDraft => (true, if interacting { 0.25 } else { 1.0 }),
            FastPreviews::Wireframe => (true, 1.0),
        }
    }
}

/// Viewer display options (Show Channel, exposure, snapshot, Fast Previews, display colour).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ViewOptions {
    pub channel: Channel,
    /// Show Channel ▸ Colorize: single channels shown in their colour instead of gray.
    pub colorized: bool,
    /// Adjust Exposure, in stops.
    pub exposure: f32,
    /// Show Snapshot (F5 held): the viewer shows the snapshot instead of the frame.
    pub show_snapshot: bool,
    pub fast_previews: FastPreviews,
    /// View ▸ Use Display Color Management (with a working space): the frame is converted
    /// from the working space to the display; off shows the working space's numbers.
    pub display_color_management: bool,
    /// View ▸ Simulate Output.
    pub simulation: Simulation,
    /// The profile View ▸ Simulate Output ▸ Custom... uses.
    pub custom_simulation: Simulation,
}

impl Default for ViewOptions {
    fn default() -> Self {
        ViewOptions {
            channel: Channel::default(),
            colorized: false,
            exposure: 0.0,
            show_snapshot: false,
            fast_previews: FastPreviews::default(),
            display_color_management: true,
            simulation: Simulation::default(),
            custom_simulation: Simulation { profile: SimProfile::Linear, preserve_rgb: true },
        }
    }
}

/// View ▸ Split with New Locked Viewer: a second Composition viewer that keeps showing one
/// comp and 3D view whatever comp is active.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct LockedViewer {
    pub comp: lumiere_motion_project::ItemId,
    pub view: lumiere_motion_render::three_d::View3D,
}

fn snapping(s: &mut Session, p: &Value) -> Result<Value> {
    s.state.snapping = b_p(p, "value").unwrap_or(!s.state.snapping);
    Ok(json!(s.state.snapping))
}

fn channel(s: &mut Session, p: &Value) -> Result<Value> {
    let name = str_p(p, "channel").unwrap_or("rgb");
    let c = Channel::from_name(name).ok_or_else(|| bad("view.channel", format!("unknown channel `{name}` (rgb|red|green|blue|alpha|rgbStraight)")))?;
    // Choosing the active single channel again returns to RGB, as in After Effects.
    let c = if c == s.state.viewer.channel && c != Channel::Rgb && b_p(p, "toggle").unwrap_or(false) { Channel::Rgb } else { c };
    s.state.viewer.channel = c;
    if let Some(v) = b_p(p, "colorized") {
        s.state.viewer.colorized = v;
    }
    Ok(json!({"channel": c.id(), "colorized": s.state.viewer.colorized}))
}

fn exposure(s: &mut Session, p: &Value) -> Result<Value> {
    let cur = s.state.viewer.exposure as f64;
    let v = match (f_p(p, "stops"), f_p(p, "delta")) {
        (Some(v), _) => v,
        (None, Some(d)) => cur + d,
        _ => return Err(bad("view.exposure", "pass `stops` or `delta`")),
    };
    s.state.viewer.exposure = v.clamp(-40.0, 40.0) as f32;
    Ok(json!(s.state.viewer.exposure))
}

fn reset_exposure(s: &mut Session, _: &Value) -> Result<Value> {
    s.state.viewer.exposure = 0.0;
    Ok(json!(0.0))
}

fn take_snapshot(s: &mut Session, p: &Value) -> Result<Value> {
    let cid = comp_id(s, p)?;
    let comp = s.project.comp(cid).ok_or(EngineError::NoComp)?;
    let t = s.time_of(cid);
    let scale = f_p(p, "scale").unwrap_or(1.0).clamp(0.05, 1.0);
    let img = s.render(cid, t, RenderOpts { scale, guides: true, view: s.view_camera(cid).filter(|_| comp.has_3d()), ..Default::default() });
    let (w, h) = (img.width, img.height);
    s.snapshot = Some(Snapshot { comp: cid, time: t, scale, image: std::sync::Arc::new(img) });
    s.events.push(crate::Event::ProjectChanged { revision: s.revision });
    Ok(json!({"width": w, "height": h, "time": t.seconds()}))
}

fn has_snapshot(s: &Session) -> std::result::Result<(), String> {
    if s.snapshot.is_some() { Ok(()) } else { Err("take a snapshot first (Shift+F5)".into()) }
}

fn show_snapshot(s: &mut Session, p: &Value) -> Result<Value> {
    s.state.viewer.show_snapshot = b_p(p, "value").unwrap_or(!s.state.viewer.show_snapshot);
    Ok(json!(s.state.viewer.show_snapshot))
}

fn fast_previews(s: &mut Session, p: &Value) -> Result<Value> {
    let name = str_p(p, "mode").unwrap_or("off");
    let m =
        FastPreviews::from_name(name).ok_or_else(|| bad("view.fastPreviewMode", format!("unknown mode `{name}` (off|adaptive|draft|fastDraft|wireframe)")))?;
    s.state.viewer.fast_previews = m;
    Ok(json!(m.id()))
}

fn guide_index(s: &Session, p: &Value, cmd: &str) -> Result<(lumiere_motion_project::ItemId, usize)> {
    let cid = comp_id(s, p)?;
    let n = s.project.comp(cid).ok_or(EngineError::NoComp)?.guides.len();
    let i = p.get("index").and_then(Value::as_u64).ok_or_else(|| bad(cmd, "missing `index`"))? as usize;
    if i >= n {
        return Err(bad(cmd, format!("no guide {i} (the comp has {n})")));
    }
    Ok((cid, i))
}

fn move_guide(s: &mut Session, p: &Value) -> Result<Value> {
    let (cid, i) = guide_index(s, p, "view.moveGuide")?;
    let pos = f_p(p, "position").ok_or_else(|| bad("view.moveGuide", "missing `position` (comp px)"))?;
    s.edit("Move Guide", merge_p(p), |proj, _| {
        proj.comp_mut(cid).ok_or(EngineError::NoComp)?.guides[i].position = pos;
        Ok(())
    })?;
    Ok(json!(pos))
}

fn remove_guide(s: &mut Session, p: &Value) -> Result<Value> {
    let (cid, i) = guide_index(s, p, "view.removeGuide")?;
    s.edit("Remove Guide", None, |proj, _| {
        proj.comp_mut(cid).ok_or(EngineError::NoComp)?.guides.remove(i);
        Ok(())
    })?;
    Ok(Value::Null)
}

fn pts(v: Option<&Value>) -> Vec<[f64; 2]> {
    v.and_then(Value::as_array).map(|a| a.iter().filter_map(|p| Some([p.get(0)?.as_f64()?, p.get(1)?.as_f64()?])).collect()).unwrap_or_default()
}

fn rgba(v: Option<&Value>, d: [f64; 4]) -> [f64; 4] {
    match v {
        Some(Value::Array(a)) => {
            let g = |i: usize, d: f64| a.get(i).and_then(Value::as_f64).unwrap_or(d);
            [g(0, d[0]), g(1, d[1]), g(2, d[2]), g(3, 1.0)]
        }
        Some(Value::String(h)) => lumiere_motion_color::Rgba::from_hex(h).map(|c| [c.r as f64, c.g as f64, c.b as f64, 1.0]).unwrap_or(d),
        _ => d,
    }
}

/// Pen tool on a shape layer: a new Shape group ("Shape n" with Path 1, Stroke 1 and Fill 1) on
/// the given / selected shape layer, or on a new shape layer when none is given. Vertices are
/// in the layer's space, or comp space (`space: "comp"`, the default for a new layer).
fn new_path(s: &mut Session, p: &Value) -> Result<Value> {
    let cid = comp_id(s, p)?;
    let comp = s.project.comp(cid).ok_or(EngineError::NoComp)?.clone();
    let mut v = pts(p.get("vertices"));
    if v.is_empty() {
        return Err(bad("shape.newPath", "need `vertices` [[x,y], …]"));
    }
    let n = v.len();
    let mut ins = pts(p.get("inTangents"));
    let mut outs = pts(p.get("outTangents"));
    ins.resize(n, [0.0; 2]);
    outs.resize(n, [0.0; 2]);
    let target = match p.get("layer") {
        Some(l) => Some(super::resolve_layer(&comp, l).ok_or_else(|| bad("shape.newPath", format!("no layer {l}")))?),
        None => s.state.selected_layers.iter().copied().find(|l| comp.layer(*l).is_some_and(|l| matches!(l.source, LayerSource::Shape) && !l.switches.locked)),
    };
    if let Some(l) = target.and_then(|l| comp.layer(l))
        && !matches!(l.source, LayerSource::Shape)
    {
        return Err(bad("shape.newPath", "the layer is not a shape layer"));
    }
    let comp_space = str_p(p, "space").map(|s| s == "comp").unwrap_or(target.is_none());
    if comp_space {
        // Comp → layer space of the target (a new layer sits at the comp centre, unrotated).
        let inv = match target.and_then(|l| comp.layer(l)) {
            Some(l) => {
                let ctx = lumiere_motion_render::EvalCtx::new(&s.project, cid, &comp, s.time_of(cid));
                ctx.layer_to_comp(l).0.inverse().unwrap_or(lumiere_motion_geom::Mat3::IDENTITY)
            }
            None => lumiere_motion_geom::Mat3::translate(lumiere_motion_geom::vec2(-(comp.width as f64) / 2.0, -(comp.height as f64) / 2.0)),
        };
        for q in &mut v {
            let r = inv.apply(lumiere_motion_geom::vec2(q[0], q[1]));
            *q = [r.x, r.y];
        }
        for t in ins.iter_mut().chain(outs.iter_mut()) {
            let r = inv.apply_vec(lumiere_motion_geom::vec2(t[0], t[1]));
            *t = [r.x, r.y];
        }
    }
    let path = ShapePath { vertices: v, in_tangents: ins, out_tangents: outs, closed: b_p(p, "closed").unwrap_or(false), feather: Vec::new() };
    let fill = (!matches!(p.get("fill"), Some(Value::Null) | Some(Value::Bool(false)))).then(|| rgba(p.get("fill"), [0.25, 0.55, 1.0, 1.0]));
    let width = f_p(p, "strokeWidth").unwrap_or(2.0);
    let stroke = (width > 0.0).then(|| rgba(p.get("stroke"), [1.0, 1.0, 1.0, 1.0]));
    let name = str_p(p, "name").map(str::to_string);
    let (lid, group, path_uid) = s.edit("Pen Tool", None, |proj, st| {
        let lid = match target {
            Some(l) => l,
            None => {
                let count = comp.layers.iter().filter(|l| matches!(l.source, LayerSource::Shape)).count();
                let lname = name.clone().unwrap_or_else(|| format!("Shape Layer {}", count + 1));
                let l = build::layer(proj, &comp, &lname, LayerSource::Shape, (comp.width, comp.height), None);
                super::layer::insert_layer(proj, st, cid, l)?
            }
        };
        let mut next = proj.next_id;
        let l = super::layer_mut(proj, cid, lid)?;
        let contents = l.props.sub_mut("contents").ok_or_else(|| bad("shape.newPath", "the layer has no contents"))?;
        let k = contents.groups().filter(|g| g.match_id == "group").count();
        let mut ids = Ids(&mut next);
        let pg = build::shape_path(&mut ids, path);
        let path_uid = pg.uid;
        let mut items = vec![pg];
        if let Some(c) = stroke {
            items.push(build::shape_stroke(&mut ids, c, width));
        }
        if let Some(c) = fill {
            items.push(build::shape_fill(&mut ids, c));
        }
        let g = build::shape_group(&mut ids, &format!("Shape {}", k + 1), items);
        let group = g.uid;
        contents.children.insert(0, g.into());
        proj.next_id = next;
        st.selected_layers = vec![lid];
        st.selected_vertices = vec![VertexRef { layer: lid, mask: path_uid, index: n - 1 }];
        Ok((lid, group, path_uid))
    })?;
    Ok(json!({"layer": lid.0, "group": group, "path": path_uid}))
}

/// Snapshot image as straight 8-bit RGBA rows (for frontends / `--out`).
pub fn snapshot_rgba8(s: &Session) -> Option<(u32, u32, Vec<u8>)> {
    let snap = s.snapshot.as_ref()?;
    let img = &snap.image;
    let mut out = Vec::with_capacity(img.data.len() * 4);
    for p in &img.data {
        let a = p[3].clamp(0.0, 1.0);
        let un = |c: f32| if a > 0.0 { (c / a).clamp(0.0, 1.0) } else { 0.0 };
        out.extend([un(p[0]), un(p[1]), un(p[2]), a].map(|v| (v * 255.0 + 0.5) as u8));
    }
    Some((img.width, img.height, out))
}

fn shape_layer_or_none(s: &Session) -> std::result::Result<(), String> {
    has_comp(s)
}

// ---------------------------------------------------------------- display colour, locked viewer

/// View ▸ Use Display Color Management (toggle, or `value`).
fn display_cm(s: &mut Session, p: &Value) -> Result<Value> {
    let v = b_p(p, "value").unwrap_or(!s.state.viewer.display_color_management);
    s.state.viewer.display_color_management = v;
    s.events.push(crate::Event::ProjectChanged { revision: s.revision });
    Ok(json!({"displayColorManagement": v, "workingSpace": s.project.settings.working_space.map(|w| w.id()), "display": s.prefs.previews.display_profile}))
}

/// View ▸ Simulate Output ▸ (profile). `custom` uses (and with `space` sets) the Custom profile.
fn simulate_output(s: &mut Session, p: &Value) -> Result<Value> {
    let cmd = "view.simulateOutput";
    let name = str_p(p, "profile").unwrap_or("none");
    let ids = SimProfile::ALL.iter().map(|p| p.id()).collect::<Vec<_>>().join("|");
    let sim = if name.eq_ignore_ascii_case("custom") {
        let mut c = s.state.viewer.custom_simulation;
        if let Some(sp) = str_p(p, "space") {
            c.profile = SimProfile::parse(sp).ok_or_else(|| bad(cmd, format!("space: {ids}")))?;
        }
        if let Some(v) = b_p(p, "preserveRgb") {
            c.preserve_rgb = v;
        }
        s.state.viewer.custom_simulation = c;
        c
    } else {
        let profile = SimProfile::parse(name).ok_or_else(|| bad(cmd, format!("profile: {ids}|custom")))?;
        Simulation { profile, preserve_rgb: b_p(p, "preserveRgb").unwrap_or(false) }
    };
    s.state.viewer.simulation = sim;
    s.events.push(crate::Event::ProjectChanged { revision: s.revision });
    Ok(json!({"profile": sim.profile.id(), "label": sim.profile.label(), "preserveRgb": sim.preserve_rgb}))
}

fn custom_rgb_json(c: &CustomRgb) -> Value {
    let mut v = serde_json::to_value(c).unwrap_or_default();
    // A LUT-based profile's tables stay in Settings; the reply says it is one.
    if let Some(o) = v.as_object_mut() {
        o.remove("iccLut");
        o.insert("lutProfile".into(), json!(!c.icc_lut.is_empty()));
    }
    v
}

/// View ▸ Simulate Output ▸ My Custom RGB…: define the custom output device (kept in Settings)
/// and, unless `apply` is false, simulate it. An `icc` path that differs from the one the
/// definition came from is read (its primaries, white and curve replace the numbers); `from`
/// starts from a built-in profile; `reset` goes back to the default.
fn custom_rgb(s: &mut Session, p: &Value) -> Result<Value> {
    let cmd = "view.customRgb";
    let mut c = if b_p(p, "reset") == Some(true) { CustomRgb::default() } else { s.prefs.custom_rgb.clone() };
    if let Some(from) = str_p(p, "from") {
        let prof = SimProfile::parse(from).ok_or_else(|| bad(cmd, format!("from: unknown profile `{from}`")))?;
        if let Some((prims, curve)) = prof.space() {
            [c.red, c.green, c.blue] = prims;
            c.white = [0.3127, 0.3290];
            (c.gamma, c.srgb_curve) = match curve {
                crate::viewer::Curve::Srgb => (2.2, true),
                crate::viewer::Curve::Gamma(g) => ((g as f64 * 1000.0).round() / 1000.0, false),
                _ => (1.0, false),
            };
            c.icc.clear();
            c.icc_lut = Default::default();
            c.name = format!("Custom ({})", prof.label());
        }
    }
    let pt = |k: &str| -> Result<Option<[f64; 2]>> {
        match p.get(k) {
            None | Some(Value::Null) => Ok(None),
            Some(v) => {
                let a = v.as_array().filter(|a| a.len() == 2).ok_or_else(|| bad(cmd, format!("{k}: [x, y]")))?;
                let g = |i: usize| a[i].as_f64().ok_or_else(|| bad(cmd, format!("{k}: [x, y] numbers")));
                Ok(Some([g(0)?, g(1)?]))
            }
        }
    };
    let mut typed = false;
    for (k, slot) in [("red", &mut c.red), ("green", &mut c.green), ("blue", &mut c.blue), ("white", &mut c.white)] {
        if let Some(v) = pt(k)? {
            typed |= *slot != v;
            *slot = v;
        }
    }
    if let Some(g) = f_p(p, "gamma") {
        typed |= c.gamma != g;
        c.gamma = g;
    }
    if let Some(b) = b_p(p, "srgbCurve") {
        typed |= c.srgb_curve != b;
        c.srgb_curve = b;
    }
    // Typed-in numbers replace a LUT-based profile's tables (they only approximated it).
    if typed {
        c.icc_lut = Default::default();
    }
    if let Some(n) = str_p(p, "name").map(str::trim).filter(|n| !n.is_empty()) {
        c.name = n.to_string();
    }
    match str_p(p, "icc").map(str::trim) {
        Some("") => {
            c.icc.clear();
            c.icc_lut = Default::default();
        }
        Some(path) if path != c.icc || b_p(p, "reload") == Some(true) => {
            let bytes = s.services.read_file(path).map_err(|e| EngineError::Other(format!("cannot read {path}: {e}")))?;
            let mut from = CustomRgb::from_icc(&bytes, path).map_err(|e| bad(cmd, format!("{path}: {e}")))?;
            if let Some(n) = str_p(p, "name").map(str::trim).filter(|n| !n.is_empty() && *n != s.prefs.custom_rgb.name) {
                from.name = n.to_string();
            }
            c = from;
        }
        _ => {}
    }
    c.validate().map_err(|e| bad(cmd, e))?;
    s.prefs.custom_rgb = c.clone();
    s.prefs_revision += 1;
    s.save_prefs();
    if b_p(p, "apply") != Some(false) {
        let preserve = b_p(p, "preserveRgb").unwrap_or(false);
        s.state.viewer.simulation = Simulation { profile: SimProfile::MyCustom, preserve_rgb: preserve };
    }
    s.events.push(crate::Event::ProjectChanged { revision: s.revision });
    let mut out = custom_rgb_json(&c);
    out["active"] = json!(s.state.viewer.simulation.profile == SimProfile::MyCustom);
    Ok(out)
}

/// View ▸ Split with New Locked Viewer: lock a second viewer to the comp (default the active
/// one) and its current 3D view (or `view`).
fn split_locked(s: &mut Session, p: &Value) -> Result<Value> {
    let cid = comp_id(s, p)?;
    let view = match str_p(p, "view") {
        Some(v) => lumiere_motion_render::three_d::View3D::from_id(v).ok_or_else(|| bad("view.splitLockedViewer", format!("unknown view `{v}`")))?,
        None => s.state.views3d.get(&cid).map(|v| v.current).unwrap_or_default(),
    };
    s.state.locked_viewer = Some(LockedViewer { comp: cid, view });
    s.events.push(crate::Event::ProjectChanged { revision: s.revision });
    Ok(json!({"comp": cid.0, "view": view.id()}))
}

fn close_locked(s: &mut Session, _: &Value) -> Result<Value> {
    let was = s.state.locked_viewer.take().is_some();
    s.events.push(crate::Event::ProjectChanged { revision: s.revision });
    Ok(json!({"closed": was}))
}

fn has_locked(s: &Session) -> std::result::Result<(), String> {
    if s.state.locked_viewer.is_some() { Ok(()) } else { Err("no locked viewer".into()) }
}

/// The viewer's colour settings and what they do.
fn display_color_state(s: &mut Session, _: &Value) -> Result<Value> {
    let v = &s.state.viewer;
    Ok(json!({
        "displayColorManagement": v.display_color_management,
        "simulation": {"profile": v.simulation.profile.id(), "preserveRgb": v.simulation.preserve_rgb},
        "customRgb": custom_rgb_json(&s.prefs.custom_rgb),
        "display": s.prefs.previews.display_profile,
        "workingSpace": s.project.settings.working_space.map(|w| w.id()),
        "active": crate::viewer::DisplayColor::of(s).is_some(),
        "lockedViewer": s.state.locked_viewer.map(|l| json!({"comp": l.comp.0, "view": l.view.id()})),
    }))
}

pub fn specs() -> Vec<CommandSpec> {
    vec![
        cmd!("view.snapping", "Snapping", ["View"], None, "{value?}", always, snapping),
        cmd!("view.displayColorManagement", "Use Display Color Management", ["View"], None, "{value?}", always, display_cm),
        cmd!(
            "view.simulateOutput",
            "Simulate Output",
            [],
            None,
            "{profile: none|rec709|ntsc|pal|mac18|srgb|rec2020|p3|linear|myCustom|custom, preserveRgb?, space? (custom)}",
            always,
            simulate_output
        ),
        cmd!(
            "view.customRgb",
            "My Custom RGB...",
            [],
            None,
            "{name?, red?/green?/blue?/white?: [x, y] (CIE xy), gamma?, srgbCurve?: bool, icc?: path (RGB ICC profile, matrix/TRC or LUT-based A2B0: read when it changes, \"\" clears; a LUT profile simulates through its tables, `lutProfile` in the reply; typed-in numbers replace it), reload?, from?: a Simulate Output profile to start from, reset?, preserveRgb?, apply?: bool (default true: simulate it)} → the definition (kept in Settings)",
            always,
            custom_rgb
        ),
        cmd!("view.splitLockedViewer", "Split with New Locked Viewer", ["View"], None, "{comp?, view?: 3D view id}", has_comp, split_locked),
        cmd!("view.closeLockedViewer", "Close Locked Viewer", [], None, "{}", has_locked, close_locked),
        query!(
            "view.displayColor",
            "Viewer Color State",
            "{} → display colour management, output simulation, display profile, locked viewer",
            display_color_state
        ),
        cmd!("view.channel", "Show Channel", [], None, "{channel: rgb|red|green|blue|alpha|rgbStraight, colorized?, toggle?}", always, channel),
        cmd!("view.exposure", "Adjust Exposure", [], None, "{stops? | delta?}", always, exposure),
        cmd!("view.resetExposure", "Reset Exposure", [], None, "{}", always, reset_exposure),
        cmd!("view.takeSnapshot", "Take Snapshot", [], Some("Shift+F5"), "{comp?, scale?: 0.05..1}", has_comp, take_snapshot),
        cmd!("view.showSnapshot", "Show Snapshot", [], None, "{value?}", has_snapshot, show_snapshot),
        cmd!("view.fastPreviewMode", "Fast Previews", [], None, "{mode: off|adaptive|draft|fastDraft|wireframe}", always, fast_previews),
        cmd!("view.moveGuide", "Move Guide", [], None, "{comp?, index, position (comp px), merge?}", has_comp, move_guide),
        cmd!("view.removeGuide", "Remove Guide", [], None, "{comp?, index}", has_comp, remove_guide),
        cmd!(
            "shape.newPath",
            "Pen Tool (Shape Path)",
            [],
            None,
            "{layer?, vertices: [[x,y]…], inTangents?, outTangents?, closed?, space?: comp|layer, fill?: [r,g,b]|#hex|false, stroke?, strokeWidth?, name?}",
            shape_layer_or_none,
            new_path
        ),
    ]
}
