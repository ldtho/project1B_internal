'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../viewer/index.html'), 'utf8');
const start = html.indexOf('const TAG = '), end = html.indexOf('const hands = ', start);
const { swapHands, deleteHandPhrase } = vm.runInNewContext(html.slice(start, end) + '\n({ swapHands, deleteHandPhrase })');
const caption = '[left hand] hold cup | [right hand] lift lid | [both hands] steady tray';
assert.equal(swapHands(caption), '[right hand] hold cup | [left hand] lift lid | [both hands] steady tray');
assert.equal(swapHands(swapHands(caption)), caption);
assert.equal(swapHands('The left hand holds a cup.'), 'The left hand holds a cup.');
assert.equal(swapHands('[both hands] lift cup'), '[both hands] lift cup');
for (const [cursor, first, second] of [
  [3, '[left hand] | [right hand] lift lid | [both hands] steady tray', '[right hand] lift lid | [both hands] steady tray'],
  [caption.indexOf('lid'), '[left hand] hold cup | [right hand] | [both hands] steady tray', '[left hand] hold cup | [both hands] steady tray'],
  [caption.length, '[left hand] hold cup | [right hand] lift lid | [both hands] ', '[left hand] hold cup | [right hand] lift lid']
]) {
  const cleared = deleteHandPhrase(caption, cursor);
  assert.equal(cleared.text, first);
  const removed = deleteHandPhrase(cleared.text, cleared.cursor);
  assert.equal(removed.text, second);
  assert.ok(removed.cursor >= 0 && removed.cursor <= removed.text.length);
}
const single = deleteHandPhrase('[left hand] hold cup', 15);
assert.equal(single.text, '[left hand] ');
assert.equal(deleteHandPhrase(single.text, single.cursor).text, '');
assert.equal(deleteHandPhrase('No hand tags here', 5), null);
assert.equal(deleteHandPhrase('Prefix [left hand] hold cup', 2), null);
assert.equal(deleteHandPhrase('[left hand] cup [right hand] lid', 12).text, '[left hand] | [right hand] lid');
console.log('PASS: hand swap and two-step phrase/tag deletion; first/middle/last/single clauses; neighboring captions preserved.');
