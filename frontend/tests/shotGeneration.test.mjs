import test from 'node:test';
import assert from 'node:assert/strict';
import { shotGenerationStatus } from '../src/shotGeneration.ts';

test('an empty shot remains incomplete after any production job finishes', () => {
  assert.equal(shotGenerationStatus(undefined, undefined, true), 'missing_frame_and_clip');
  assert.equal(shotGenerationStatus('frame-a', undefined, true), 'missing_clip');
});

test('a still shot needs its frame but does not require a video clip', () => {
  assert.equal(shotGenerationStatus(undefined, undefined, false), 'missing_frame');
  assert.equal(shotGenerationStatus('frame-a', undefined, false), 'ready_still');
});

test('readiness follows the actual outputs and notices a missing start frame', () => {
  assert.equal(shotGenerationStatus('frame-a', 'clip-a', true), 'ready_clip');
  assert.equal(shotGenerationStatus(undefined, 'clip-a', true), 'missing_frame');
});
