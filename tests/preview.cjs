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
  const mediaLayout = () => page.evaluate(() => {
    const rect = selector => {
      const box = document.querySelector(selector).getBoundingClientRect();
      return { x: box.x, y: box.y, right: box.right, bottom: box.bottom, width: box.width };
    };
    return { video: rect('#videos'), comparison: rect('#captionCompare') };
  });
  assert.equal(await page.locator('#captionCompare').evaluate(element => element.open), true);
  const wide = await mediaLayout();
  assert.ok(wide.comparison.x >= wide.video.right + 10);
  assert.ok(Math.abs(wide.comparison.y - wide.video.y) < 1);
  await page.locator('#captionCompare > summary').click();
  assert.ok((await mediaLayout()).video.width > wide.video.width);
  await page.locator('#captionCompare > summary').click();
  await page.setViewportSize({ width: 1000, height: 720 });
  const narrow = await mediaLayout();
  assert.ok(narrow.comparison.y >= narrow.video.bottom + 10);
  assert.ok(await page.locator('.review-media').evaluate(element => element.scrollWidth <= element.clientWidth + 1));
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.locator('#fs').click();
  await page.waitForFunction(() => document.fullscreenElement?.id === 'stage');
  assert.ok(await page.locator('#stage').evaluate(element => element.getBoundingClientRect().width >= innerWidth - 1));
  await page.evaluate(() => document.exitFullscreen());
  const duration = initial.duration;
  const fixture = { ...initial, instruction: 'Instruction fixture',
    subtasks: [{ i: 0, start: 0, end: duration * 0.75, desc: 'Subtask fixture' }],
    atomic: [{ start: 0, end: duration * 0.25, text: '[both hands] First action' },
      { start: duration * 0.375, end: duration * 0.5, text: '[both hands] Second action' }] };
  await page.evaluate(fixture => renderView(CUR, fixture, 0), fixture);
  await page.waitForFunction(() => document.querySelector('video')?.readyState > 0);
  const seek = async time => page.evaluate(time => {
    const video = document.querySelector('video'); video.pause(); video.currentTime = time;
    video.dispatchEvent(new Event('timeupdate'));
  }, time);
  const draft = () => page.evaluate(() => JSON.parse(JSON.stringify({ ins: EDIT.ins, subs: EDIT.subs, atoms: EDIT.atoms })));
  await seek(duration * 0.1);
  await page.locator('video').focus();
  await page.keyboard.press('Space');
  await page.waitForFunction(() => !document.querySelector('video').paused);
  await page.evaluate(() => document.querySelector('video').dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', repeat: true, bubbles: true })));
  assert.equal(await page.locator('video').evaluate(video => video.paused), false);
  await page.keyboard.press('Space');
  assert.equal(await page.locator('video').evaluate(video => video.paused), true);
  await seek(duration * 0.1);
  const originalDraft = await draft();
  await page.keyboard.press('e');
  assert.equal((await draft()).atoms[0].end, Math.round(duration * 0.1 * 10) / 10);
  assert.equal((await draft()).atoms.length, 2);
  assert.equal(await page.locator('#selIn').inputValue(), 'Subtask fixture');
  await page.keyboard.press('Control+z');
  assert.deepEqual(await draft(), originalDraft);
  await seek(duration * 0.4);
  await page.keyboard.press('p');
  const extended = await draft();
  assert.equal(extended.atoms[0].end, Math.round(duration * 0.4 * 10) / 10);
  assert.equal(extended.atoms[1].start, extended.atoms[0].end);
  assert.equal(extended.atoms[0].text, originalDraft.atoms[0].text);
  assert.equal(extended.atoms[1].text, originalDraft.atoms[1].text);
  assert.equal(await page.locator('#selIn').inputValue(), '[both hands] Second action');
  await page.keyboard.press('Control+z');
  assert.deepEqual(await draft(), originalDraft);
  await seek(duration * 0.499);
  await page.keyboard.press('p');
  assert.equal((await draft()).atoms[1].start, Math.round((duration * 0.5 - 0.1) * 10) / 10);
  await page.keyboard.press('Control+z');
  await seek(duration * 0.1);
  await page.keyboard.press('p');
  assert.deepEqual(await draft(), originalDraft);
  await seek(duration * 0.3);
  await page.keyboard.press('e');
  assert.equal((await draft()).subs[0].end, Math.round(duration * 0.3 * 10) / 10);
  assert.deepEqual((await draft()).atoms, originalDraft.atoms);
  await page.keyboard.press('Control+z');
  await seek(duration * 0.1);
  await page.locator('#selIn').focus();
  await page.keyboard.press('Space'); await page.keyboard.press('e'); await page.keyboard.press('p');
  assert.equal(await page.locator('video').evaluate(video => video.paused), true);
  assert.deepEqual((await draft()).atoms.map(x => [x.start, x.end]), originalDraft.atoms.map(x => [x.start, x.end]));
  await page.keyboard.press('Control+z');
  assert.deepEqual(await draft(), originalDraft);
  await page.locator('video').focus();
  assert.equal(await page.locator('#selIn').inputValue(), originalDraft.atoms[0].text);
  const synchronized = async (level, index, text) => {
    assert.equal(await page.locator('#selIn').inputValue(), text);
    assert.equal(await page.locator(`#levels tr[data-l="${level}"][data-k="${index}"] input.tx`).inputValue(), text);
    assert.ok((await page.locator(`#lane${level === 'atoms' ? 'Atom' : 'Sub'} [data-${level === 'atoms' ? 'atom' : 'sub'}="${index}"]`).getAttribute('title')).includes(text));
    assert.ok((await page.locator(level === 'atoms' ? '#ovlAtom' : '#ovlSub').innerText()).includes(text.replace('[both hands] ', '')));
    assert.ok(await page.evaluate(text => [...document.querySelector('video').textTracks[0].cues].some(cue => cue.text === text), text));
  };
  await seek(duration * 0.1);
  assert.equal(await page.locator('#selIn').inputValue(), '[both hands] First action');
  const atomicInput = page.locator('#levels tr[data-l="atoms"][data-k="0"] input.tx');
  await atomicInput.fill('[both hands] Edited in table');
  await synchronized('atoms', 0, '[both hands] Edited in table');
  await page.locator('#selIn').fill('[both hands] Edited in selected annotation');
  await page.locator('#selIn').pressSequentially(' live');
  await synchronized('atoms', 0, '[both hands] Edited in selected annotation live');
  await page.locator('#laneAtom [data-atom="0"]').dblclick();
  await page.locator('.tled').fill('[both hands] Edited on timeline');
  assert.equal(await page.locator('.tled').evaluate(input => input === document.activeElement), true);
  await synchronized('atoms', 0, '[both hands] Edited on timeline');
  await page.locator('.tled').press('Escape');
  await synchronized('atoms', 0, '[both hands] Edited in selected annotation live');
  await page.locator('#laneAtom [data-atom="0"]').dblclick();
  await page.locator('.tled').fill('[both hands] Timeline commit');
  await page.locator('.tled').press('Enter');
  await synchronized('atoms', 0, '[both hands] Timeline commit');
  await seek(duration * 0.25);
  assert.equal(await page.locator('#selIn').inputValue(), 'Subtask fixture');
  await page.locator('#selIn').fill('Edited fallback subtask');
  await synchronized('subs', 0, 'Edited fallback subtask');
  await seek(duration * 0.375);
  assert.equal(await page.locator('#selIn').inputValue(), '[both hands] Second action');
  await seek(duration * 0.1);
  assert.equal(await page.locator('#selIn').inputValue(), '[both hands] Timeline commit');
  await seek(duration * 0.2);
  await page.evaluate(() => document.querySelector('video').play());
  await page.waitForFunction(() => document.querySelector('#selIn')?.value === 'Edited fallback subtask');
  await page.evaluate(() => document.querySelector('video').pause());
  await seek(duration * 0.875);
  assert.equal(await page.locator('#selIn').inputValue(), 'Instruction fixture');
  await page.locator('#selIn').fill('Edited fallback instruction');
  assert.equal(await page.locator('#levels input[data-l="ins"]').inputValue(), 'Edited fallback instruction');
  assert.equal(await page.locator('#laneIns').innerText(), 'Edited fallback instruction');
  assert.equal(await page.locator('#ovlSub').innerText(), 'Edited fallback instruction');
  assert.ok(await page.evaluate(() => [...document.querySelector('video').textTracks[0].cues].some(cue => cue.text === 'Edited fallback instruction')));
  await page.locator('#annotationToggle').uncheck();
  assert.equal(await page.locator('#selbox').isHidden(), true);
  await seek(duration * 0.4);
  assert.equal(await page.locator('#ovlAtom').isVisible(), true);
  await page.locator('#annotationToggle').check();
  assert.equal(await page.locator('#selIn').inputValue(), '[both hands] Second action');
  await seek(duration * 0.23);
  const movedEnd = page.locator('#levels tr[data-l="atoms"][data-k="0"] input[data-f="end"]');
  await movedEnd.fill(String(duration * 0.2)); await movedEnd.dispatchEvent('change');
  assert.equal(await page.locator('#selIn').inputValue(), 'Edited fallback subtask');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('.tlbar label.switch').click();
  await seek(duration * 0.4);
  assert.match(await page.locator('#selTx').innerText(), /Second action/);
  await seek(duration * 0.3);
  assert.equal(await page.locator('#selTx').innerText(), 'Subtask fixture');
  await page.locator('.tlbar label.switch').click();
  await page.locator('#annotationToggle').uncheck();
  await page.evaluate(initial => { EDIT = null; renderView(CUR, initial, 0); }, initial);
  assert.equal(await page.locator('#annotationToggle').isChecked(), false);
  assert.equal(await page.locator('#selbox').isHidden(), true);
  await page.locator('#annotationToggle').check();
  await page.waitForFunction(() => document.querySelector('video')?.readyState > 0);
  await page.evaluate(() => document.querySelector('video').pause());
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
  await page.locator('.review-media').scrollIntoViewIfNeeded();
  assert.equal(await page.locator('#captionCompare').evaluate(element => getComputedStyle(element).overflowY), 'auto');
  await page.locator('.review-media').screenshot({ path: path.join(state, 'captioning-data-video-comparison.png') });
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
  const mobile = await mediaLayout();
  assert.ok(mobile.comparison.y >= mobile.video.bottom + 10);
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
  console.log('PASS: mock login/roles; real video; Space playback, trim/extend shortcuts, undo and typing isolation; playhead atomic/subtask/instruction priority; live table/panel/timeline/native-caption sync; toggle persistence; isolated corrections; comparison beside video, responsive stacking, collapse and fullscreen; before/after drafts, timing, text safety, saved/reverted history, video jumps, mobile layout; admin navigation; logout.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  try { if (restoreCaptions) await restoreCaptions(); }
  catch (error) { console.error(error); process.exitCode = 1; }
  finally { if (browser) await browser.close(); }
});
