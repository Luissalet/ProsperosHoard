//! Shape Stroke options: the Dashes "+" / "−" buttons (Dash 2 / Gap 2, Dash 3 / Gap 3), Taper and
//! Wave. Taper and Wave are ordinary properties of the stroke (`…/stroke/taper/startLength`); the
//! commands here set several of them in one undo step and add the groups to strokes saved before
//! they existed.

use lumiere_motion_keyframe::Value as KV;
use lumiere_motion_project::build::{self, EXTRA_DASHES, Ids};
use lumiere_motion_project::{ItemId, LayerId, PropGroup, Uid};
use serde_json::{Value, json};

use super::{CommandSpec, bad, has_layers, layer_mut, layer_p, merge_p};
use crate::{EngineError, Result, Session, cmd};

fn is_stroke(g: &PropGroup) -> bool {
    matches!(g.match_id.as_str(), "stroke" | "gstroke")
}

/// `{layer?, prop?: stroke uid}` → the stroke; without `prop`, the first stroke of the layer (a
/// selected stroke property wins).
fn stroke_ref(s: &Session, p: &Value, cmd: &str) -> Result<(ItemId, LayerId, Uid)> {
    let (cid, lid) = layer_p(s, p, cmd)?;
    let l = s.project.comp(cid).and_then(|c| c.layer(lid)).ok_or(EngineError::NoComp)?;
    if let Some(uid) = p.get("prop").and_then(Value::as_u64) {
        let g = l.props.find_group(uid).ok_or_else(|| bad(cmd, format!("no property group @{uid}")))?;
        if !is_stroke(g) {
            return Err(bad(cmd, format!("`{}` is not a Stroke or Gradient Stroke", g.name)));
        }
        return Ok((cid, lid, uid));
    }
    let contents = l.props.sub("contents").ok_or_else(|| bad(cmd, "not a shape layer"))?;
    // A selected stroke (or a property inside one), else the first stroke in the contents.
    let mut found = None;
    for (sl, su) in &s.state.selected_props {
        if *sl == lid
            && let Some(g) = find_stroke_containing(contents, *su)
        {
            found = Some(g);
            break;
        }
    }
    if found.is_none() {
        let mut first = None;
        walk_groups(contents, &mut |g| {
            if first.is_none() && is_stroke(g) {
                first = Some(g.uid);
            }
        });
        found = first;
    }
    found.map(|u| (cid, lid, u)).ok_or_else(|| bad(cmd, "the layer has no stroke"))
}

fn walk_groups(g: &PropGroup, f: &mut impl FnMut(&PropGroup)) {
    for sub in g.groups() {
        f(sub);
        walk_groups(sub, f);
    }
}

fn find_stroke_containing(contents: &PropGroup, uid: Uid) -> Option<Uid> {
    let mut out = None;
    walk_groups(contents, &mut |g| {
        if out.is_none() && is_stroke(g) && (g.uid == uid || g.find(uid).is_some() || g.find_group(uid).is_some()) {
            out = Some(g.uid);
        }
    });
    out
}

fn dashes_add(s: &mut Session, p: &Value) -> Result<Value> {
    let c = "shape.dashes.add";
    let (cid, lid, uid) = stroke_ref(s, p, c)?;
    s.edit("Add Dash", None, |proj, _| {
        let mut next = proj.next_id;
        let g = layer_mut(proj, cid, lid)?.props.find_group_mut(uid).ok_or_else(|| bad(c, "stroke vanished"))?;
        let d = g.sub_mut("dashes").ok_or_else(|| bad(c, "the stroke has no Dashes group"))?;
        // Before the first "+", Dash is 0 (no dashes): the first "+" turns it on.
        let dash = d.get("dash").map(|p| p.value.as_f64()).unwrap_or(0.0);
        let mut ids = Ids(&mut next);
        let added = if dash <= 0.0 {
            if let Some(p) = d.get_mut("dash") {
                p.value = KV::Scalar(10.0);
            }
            "dash"
        } else {
            let Some((dm, gm)) = EXTRA_DASHES.iter().find(|(dm, _)| d.get(dm).is_none()) else {
                return Err(bad(c, "a stroke has at most three dash/gap pairs"));
            };
            let k = if *dm == "dash2" { 2 } else { 3 };
            // Insert before Offset, which stays last.
            let at = d.children.iter().position(|n| n.match_id() == "offset").unwrap_or(d.children.len());
            let dv = d.get("dash").map(|p| p.value.as_f64()).unwrap_or(10.0);
            let gv = d.get("gap").map(|p| p.value.as_f64()).filter(|g| *g > 0.0).unwrap_or(dv);
            d.children.insert(at, ids.prop(gm, &format!("Gap {k}"), KV::Scalar(gv)).with_ui(lumiere_motion_project::ParamUi::Pixels).into());
            d.children.insert(at, ids.prop(dm, &format!("Dash {k}"), KV::Scalar(dv)).with_ui(lumiere_motion_project::ParamUi::Pixels).into());
            dm
        };
        proj.next_id = next;
        Ok(json!({"added": added}))
    })
}

