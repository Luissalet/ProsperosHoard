import test from 'node:test';
import assert from 'node:assert/strict';
import { flattenTranscriptWords, formatCutBy, wordRangeAfterClick } from '../src/voiceRetakeUi.ts';

test('flattenTranscriptWords drops blanks and keeps timings', () => {
  const words = flattenTranscriptWords([
    { words: [{ start_s: 0, end_s: 0.3, word: 'Mañana' }, { start_s: 0.3, end_s: 0.6, word: '  ' }] },
    { words: [{ start_s: 0.6, end_s: 1.0, word: 'iremos' }] },
  ]);
  assert.deepEqual(words, [
    { start_s: 0, end_s: 0.3, word: 'Mañana' },
    { start_s: 0.6, end_s: 1.0, word: 'iremos' },
  ]);
});

test('formatCutBy renders #288 array cut_by sides', () => {
  const label = formatCutBy(
    { cut_start_s: 0.3, cut_end_s: 0.6, cut_by: ['valley', 'timing'] },
    { start: 'start', end: 'end' },
  );
  assert.equal(label, 'start: valley · end: timing');
  assert.equal(formatCutBy({ cut_start_s: 0, cut_end_s: 1 }, { start: 'start', end: 'end' }), null);
});

test('wordRangeAfterClick builds a contiguous selection in two clicks', () => {
  const first = wordRangeAfterClick(0, null, null, null);
  assert.deepEqual(first, { selAnchor: 0, selFirst: 0, selLast: 0 });
  const range = wordRangeAfterClick(3, first.selAnchor, first.selFirst, first.selLast);
  assert.deepEqual(range, { selAnchor: null, selFirst: 0, selLast: 3 });
  const reset = wordRangeAfterClick(1, range.selAnchor, range.selFirst, range.selLast);
  assert.deepEqual(reset, { selAnchor: 1, selFirst: 1, selLast: 1 });
});
