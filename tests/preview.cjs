'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const base = process.env.PREVIEW_URL || 'https://127.0.0.1:9445';
const state = process.env.PREVIEW_STATE || path.resolve(__dirname, '../.preview');
const db = path.join(state, 'mock.sqlite3');
const python = process.env.DATASETS_PYTHON || 'python3';
let browser;
let restoreCaptions;
const captionState = episode => ({ instruction: episode.instruction,
  subtasks: episode.subtasks.map(cue => ({ start: cue.start, end: cue.end, text: cue.text ?? cue.desc })),
  atomic: episode.atomic.map(cue => ({ start: cue.start, end: cue.end, text: cue.text })) });
const grantAdmin = enabled => execFileSync(python, ['-c',
  "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"INSERT OR IGNORE INTO user_roles VALUES ('mock-worker','admin')\" if sys.argv[2]=='1' else \"DELETE FROM user_roles WHERE user_id='mock-worker' AND role='admin'\"); c.commit(); c.close()",
  db, enabled ? '1' : '0']);

(async () => {
  assert.ok(fs.existsSync(db));
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const anonymous = await context.request.get(`${base}/captioning_data/`, { maxRedirects: 0 });
  assert.equal(anonymous.status(), 302);
  assert.equal(anonymous.headers().location, '/?next=/captioning_data/');
  assert.equal((await context.request.get(`${base}/captioning_data/api/episodes`)).status(), 401);
  assert.equal((await context.request.post(`${base}/api/auth/web/login`, { data: { email: 'worker', password: 'wrong' } })).status(), 401);
  const page = await context.newPage();
  const errors = []; page.on('pageerror', error => { errors.push(error.message); console.error(error.stack); });
  await page.goto(`${base}/captioning_data/`);
  await page.locator('#login-button:not([disabled])').waitFor();
  await page.locator('#identifier').fill('worker'); await page.locator('#password').fill('preview-password'); await page.locator('#login-button').click();
  await page.waitForURL(`${base}/captioning_data/**`);
  await page.locator('#edSave').waitFor({ timeout: 60000 });
  assert.equal(await page.title(), 'Project1B · captioning_data');
  assert.equal((await context.request.get(`${base}/api/admin/videos`)).status(), 403);
  const user = await (await context.request.get(`${base}/api/auth/me`)).json();
  assert.equal(user.user_id, 'mock-worker'); assert.deepEqual(user.roles, ['worker']);
  try {
    grantAdmin(true);
    assert.ok((await (await context.request.get(`${base}/api/auth/me`)).json()).roles.includes('admin'));
    assert.equal((await context.request.get(`${base}/api/admin/videos`)).status(), 200);
  } finally { grantAdmin(false); }
  assert.equal((await context.request.get(`${base}/api/admin/videos`)).status(), 403);
  const id = await page.evaluate(() => CUR.id);
  const version = await page.evaluate(() => EDIT.version);
  const initial = await (await context.request.get(`${base}/captioning_data/api/episode?p=${encodeURIComponent(id)}`)).json();
  let lastTestVersion = initial.version;
  restoreCaptions = async () => {
    await context.request.post(`${base}/api/auth/web/login`, { data: { email: 'worker', password: 'preview-password' } });
    const current = await (await context.request.get(`${base}/captioning_data/api/episode?p=${encodeURIComponent(id)}`)).json();
    if (JSON.stringify(captionState(current)) === JSON.stringify(captionState(initial))) return;
    if (current.version !== lastTestVersion) { console.log('Concurrent preview correction preserved.'); return; }
    const cookie = (await context.cookies()).find(cookie => cookie.name === '__Host-rbt_csrf');
    const restored = await context.request.post(`${base}/captioning_data/api/${initial.edited ? 'edit' : 'revert'}`, {
      data: { id, version: current.version, ...captionState(initial) }, headers: { Origin: base, 'X-CSRF-Token': cookie.value }
    });
    assert.equal(restored.status(), 200, 'Restore initial preview captions');
  };
  const media = await context.request.get(`${base}/captioning_data/video?p=${encodeURIComponent(id)}`, { headers: { Range: 'bytes=0-31' } });
  assert.equal(media.status(), 206); assert.equal((await media.body()).length, 32);
  await page.waitForFunction(() => document.querySelector('video')?.readyState > 0, null, { timeout: 30000 });
  if (!(await page.locator('#captionCompare').evaluate(element => element.open))) await page.locator('#compareBtn').click();
  const pairs = await page.evaluate(() => captionPairs(
    [{ start: 0, end: 1, text: 'A' }, { start: 1, end: 2, text: 'B' }],
    [{ start: 0, end: 1, text: 'A' }, { start: 1, end: 1.2, text: 'Added' }, { start: 1.2, end: 2, text: 'B' }]
  ).map(([before, after]) => [before?.text || null, after?.text || null]));
  assert.deepEqual(pairs, [['A', 'A'], [null, 'Added'], ['B', 'B']]);
  const end = page.locator('#levels tr[data-l="atoms"][data-k="0"] input[data-f="end"]');
  await end.fill(await page.evaluate(() => fmt(EDIT.atoms[0].end - 0.1))); await end.dispatchEvent('change');
  assert.match(await page.locator('#cmpContent').innerText(), /Timing changed/);
  page.once('dialog', dialog => dialog.accept()); await page.locator('#edDiscard').click();
  await page.locator('input[data-l="ins"]').fill('<img src=x onerror="window.captionCompareUnsafe=true">');
  assert.equal(await page.locator('#cmpContent img').count(), 0);
  assert.equal(await page.evaluate(() => !!window.captionCompareUnsafe), false);
  await page.locator('input[data-l="ins"]').fill('Local preview correction');
  assert.equal(await page.locator('#cmpAfter option[value="current"]').textContent(), 'Unsaved draft');
  await page.locator('#cmpBefore').selectOption('previous');
  assert.match(await page.locator('#cmpMeta').innerText(), /before selected change/i);
  assert.ok(await page.locator('#cmpContent ins').count()); assert.ok(await page.locator('#cmpContent del').count());
  await page.locator('#edSave').click();
  await page.waitForFunction(before => EDIT?.version === before + 1, version);
  lastTestVersion = version + 1;
  const records = fs.readFileSync(path.join(state, 'edits.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(records.at(-1).editor_id, 'mock-worker'); assert.equal(records.at(-1).instruction, 'Local preview correction');
  assert.deepEqual(records.at(-1).editor_roles, ['worker']);
  assert.equal(records.at(-1).version, version + 1); assert.match(records.at(-1).source_sha256, /^[a-f0-9]{64}$/);
  assert.deepEqual(records.at(-1).before, captionState(initial));
  assert.deepEqual(records.at(-1).original, initial.original);
  const cookies = await context.cookies();
  const csrf = cookies.find(cookie => cookie.name === '__Host-rbt_csrf').value;
  const reverted = await context.request.post(`${base}/captioning_data/api/revert`, {
    data: { id, version: version + 1 }, headers: { Origin: base, 'X-CSRF-Token': csrf }
  });
  assert.equal(reverted.status(), 200);
  lastTestVersion = version + 2;
  await page.reload(); await page.locator('#edSave').waitFor();
  const savedHistory = await (await context.request.get(`${base}/captioning_data/api/episode?p=${encodeURIComponent(id)}`)).json();
  assert.equal(savedHistory.history.at(-2).captions.instruction, 'Local preview correction');
  assert.equal(savedHistory.history.at(-1).revert, true);
  assert.equal(savedHistory.instruction, savedHistory.original.instruction);
  await page.locator('#cmpAfter').selectOption(String(version + 1));
  assert.match(await page.locator('#cmpContent').innerText(), /Local preview correction/);
  assert.match(await page.locator('#cmpMeta').innerText(), /Mock worker/);
  await page.locator('#cmpBefore').selectOption(String(version + 1));
  assert.equal(await page.locator('#cmpCount').textContent(), 'No changes');
  await page.locator('#cmpBefore').selectOption('0');
  await page.locator('#captionCompare').screenshot({ path: path.join(state, 'captioning-data-comparison.png') });
  await page.locator('#cmpUnchanged').check();
  const jump = page.locator('#cmpContent button[data-t]').first();
  const target = Number(await jump.getAttribute('data-t')); await jump.click();
  assert.ok(Math.abs(await page.evaluate(() => document.querySelector('video').currentTime) - target) < 1);
  await page.evaluate(() => document.querySelector('video').pause());
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#compareBtn').click(); await page.locator('#compareBtn').click();
  await page.waitForFunction(() => document.body.classList.contains('noside'));
  assert.equal(await page.locator('#cmpContent td').first().evaluate(element => getComputedStyle(element).display), 'block');
  assert.ok(await page.locator('#captionCompare').evaluate(element => element.scrollWidth <= element.clientWidth + 1));
  await page.locator('#cmpUnchanged').uncheck();
  await page.locator('#captionCompare').screenshot({ path: path.join(state, 'captioning-data-comparison-mobile.png') });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.screenshot({ path: path.join(state, 'captioning-data-preview.png'), fullPage: true });
  await page.locator('#logout').click(); await page.locator('#login-view').waitFor();
  assert.equal((await context.request.get(`${base}/captioning_data/video?p=${encodeURIComponent(id)}`)).status(), 401);
  await page.locator('#identifier').fill('admin'); await page.locator('#password').fill('preview-password'); await page.locator('#login-button').click();
  await page.locator('#library-view').waitFor(); await page.locator('.recording-row').first().waitFor();
  await page.getByRole('link', { name: 'Open captioning_data' }).click(); await page.locator('#edSave').waitFor();
  await page.getByRole('button', { name: 'sign out', exact: true }).click(); await page.locator('#login-view').waitFor();
  for (const name of ['caption_data_admin', 'caption_data_reviewer']) {
    const response = await context.request.post(`${base}/api/auth/web/login`, { data: { email: name, password: 'preview-password' } });
    assert.equal(response.status(), 200); assert.deepEqual((await response.json()).roles, [name, 'worker']);
    const responseData = await context.request.get(`${base}/captioning_data/api/episodes`);
    assert.equal(responseData.status(), 200);
    assert.equal((await responseData.json()).user.role, name === 'caption_data_admin' ? 'admin' : 'reviewer');
    assert.equal((await context.request.get(`${base}/api/admin/videos`)).status(), 403);
  }
  assert.deepEqual(errors, []);
  console.log('PASS: mock login/roles; real video; isolated corrections; before/after drafts, timing, text safety, saved/reverted history, video jumps, mobile layout; admin navigation; logout.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  try { if (restoreCaptions) await restoreCaptions(); }
  catch (error) { console.error(error); process.exitCode = 1; }
  finally { if (browser) await browser.close(); }
});
