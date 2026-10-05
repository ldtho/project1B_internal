'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../viewer/index.html'), 'utf8');
const begin = html.indexOf('  const done = (j, next = []) => {');
const end = html.indexOf('  // one edit op on line', begin);
assert.ok(begin >= 0 && end > begin);
const handlers = html.slice(begin, end);

async function check({ qa = 'corrected', last = false, todo = false, skip = false, failed = false, navigated = false, reset = false, offList = false } = {}) {
  const e = { id: 'a' }, b = { id: 'b' }, c = { id: 'c' };
  const queue = last ? [e] : [e, b, c], events = [], button = { disabled: false };
  const draft = { version: 1, ins: 'draft', subs: [], atoms: [] };
  let pending, posts = 0;
  const ctx = { e, AUTH: true, CUR: e, EDIT: draft, FILTERED: offList ? [b, c] : [...queue], DETAIL: new Map(), SEL: null,
    v: { currentTime: 0 }, $: () => button, canSave: () => true,
    msg: text => events.push(['message', text]), renderStats() {}, renderQAStats() {},
    applyFilter: () => { ctx.FILTERED = queue.filter(episode => !(todo && episode.qa) && !(skip && episode === b)); },
    renderView: episode => events.push(['stay', episode.id]),
    open: id => { events.push(['next', id]); ctx.CUR = queue.find(episode => episode.id === id); },
    post: () => { posts++; return new Promise((resolve, reject) => { pending = { resolve, reject }; }); }
  };
  const response = { instruction: 'saved', version: 2, subtasks: [], atomic: [], qa };
  if (reset) {
    vm.runInNewContext(handlers + '\ndone(response);', Object.assign(ctx, { response }));
  } else {
    vm.runInNewContext(handlers + '\nsave(); save();', ctx);
    assert.equal(posts, 1, 'Duplicate saves blocked while request is pending');
    assert.equal(ctx.CUR, e, 'No navigation before save succeeds');
    if (navigated) { ctx.CUR = b; ctx.EDIT = { ins: 'another draft', dirty: true }; }
    if (failed) pending.reject(new Error('save failed'));
    else pending.resolve(response);
    await new Promise(resolve => setImmediate(resolve));
  }
  const navigation = events.filter(([event]) => event === 'next' || event === 'stay');
  if (failed) {
    assert.equal(ctx.EDIT, draft); assert.equal(ctx.CUR, e); assert.equal(button.disabled, false);
    assert.deepEqual(navigation, []);
    assert.ok(events.some(([event, text]) => event === 'message' && text === 'save failed'));
  } else {
    assert.equal(e.qa, qa); assert.equal((await ctx.DETAIL.get(e.id)).version, 2);
    if (navigated) {
      assert.equal(ctx.CUR, b); assert.equal(ctx.EDIT.ins, 'another draft'); assert.deepEqual(navigation, []);
    } else {
      assert.equal(ctx.EDIT, null);
      assert.deepEqual(navigation, [[last || reset || offList ? 'stay' : 'next', last || reset || offList ? 'a' : skip ? 'c' : 'b']]);
      if (todo) assert.ok(!ctx.FILTERED.includes(e));
    }
  }
}

(async () => {
  for (const scenario of [{}, { qa: 'confirmed' }, { todo: true }, { todo: true, skip: true },
    { last: true }, { failed: true }, { navigated: true }, { reset: true }, { offList: true }]) await check(scenario);
  console.log('PASS: corrected/confirmed save advance; pending-filter order; changed filters; last/off-list stay; failed saves keep drafts; duplicate saves blocked; late responses preserve new drafts; reset stays.');
})().catch(error => { console.error(error); process.exitCode = 1; });
