//! Project panel item edits: move into folders, rename, label, comment, with undo.

use lumiere_motion_project::ItemId;
use serde_json::json;

use crate::Session;

#[test]
fn move_into_folder_rename_label_comment_with_undo() {
    let mut s = Session::default();
    s.execute("comp.new", json!({"name": "A", "width": 64, "height": 64, "frameRate": 24, "duration": 1})).unwrap();
    let comp = s.active_comp_id().unwrap();
    let f = ItemId(s.execute("project.newFolder", json!({"name": "Shots"})).unwrap()["item"].as_u64().unwrap());
    let g = ItemId(s.execute("project.newFolder", json!({"name": "Inner", "parent": f.0})).unwrap()["item"].as_u64().unwrap());

    s.execute("project.move", json!({"items": [comp.0], "folder": "Shots"})).unwrap();
    assert_eq!(s.project.item(comp).unwrap().parent, Some(f));
    s.execute("edit.undo", json!({})).unwrap();
    assert_eq!(s.project.item(comp).unwrap().parent, None);
    s.execute("edit.redo", json!({})).unwrap();
    // Out of the folder (to the root) via the selection.
    s.execute("project.select", json!({"items": [comp.0]})).unwrap();
    s.execute("project.move", json!({"folder": null})).unwrap();
    assert_eq!(s.project.item(comp).unwrap().parent, None);
    // A folder can't go into itself or its descendants.
    assert!(s.execute("project.move", json!({"items": [f.0], "folder": g.0})).is_err());
    assert!(s.execute("project.move", json!({"items": [comp.0], "folder": comp.0})).is_err());

    s.execute("project.rename", json!({"item": comp.0, "name": "Hero"})).unwrap();
    assert_eq!(s.project.item(comp).unwrap().name, "Hero");
    assert!(s.execute("project.rename", json!({"item": comp.0, "name": "  "})).is_err());
    s.execute("edit.undo", json!({})).unwrap();
    assert_eq!(s.project.item(comp).unwrap().name, "A");

    s.execute("project.setLabel", json!({"items": [comp.0], "label": "Pink"})).unwrap();
    assert_eq!(s.project.item(comp).unwrap().label.name(), "Pink");
    s.execute("project.setComment", json!({"item": comp.0, "comment": "final"})).unwrap();
    assert_eq!(s.project.item(comp).unwrap().comment, "final");
    s.execute("edit.undo", json!({})).unwrap();
    assert_eq!(s.project.item(comp).unwrap().comment, "");
}

#[test]
fn delete_and_duplicate_items() {
    let mut s = Session::default();
    s.execute("comp.new", json!({"name": "Inner", "width": 64, "height": 64, "duration": 1})).unwrap();
    let inner = s.active_comp_id().unwrap();
    s.execute("layer.newNull", json!({})).unwrap();
    s.execute("comp.new", json!({"name": "Outer", "width": 64, "height": 64, "duration": 1})).unwrap();
    let outer = s.active_comp_id().unwrap();
    s.execute("layer.addItem", json!({"item": inner.0})).unwrap();
    s.execute("renderQueue.add", json!({"comp": inner.0})).unwrap();
    // Duplicate: a new comp with fresh layer ids.
    let r = s.execute("project.duplicate", json!({"items": [inner.0]})).unwrap();
    let dup = ItemId(r["items"][0].as_u64().unwrap());
    assert_eq!(s.project.item(dup).unwrap().name, "Inner 2");
    let a = s.project.comp(inner).unwrap().layers[0].id;
    let b = s.project.comp(dup).unwrap().layers[0].id;
    assert_ne!(a, b);
    // Delete: layers that use the item and its render items go too; undoable.
    s.execute("project.delete", json!({"items": ["Inner"]})).unwrap();
    assert!(s.project.item(inner).is_none());
    assert!(s.project.comp(outer).unwrap().layers.is_empty());
    assert!(s.project.render_queue.is_empty());
    s.undo();
    assert!(s.project.item(inner).is_some());
    assert_eq!(s.project.comp(outer).unwrap().layers.len(), 1);
    // Folders take their contents.
    let f = ItemId(s.execute("project.newFolder", json!({"name": "F"})).unwrap()["item"].as_u64().unwrap());
    s.execute("project.move", json!({"items": [dup.0], "folder": f.0})).unwrap();
    s.execute("project.delete", json!({"items": [f.0]})).unwrap();
    assert!(s.project.item(dup).is_none());
}

#[test]
fn property_group_edits() {
    let mut s = Session::default();
    s.execute("comp.new", json!({"name": "C", "width": 64, "height": 64, "duration": 1})).unwrap();
    let l = s.execute("layer.newSolid", json!({})).unwrap()["layer"].as_u64().unwrap();
    let a = s.execute("effect.apply", json!({"layer": l, "effect": "Gaussian Blur"})).unwrap()["effects"][0].as_u64().unwrap();
    let b = s.execute("effect.apply", json!({"layer": l, "effect": "Invert"})).unwrap()["effects"][0].as_u64().unwrap();
    let fx = |s: &Session| -> Vec<(u64, String, bool)> {
        s.active_comp().unwrap().layers[0].props.sub("effects").unwrap().groups().map(|g| (g.uid, g.name.clone(), g.enabled)).collect()
    };
    s.execute("prop.renameGroup", json!({"layer": l, "prop": a, "name": "Soft"})).unwrap();
    s.execute("prop.setGroupEnabled", json!({"layer": l, "prop": a, "value": false})).unwrap();
    s.execute("prop.moveGroup", json!({"layer": l, "prop": b, "index": 1})).unwrap();
    let d = s.execute("prop.duplicateGroup", json!({"layer": l, "prop": a})).unwrap()["prop"].as_u64().unwrap();
    let v = fx(&s);
    assert_eq!(v.iter().map(|x| x.0).collect::<Vec<_>>(), [b, a, d]);
    assert_eq!(v[1].1, "Soft");
    assert!(!v[1].2);
    s.execute("prop.removeGroup", json!({"layer": l, "prop": a})).unwrap();
    assert_eq!(fx(&s).len(), 2);
    // Fixed groups can't be removed.
    let tr = s.active_comp().unwrap().layers[0].props.sub("transform").unwrap().uid;
    assert!(s.execute("prop.removeGroup", json!({"layer": l, "prop": tr})).is_err());
}
