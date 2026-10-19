//! Generic edits of property-tree groups by uid: rename, enable, remove, duplicate and reorder an
//! effect, mask, shape item or text animator (what scripting's `PropertyGroup.name`, `enabled`,
//! `remove()`, `duplicate()` and `moveTo()` do, and what agents need without a UI selection).

use lumiere_motion_project::{GroupKind, ItemId, LayerId, Node, PropGroup, Uid};
use serde_json::{Value, json};

use super::{CommandSpec, b_p, bad, has_layers, layer_mut, layer_p, str_p};
use crate::{EngineError, Result, Session, cmd};

/// Groups that can be renamed, removed, duplicated and reordered: instances in indexed lists.
fn is_instance(g: &PropGroup) -> bool {
    matches!(g.kind, GroupKind::Indexed | GroupKind::Effect { .. } | GroupKind::Mask { .. } | GroupKind::Tracker { .. })
}

/// `{layer, prop: uid}` → (comp, layer, group uid), checking that the group is an instance.
fn group_ref(s: &Session, p: &Value, cmd: &str, need_instance: bool) -> Result<(ItemId, LayerId, Uid)> {
    let (cid, lid) = layer_p(s, p, cmd)?;
    let uid = p.get("prop").and_then(Value::as_u64).ok_or_else(|| bad(cmd, "missing `prop` (group uid)"))?;
    let l = s.project.comp(cid).and_then(|c| c.layer(lid)).ok_or(EngineError::NoComp)?;
    let g = l.props.find_group(uid).ok_or_else(|| bad(cmd, format!("no property group @{uid}")))?;
    if need_instance && !is_instance(g) {
        return Err(bad(cmd, format!("`{}` is a fixed group (only effects, masks, shape items and animators)", g.name)));
    }
    Ok((cid, lid, uid))
}

fn rename(s: &mut Session, p: &Value) -> Result<Value> {
    let c = "prop.renameGroup";
    let (cid, lid, uid) = group_ref(s, p, c, true)?;
    let name = str_p(p, "name").ok_or_else(|| bad(c, "missing `name`"))?.to_string();
    if name.trim().is_empty() {
        return Err(bad(c, "the name can't be empty"));
    }
    s.edit("Rename", None, |proj, _| {
        let g = layer_mut(proj, cid, lid)?.props.find_group_mut(uid).ok_or_else(|| bad(c, "group vanished"))?;
        g.name = name.clone();
        Ok(json!(name))
    })
}

fn set_enabled(s: &mut Session, p: &Value) -> Result<Value> {
    let c = "prop.setGroupEnabled";
    let (cid, lid, uid) = group_ref(s, p, c, true)?;
    let v = b_p(p, "value");
    s.edit("Toggle", None, |proj, _| {
        let g = layer_mut(proj, cid, lid)?.props.find_group_mut(uid).ok_or_else(|| bad(c, "group vanished"))?;
        g.enabled = v.unwrap_or(!g.enabled);
        Ok(json!(g.enabled))
    })
}

fn remove(s: &mut Session, p: &Value) -> Result<Value> {
    let c = "prop.removeGroup";
    let (cid, lid, uid) = group_ref(s, p, c, true)?;
    s.edit("Delete", None, |proj, st| {
        let l = layer_mut(proj, cid, lid)?;
        let parent = l.props.parent_of_mut(uid).ok_or_else(|| bad(c, "group vanished"))?;
        parent.children.retain(|n| n.uid() != uid);
        st.selected_props.retain(|(_, u)| *u != uid);
        Ok(Value::Null)
    })
}

fn duplicate(s: &mut Session, p: &Value) -> Result<Value> {
    let c = "prop.duplicateGroup";
    let (cid, lid, uid) = group_ref(s, p, c, true)?;
    s.edit("Duplicate", None, |proj, _| {
        let mut next = proj.next_id;
        let l = layer_mut(proj, cid, lid)?;
        let parent = l.props.parent_of_mut(uid).ok_or_else(|| bad(c, "group vanished"))?;
        let i = parent.children.iter().position(|n| n.uid() == uid).ok_or_else(|| bad(c, "group vanished"))?;
        let Node::Group(mut g) = parent.children[i].clone() else { return Err(bad(c, "not a group")) };
        g.reassign_uids(&mut next);
        let new = g.uid;
        parent.children.insert(i + 1, Node::Group(g));
        proj.next_id = next + 1;
        Ok(json!({"prop": new}))
    })
}

fn move_to(s: &mut Session, p: &Value) -> Result<Value> {
    let c = "prop.moveGroup";
    let (cid, lid, uid) = group_ref(s, p, c, true)?;
    let to = p.get("index").and_then(Value::as_u64).ok_or_else(|| bad(c, "missing `index` (1-based)"))? as usize;
    s.edit("Reorder", None, |proj, _| {
        let l = layer_mut(proj, cid, lid)?;
        let parent = l.props.parent_of_mut(uid).ok_or_else(|| bad(c, "group vanished"))?;
        let i = parent.children.iter().position(|n| n.uid() == uid).ok_or_else(|| bad(c, "group vanished"))?;
        if to < 1 || to > parent.children.len() {
            return Err(bad(c, format!("index {to} is out of range 1..{}", parent.children.len())));
        }
        let n = parent.children.remove(i);
        parent.children.insert(to - 1, n);
        Ok(json!(to))
    })
}

pub fn specs() -> Vec<CommandSpec> {
    vec![
        cmd!("prop.renameGroup", "Rename Property Group", [], None, "{layer?, prop: uid, name}", has_layers, rename),
        cmd!("prop.setGroupEnabled", "Enable Property Group", [], None, "{layer?, prop: uid, value?}", has_layers, set_enabled),
        cmd!("prop.removeGroup", "Delete Property Group", [], None, "{layer?, prop: uid}", has_layers, remove),
        cmd!("prop.duplicateGroup", "Duplicate Property Group", [], None, "{layer?, prop: uid} → {prop: new uid}", has_layers, duplicate),
        cmd!("prop.moveGroup", "Reorder Property Group", [], None, "{layer?, prop: uid, index (1-based among its siblings)}", has_layers, move_to),
    ]
}
