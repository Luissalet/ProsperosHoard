import test from 'node:test';
import assert from 'node:assert/strict';
import { frameTimeAtPlayhead } from '../src/frameTime.ts';

test('a frame selection retains the exact playhead time near a transition or the end', () => {
  assert.equal(frameTimeAtPlayhead(.9999, 2), .9999);
  assert.equal(frameTimeAtPlayhead(1.9999999, 2), 1.9999999);
  assert.equal(frameTimeAtPlayhead(1.23456789, 2), 1.23456789);
});

test('an ended playhead selects the final representable interval without a fixed FPS offset', () => {
  for (const duration of [2, 5.041667, .00001, 3600, Number.MIN_VALUE]) {
    const picked = frameTimeAtPlayhead(duration, duration);
    assert.ok(picked >= 0 && picked < duration);
    assert.equal(frameTimeAtPlayhead(duration + 1, duration), picked);
    assert.ok(duration - picked <= Math.max(Number.MIN_VALUE, duration * Number.EPSILON));
  }
  assert.equal(frameTimeAtPlayhead(NaN, 2), 0);
});
