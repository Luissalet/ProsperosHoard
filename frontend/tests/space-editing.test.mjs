import test from 'node:test';
import assert from 'node:assert/strict';
import { EditHistory, SaveQueue, freeNodePosition } from '../src/spaceEditing.ts';

test('new nodes leave room for the actual occupied card sizes', () => {
  const cards = [{x: 0, y: 0, width: 340, height: 780}, {x: 380, y: 0, width: 340, height: 500}];
  const position = freeNodePosition({x: 30, y: 20}, {width: 280, height: 400}, cards);
  assert.ok(cards.every(r => position.x + 280 + 36 <= r.x || position.x >= r.x + r.width + 36 || position.y + 400 + 36 <= r.y || position.y >= r.y + r.height + 36));
  assert.deepEqual(freeNodePosition({x: 1000, y: 0}, {width: 280, height: 400}, cards), {x: 1000, y: 0});
});
test('very large occupied areas still yield a free position', () => {
  assert.deepEqual(freeNodePosition({x: 0, y: 0}, {width: 300, height: 400}, [{x: -10000, y: -10000, width: 20000, height: 20000}]), {x: 10036, y: 0});
});

test('undo restores a removed node and its wires together, without retaining mutable references', () => {
  const original = {nodes: [{id: 'person', data: {role: 'identity'}}], edges: [{source: 'person', target: 'image'}]};
  const history = new EditHistory(original);
  original.nodes[0].data.role = 'changed externally';
  history.commit({nodes: [], edges: []});
  const undone = history.undo();
  assert.equal(undone.nodes[0].data.role, 'identity');
  assert.equal(undone.edges.length, 1);
  undone.nodes[0].data.role = 'changed undo result';
  assert.deepEqual(history.redo(), {nodes: [], edges: []});
  assert.equal(history.undo().nodes[0].data.role, 'identity');
});
test('typing coalesces within one field; different fields and a pause form separate steps', () => {
  const history = new EditHistory('');
  history.commit('a', 'prompt', 1000); history.commit('ab', 'prompt', 1100);
  history.commit('abc', 'prompt', 1900); history.commit('title', 'name', 2000);
  assert.equal(history.undo(), 'abc'); assert.equal(history.undo(), 'ab'); assert.equal(history.undo(), '');
});
test('new edits after undo discard redo and the history is bounded', () => {
  const history = new EditHistory(0, 2);
  history.commit(1); history.commit(2); history.commit(3);
  assert.equal(history.undo(), 2); history.commit(9);
  assert.equal(history.canRedo, false); assert.equal(history.undo(), 2);
  assert.equal(history.undo(), 1); assert.equal(history.undo(), null);
  history.reset(20); assert.equal(history.canUndo, false);
});
test('an unchanged snapshot creates no undo entry', () => {
  const history = new EditHistory({nodes: []});
  assert.equal(history.commit({nodes: []}), false); assert.equal(history.canUndo, false);
});
test('failed saves return false, remain dirty and do not permit the dependent action', async () => {
  let fail = true; let errors = 0; let saved = 0; let runs = 0;
  const queue = new SaveQueue({read: () => 'latest graph', write: async () => { if (fail) throw new Error('503'); }, onError: () => errors++, onSaved: () => saved++});
  queue.changed(); if (await queue.flush()) runs++;
  assert.equal(runs, 0); assert.equal(queue.isDirty, true); assert.equal(errors, 1); assert.equal(saved, 0);
  fail = false; assert.equal(await queue.flush(), true); assert.equal(queue.isDirty, false); assert.equal(saved, 1);
});
test('an edit during a slow save is drained before success, and concurrent flushes serialize', async () => {
  let current = 'first'; const writes = []; let release;
  const queue = new SaveQueue({read: () => current, write: async (value) => { writes.push(value); if (value === 'first') await new Promise(resolve => { release = resolve; }); }, onError: assert.fail, onSaved: () => {}});
  queue.changed(); const first = queue.flush();
  await new Promise(resolve => setImmediate(resolve));
  current = 'newer'; queue.changed(); const second = queue.flush(); release();
  assert.deepEqual(await Promise.all([first, second]), [true, true]); assert.deepEqual(writes, ['first', 'newer']); assert.equal(queue.isDirty, false);
});
test('failure on the newer edit retains the unsaved revision until retry', async () => {
  let current = 'first'; let release; let fail = true; const writes = [];
  const queue = new SaveQueue({read: () => current, write: async value => { writes.push(value); if (value === 'first') await new Promise(resolve => { release = resolve; }); else if (fail) throw new Error('conflict'); }, onError: () => {}, onSaved: () => {}});
  queue.changed(); const saving = queue.flush(); await new Promise(resolve => setImmediate(resolve));
  current = 'newer'; queue.changed(); release();
  assert.equal(await saving, false); assert.equal(queue.isDirty, true);
  fail = false; assert.equal(await queue.flush(), true); assert.deepEqual(writes, ['first', 'newer', 'newer']);
});