fn dashes_remove(s: &mut Session, p: &Value) -> Result<Value> {
    let c = "shape.dashes.remove";
    let (cid, lid, uid) = stroke_ref(s, p, c)?;
    s.edit("Remove Dash", None, |proj, _| {
        let g = layer_mut(proj, cid, lid)?.props.find_group_mut(uid).ok_or_else(|| bad(c, "stroke vanished"))?;
        let d = g.sub_mut("dashes").ok_or_else(|| bad(c, "the stroke has no Dashes group"))?;
        for (dm, gm) in EXTRA_DASHES.iter().rev() {
            if d.get(dm).is_some() {
                d.children.retain(|n| n.match_id() != *dm && n.match_id() != *gm);
                return Ok(json!({"removed": dm}));
            }
        }
        // The last "−" turns dashes off.
        if let Some(p) = d.get_mut("dash") {
            p.value = KV::Scalar(0.0);
            p.keys.clear();
        }
        Ok(json!({"removed": "dash"}))
    })
}

/// Set `fields` (`param name → (match id, kind)`) of the stroke's `group`, creating the group.
fn set_group(s: &mut Session, p: &Value, c: &str, group: &str, fields: &[(&str, &str, bool)]) -> Result<Value> {
    let (cid, lid, uid) = stroke_ref(s, p, c)?;
    let t = s.time();
    let label = if group == "taper" { "Stroke Taper" } else { "Stroke Wave" };
    s.edit(label, merge_p(p), |proj, _| {
        let mut next = proj.next_id;
        {
            let g = layer_mut(proj, cid, lid)?.props.find_group_mut(uid).ok_or_else(|| bad(c, "stroke vanished"))?;
            if g.sub(group).is_none() {
                let mut ids = Ids(&mut next);
                let ng = if group == "taper" { build::stroke_taper(&mut ids) } else { build::stroke_wave(&mut ids) };
                g.children.push(ng.into());
            }
        }
        proj.next_id = next;
        let l = layer_mut(proj, cid, lid)?;
        let lt = l.layer_time(t);
        let g = l.props.find_group_mut(uid).ok_or_else(|| bad(c, "stroke vanished"))?;
        let sub = g.sub_mut(group).ok_or_else(|| bad(c, "group vanished"))?;
        let mut out = serde_json::Map::new();
        for (param, m, is_enum) in fields {
            let Some(v) = p.get(*param) else { continue };
            let pr = sub.get_mut(m).ok_or_else(|| bad(c, format!("no {m}")))?;
            let nv = if *is_enum {
                let opts: &[&str] = if *m == "units" && group == "taper" { &["pixels", "percent"] } else { &["pixels", "cycles"] };
                let i = match v {
                    Value::String(s) => {
                        opts.iter().position(|o| o.eq_ignore_ascii_case(s)).ok_or_else(|| bad(c, format!("`{param}` is one of {opts:?}")))? as u32
                    }
                    Value::Number(n) => n.as_u64().unwrap_or(0).min(opts.len() as u64 - 1) as u32,
                    _ => return Err(bad(c, format!("bad `{param}`"))),
                };
                KV::Enum(i)
            } else {
                let x = v.as_f64().ok_or_else(|| bad(c, format!("`{param}` must be a number")))?;
                let x = if let lumiere_motion_project::ParamUi::Slider { min, max, .. } = pr.ui { x.clamp(min, max) } else { x.max(0.0) };
                KV::Scalar(x)
            };
            pr.set_value_at(lt, nv.clone());
            out.insert(param.to_string(), nv.to_json());
        }
        Ok(json!({"stroke": uid, group: Value::Object(out)}))
    })
}

fn taper(s: &mut Session, p: &Value) -> Result<Value> {
    set_group(
        s,
        p,
        "shape.stroke.taper",
        "taper",
        &[
            ("units", "units", true),
            ("startLength", "startLength", false),
            ("endLength", "endLength", false),
            ("startWidth", "startWidth", false),
            ("endWidth", "endWidth", false),
            ("startEase", "startEase", false),
            ("endEase", "endEase", false),
        ],
    )
}

fn wave(s: &mut Session, p: &Value) -> Result<Value> {
    set_group(
        s,
        p,
        "shape.stroke.wave",
        "wave",
        &[("amount", "amount", false), ("units", "units", true), ("wavelength", "wavelength", false), ("cycles", "cycles", false), ("phase", "phase", false)],
    )
}

pub fn specs() -> Vec<CommandSpec> {
    vec![
        cmd!("shape.dashes.add", "Add Dash or Gap", [], None, "{layer?, prop?: stroke uid}", has_layers, dashes_add),
        cmd!("shape.dashes.remove", "Remove Dash or Gap", [], None, "{layer?, prop?: stroke uid}", has_layers, dashes_remove),
        cmd!(
            "shape.stroke.taper",
            "Stroke Taper",
            [],
            None,
            "{layer?, prop?: stroke uid, units?: pixels|percent, startLength?, endLength?, startWidth? (%), endWidth? (%), startEase? (%), endEase? (%)}",
            has_layers,
            taper
        ),
        cmd!(
            "shape.stroke.wave",
            "Stroke Wave",
            [],
            None,
            "{layer?, prop?: stroke uid, amount? (%), units?: pixels|cycles, wavelength?, cycles?, phase? (°)}",
            has_layers,
            wave
        ),
    ]
}
