import test from 'node:test';
import assert from 'node:assert/strict';
import { rowsFromLyrics, stampNext, timingIssue } from '../src/lyricTiming.ts';

test('timing a repeated lyric keeps occurrences and section tags distinct', () => {
  const rows = rowsFromLyrics('[Verse]\nOne\n[Chorus]\nAgain!\nAgain!', [
    {text:'One',time_s:1,end_s:2},{text:'Again!',time_s:10,end_s:12},{text:'Again!',time_s:20,end_s:22}]);
  assert.deepEqual(rows.map(x=>[x.start_s,x.section]),[[1,'Verse'],[10,'Chorus'],[20,'Chorus']]);
});
test('tapping all line starts and the final end produces complete ordered intervals', () => {
  const original = rowsFromLyrics('One\nTwo');
  const a = stampNext(original,0,1.25), b = stampNext(a.rows,a.next,3.5), c = stampNext(b.rows,b.next,5);
  assert.deepEqual(c.rows.map(x=>[x.start_s,x.end_s]),[[1.25,3.5],[3.5,5]]);
  assert.equal(c.finished,true); assert.equal(timingIssue(c.rows,8),null);
  assert.equal(original[0].start_s,null);
});
test('blank intervals stay untimed and invalid edits are identified before saving', () => {
  const rows = rowsFromLyrics('One\nTwo');
  assert.equal(timingIssue(rows,10),null);
  assert.deepEqual(timingIssue([{...rows[0],start_s:1,end_s:null}],10),{index:0,kind:'incomplete'});
  assert.deepEqual(timingIssue([{...rows[0],start_s:1,end_s:3},{...rows[1],start_s:2,end_s:4}],10),{index:1,kind:'overlap'});
  assert.throws(()=>stampNext([{...rows[0],start_s:2,end_s:null}],1,1));
});
