import test from 'node:test';
import assert from 'node:assert/strict';
import { NODE_TOOLS, acceptsToolInput, searchNodeTools } from '../src/nodeTools.ts';

const choices = NODE_TOOLS.map(tool => ({kind: tool.kind, tool, data: tool.data}));

test('tools keep every native node function accessible', () => {
  assert.deepEqual([...new Set(NODE_TOOLS.map(t => t.kind))].sort(),
    ['text','asset','cast','image','video','music','list','note','assistant','edit','combine','variations','group','composite','clip_edit','frame'].sort());
  assert.equal(new Set(NODE_TOOLS.map(t => t.id)).size, NODE_TOOLS.length);
});

test('search accepts accents, both languages and multiple words regardless of order', () => {
  assert.deepEqual(searchNodeTools(choices, 'iluminacion').map(c => c.tool.id), ['lighting']);
  assert.deepEqual(searchNodeTools(choices, 'background remove').map(c => c.tool.id), ['background']);
  assert.deepEqual(searchNodeTools(choices, 'POSE extraer').map(c => c.tool.id), ['pose']);
  assert.deepEqual(searchNodeTools(choices, 'fotograma extraer').map(c => c.tool.id), ['frame']);
  assert.deepEqual(searchNodeTools(choices, '   ', 'assemble').map(c => c.tool.id), ['composite', 'combine']);
  assert.deepEqual(searchNodeTools(choices, 'lighting', 'sources'), []);
  assert.deepEqual(searchNodeTools(choices, 'no-such-tool'), []);
});

test('a wired starter accepts its own guide and shared prompt but never a conflicting guide', () => {
  const tool = id => NODE_TOOLS.find(t => t.id === id);
  for (const [id, guide] of [['motion','motion'],['sing','audio'],['first-last','end']]) {
    assert.equal(acceptsToolInput(tool(id),'start'),true);
    assert.equal(acceptsToolInput(tool(id),'prompt'),true);
    for (const port of ['motion','audio','end']) assert.equal(acceptsToolInput(tool(id),port),port === guide);
  }
  assert.equal(acceptsToolInput(tool('video'),'audio'),true);
  assert.equal(acceptsToolInput(undefined,'image'),true);
});
