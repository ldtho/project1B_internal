'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const https = require('node:https');
const { spawn, execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const repo = path.resolve(__dirname, '..');
const scratch = os.tmpdir();
fs.mkdirSync(scratch, { recursive: true });
const tmp = fs.mkdtempSync(path.join(scratch, 'smoke-'));
let expired = false, maliciousTitle = false, csrfChecked = false, refuseBundle = false, authDown = false;
let browser, nginx, datasetsServer, reloadTimer, nginxLog = '', datasetsLog = '';
const video = { pk: 42, video_id: 'phone/sample-session', status: 'uploaded', worker: 'Example Researcher', task: 'Organize tools', duration_s: 84, uploaded_at: '2026-10-02T02:00:00Z' };
const other = { ...video, pk: 77, task: 'Inspect equipment', status: 'rejected' };
const zip = execFileSync('python3', ['-c', "import io,zipfile,sys; b=io.BytesIO(); z=zipfile.ZipFile(b,'w'); z.writestr('video.mp4',b'video fixture'); z.writestr('capture.mcap',b'mcap fixture'); z.writestr('metadata.json',b'{}'); z.close(); sys.stdout.buffer.write(b.getvalue())"]);
const api = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost');
  const session = /__Host-rbt_session=([^;]+)/.exec(request.headers.cookie || '')?.[1];
  const send = (code, body, headers = {}) => { response.writeHead(code, { 'Content-Type': 'application/json', ...headers }); response.end(body === null ? '' : JSON.stringify(body)); };
  if (url.pathname === '/api/auth/web/login') {
    let raw = ''; for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    if (body.password !== 'sample-password') return send(401, { detail: 'Invalid credentials' });
    expired = false;
    const role = body.email === 'worker' ? 'worker' : 'admin';
    return send(200, { full_name: 'Sample Staff', roles: [role] }, { 'Set-Cookie': [`__Host-rbt_session=${role}; Path=/; Secure; HttpOnly; SameSite=Strict`, '__Host-rbt_csrf=sample-csrf; Path=/; Secure; SameSite=Strict'] });
  }
  if (!session || expired) return send(401, { detail: 'Not signed in' });
  if (url.pathname === '/api/auth/me') return send(authDown ? 503 : 200, { user_id: `user-${session}`, full_name: 'Sample Staff', roles: [session] });
  if (url.pathname === '/api/auth/logout') {
    csrfChecked = request.headers['x-csrf-token'] === 'sample-csrf';
    if (!csrfChecked) return send(403, { detail: 'CSRF token required' });
    return send(204, null, { 'Set-Cookie': ['__Host-rbt_session=; Max-Age=0; Path=/; Secure; HttpOnly', '__Host-rbt_csrf=; Max-Age=0; Path=/; Secure'] });
  }
  if (session !== 'admin') return send(403, { detail: 'Admin role required' });
  if (url.pathname === '/api/admin/videos') return send(200, { videos: [video, other].filter(item => !url.searchParams.get('status') || item.status === url.searchParams.get('status')) });
  if (url.pathname.endsWith('/original-bundle')) {
    if (refuseBundle) return send(409, { detail: 'Original upload files are missing or invalid' });
    response.writeHead(200, { 'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="sample-session-original.zip"' });
    return response.end(zip);
  }
  if (/^\/api\/admin\/videos\/\d+$/.test(url.pathname)) {
    const item = url.pathname.endsWith('/77') ? other : video;
    return send(200, { ...item, session_uid: 'sample-session', resolution: '1920x1080', task: { name: maliciousTitle ? '<img src=x onerror=alert(1)>' : item.task }, worker: { name: item.worker }, files: item.pk === 42 ? [{ name: 'video.mp4', size: 84000000 }, { name: 'capture.mcap', size: 210000000 }, { name: 'metadata.json', size: 2400 }] : [{ name: 'video.mp4', size: 2000 }], original_bundle_available: item.pk === 42, media_roles: [{ role: 'primary', label: 'Primary video' }], sensors: item.pk === 42 ? { message_count: 98700, topics: [{ topic: '/depth' }, { topic: '/camera' }] } : null, device: { model: 'Sample capture device' }, capture_profile: 'Standard' });
  }
  send(404, { detail: 'Fixture route not found' });
});
function freePort() { return new Promise(resolve => { const server = http.createServer(); server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(() => resolve(port)); }); }); }
function request(url, method = 'GET') { return new Promise((resolve, reject) => { const call = https.request(url, { method, rejectUnauthorized: false }, response => { response.resume(); response.on('end', () => resolve(response.statusCode)); }); call.on('error', reject); call.end(); }); }

(async () => {
  await new Promise(resolve => api.listen(0, '127.0.0.1', resolve));
  const apiPort = api.address().port, httpPort = await freePort(), tlsPort = await freePort(), datasetsPort = await freePort();
  const base = `https://127.0.0.1:${tlsPort}`;
  const episode = { episode_uid: 'fixture/e1', dataset: 'egoverse', split: 'train', task: 'Wash dishes', instruction: 'Wash dishes', duration_s: 2, video: `${tmp}/video.mp4`, subtask: '[0.0 - 2.0] Wash a plate', caption: '[0.0 - 2.0] [both hands] wash plate' };
  fs.writeFileSync(`${tmp}/manifest.jsonl`, JSON.stringify(episode) + '\n');
  fs.writeFileSync(`${tmp}/video.mp4`, '0123456789');
  const python = process.env.DATASETS_PYTHON || 'python3';
  const installer = fs.readFileSync(path.join(repo, 'deploy/install.sh'), 'utf8');
  const packager = installer.split("<<'PYTHON'\n")[1].split('\nPYTHON')[0];
  const sharedRepo = process.env.DATASETS_REPO || path.dirname(repo);
  execFileSync(python, ['-', sharedRepo, `${tmp}/package/service`], { input: packager, cwd: sharedRepo, stdio: ['pipe', 'pipe', 'pipe'] });
  fs.mkdirSync(`${tmp}/package/web`, { recursive: true });
  fs.copyFileSync(path.join(repo, 'datasets_server.py'), `${tmp}/package/datasets_server.py`);
  fs.cpSync(path.join(repo, 'viewer'), `${tmp}/package/viewer`, { recursive: true });
  fs.copyFileSync(path.join(repo, 'web/datasets-auth.js'), `${tmp}/package/web/datasets-auth.js`);
  const datasetArgs = [`${tmp}/package/datasets_server.py`, '--data-root', tmp, '--source', '=manifest.jsonl', '--edits', `${tmp}/edits.jsonl`, '--port', String(datasetsPort), '--auth-port', String(apiPort), '--origin', base];
  const datasetEnv = { ...process.env, DATASETS_REPO: `${tmp}/package/service`, PYTHONDONTWRITEBYTECODE: '1' };
  async function startDatasets() {
    datasetsServer = spawn(python, datasetArgs, { env: datasetEnv, stdio: ['ignore', 'ignore', 'pipe'] });
    datasetsServer.stderr.on('data', data => { datasetsLog += data; });
    for (let count = 0; ; count++) {
      try { const response = await fetch(`http://127.0.0.1:${datasetsPort}/healthz`); assert.equal(response.status, 200); break; }
      catch (error) { if (count > 100 || datasetsServer.exitCode !== null) throw new Error(`Dataset service failed: ${datasetsLog}`); await new Promise(resolve => setTimeout(resolve, 50)); }
    }
  }
  async function stopDatasets() {
    if (datasetsServer?.exitCode === null) { const exit = new Promise(resolve => datasetsServer.once('exit', resolve)); datasetsServer.kill('SIGTERM'); await exit; }
  }
  await startDatasets();
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost', '-keyout', `${tmp}/key.pem`, '-out', `${tmp}/cert.pem`], { stdio: 'ignore' });
  let site = fs.readFileSync(path.join(repo, 'deploy/nginx.conf'), 'utf8')
    .replace('listen 80;', `listen 127.0.0.1:${httpPort};`).replace('listen [::]:80;', '')
    .replace('listen 443 ssl http2;', `listen 127.0.0.1:${tlsPort} ssl;`).replace('listen [::]:443 ssl http2;', '')
    .replace('/etc/letsencrypt/live/internal.project1b.space/fullchain.pem', `${tmp}/cert.pem`)
    .replace('/etc/letsencrypt/live/internal.project1b.space/privkey.pem', `${tmp}/key.pem`)
    .replace('include /etc/letsencrypt/options-ssl-nginx.conf;', 'ssl_protocols TLSv1.2 TLSv1.3;')
    .replace('ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;', '')
    .replace('/var/www/project1b-internal/current', path.join(repo, 'web'))
    .replaceAll('127.0.0.1:8903', `127.0.0.1:${apiPort}`)
    .replaceAll('127.0.0.1:8326', `127.0.0.1:${datasetsPort}`);
  const config = `${tmp}/nginx.conf`;
  fs.writeFileSync(config, `daemon off; pid ${tmp}/nginx.pid; error_log stderr; events {} http { include /etc/nginx/mime.types; access_log off; client_body_temp_path ${tmp}/body; proxy_temp_path ${tmp}/proxy; ${site} }`);
  execFileSync('/usr/sbin/nginx', ['-t', '-p', `${tmp}/`, '-c', config], { stdio: 'pipe' });
  nginx = spawn('/usr/sbin/nginx', ['-p', `${tmp}/`, '-c', config], { stdio: ['ignore', 'ignore', 'pipe'] });
  nginx.stderr.on('data', data => { nginxLog += data; });
  for (let count = 0; ; count++) { try { await request(base); break; } catch (error) { if (count > 50) throw error; await new Promise(resolve => setTimeout(resolve, 50)); } }
  assert.equal(await request(`${base}/api/admin/videos/42/original-bundle`), 401);
  assert.equal(await request(`${base}/api/admin/videos/42`, 'POST'), 403);
  assert.equal(await request(`${base}/api/admin/videos/42/review`, 'POST'), 404);
  assert.equal(await request(`${base}/api/auth/signup`, 'POST'), 404);
  assert.equal(await request(`${base}/captioning_data`), 308);
  assert.equal(await request(`${base}/datasets/`), 404);
  assert.equal(await request(`${base}/captioning_data/`), 302);
  for (const route of ['api/episodes', 'api/episode?p=fixture/e1', 'api/search?q=wash', 'video?p=fixture/e1', 'vtt?p=fixture/e1', 'viewer.js', 'auth.js']) {
    assert.equal(await request(`${base}/captioning_data/${route}`), 401);
  }
  assert.equal(await request(`${base}/captioning_data/api/login`, 'POST'), 404);
  assert.equal(await request(`${base}/captioning_data/healthz`), 404);
  assert.equal(await request(`${base}/captioning_data/api/edit`, 'POST'), 401);
  assert.equal(await request(`${base}/datasets_server.py`), 404);
  assert.equal(await request(`${base}/service/data_prep/viz/datasets/index.html`), 404);
  // Reproduce the first deploy: TLS still serves the old hostname until reload finishes.
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=internal.project1b.space', '-addext', 'subjectAltName=DNS:internal.project1b.space', '-keyout', `${tmp}/new-key.pem`, '-out', `${tmp}/new-cert.pem`], { stdio: 'ignore' });
  fs.writeFileSync(`${tmp}/trusted.pem`, fs.readFileSync(`${tmp}/cert.pem`) + fs.readFileSync(`${tmp}/new-cert.pem`));
  const health = installer.slice(installer.indexOf('# Wait for the new workers:'), installer.indexOf('trap - ERR\necho'))
    .replace('$domain:443:127.0.0.1', `$domain:${tlsPort}:127.0.0.1`)
    .replace('https://$domain/', `https://$domain:${tlsPort}/`);
  const checking = spawn('bash', ['-e'], { env: { ...process.env, domain: 'internal.project1b.space', repo, backup: tmp, SMOKE_CA: `${tmp}/trusted.pem` }, stdio: ['pipe', 'ignore', 'pipe'] });
  let checkError = ''; checking.stderr.on('data', data => { checkError += data; });
  const checked = new Promise((resolve, reject) => checking.on('exit', code => code === 0 ? resolve() : reject(new Error(`Reload readiness failed: ${checkError}`))));
  checking.stdin.end('curl() { command curl --noproxy "*" --cacert "$SMOKE_CA" "$@"; }\n' + health);
  reloadTimer = setTimeout(() => {
    fs.copyFileSync(`${tmp}/new-cert.pem`, `${tmp}/cert.pem`);
    fs.copyFileSync(`${tmp}/new-key.pem`, `${tmp}/key.pem`);
    nginx.kill('SIGHUP');
  }, 250);
  await checked;
  console.log('PASS: installer waits through old TLS certificate and asynchronous Nginx reload, then verifies exact page.');
  if (process.argv.includes('--readiness-only')) return;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, acceptDownloads: true, viewport: { width: 1365, height: 1000 } });
  const page = await context.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const anonymous = await browser.newContext({ ignoreHTTPSErrors: true });
  const deepLink = await anonymous.newPage();
  const episodeHash = encodeURIComponent('EgoVerse:train:fixture/e1');
  await deepLink.goto(`${base}/captioning_data/#${episodeHash}`);
  assert.equal(new URL(deepLink.url()).pathname, '/');
  assert.equal(new URL(deepLink.url()).searchParams.get('next'), '/captioning_data/');
  assert.equal(new URL(deepLink.url()).hash, `#${episodeHash}`);
  await anonymous.close();
  await page.goto(base);
  await page.locator('#login-button:not([disabled])').waitFor();
  await page.screenshot({ path: `${scratch}/internal-login.png`, fullPage: true });
  await page.locator('#identifier').fill('staff'); await page.locator('#password').fill('wrong-password'); await page.locator('#login-button').click();
  await page.getByText('Invalid credentials', { exact: true }).waitFor();
  await page.locator('#password').fill('sample-password'); await page.locator('#login-button').click();
  await page.locator('.recording-row').first().waitFor();
  assert.equal(await page.locator('.recording-row').count(), 2);
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  await page.locator('#search').fill('inspect'); assert.equal(await page.locator('.recording-row').count(), 1);
  await page.locator('#search').fill(''); await page.locator('.recording-row').first().click();
  await page.getByText('MCAP present', { exact: false }).waitFor();
  await page.screenshot({ path: `${scratch}/internal-desktop.png`, fullPage: true });
  const downloading = page.waitForEvent('download'); await page.getByRole('link', { name: 'Download original ZIP' }).click();
  const downloaded = await downloading; assert.equal(await downloaded.failure(), null);
  await downloaded.saveAs(`${tmp}/download.zip`);
  const names = JSON.parse(execFileSync('python3', ['-c', 'import zipfile,json,sys; z=zipfile.ZipFile(sys.argv[1]); assert z.testzip() is None; print(json.dumps(sorted(z.namelist())))', `${tmp}/download.zip`], { encoding: 'utf8' }));
  assert.deepEqual(names, ['capture.mcap', 'metadata.json', 'video.mp4']);
  refuseBundle = true;
  const errorTab = page.waitForEvent('popup');
  await page.getByRole('link', { name: 'Download original ZIP' }).click();
  const popup = await errorTab; await popup.waitForLoadState('domcontentloaded');
  assert.match(await popup.locator('body').innerText(), /Original upload files are missing or invalid/);
  await popup.close(); refuseBundle = false;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${scratch}/internal-mobile.png`, fullPage: true });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.locator('.recording-row').nth(1).click();
  await page.getByText('No MCAP file in this session.').waitFor();
  assert.equal(await page.getByRole('link', { name: 'Download original ZIP' }).getAttribute('aria-disabled'), 'true');
  maliciousTitle = true; await page.locator('.recording-row').first().click();
  await page.getByRole('heading', { name: '<img src=x onerror=alert(1)>' }).waitFor(); assert.equal(await page.locator('img').count(), 0);
  await page.locator('#logout').click(); await page.locator('#login-view').waitFor(); assert.ok(csrfChecked);
  await page.locator('#identifier').fill('worker'); await page.locator('#password').fill('sample-password'); await page.locator('#login-button').click();
  await page.locator('#datasets-view').waitFor();
  assert.ok(await page.locator('#library-view').isHidden());
  assert.equal((await context.request.get(`${base}/api/admin/videos`)).status(), 403);
  await page.getByRole('link', { name: 'Open captioning_data' }).click();
  await page.locator('#edSave').waitFor();
  assert.equal(await page.title(), 'Project1B · captioning_data');
  await page.getByRole('heading', { name: 'captioning_data', exact: true }).waitFor();
  assert.equal(await page.locator('#whoSel').inputValue(), '');
  assert.equal(await page.locator('#login').isHidden(), true);
  assert.equal(await page.evaluate(() => localStorage.getItem('qa')), null);
  const id = 'EgoVerse:train:fixture/e1', query = `p=${encodeURIComponent(id)}`;
  const writeHeaders = { Origin: base, 'X-CSRF-Token': 'sample-csrf' };
  const edit = { id, version: 0, instruction: 'Rinse a plate', subtasks: [{ start: 0, end: 2, text: 'Rinse plate' }], atomic: [{ start: 0, end: 2, text: '[both hands] rinse plate' }] };
  const postEdit = (body, headers = writeHeaders) => context.request.post(`${base}/captioning_data/api/edit`, { data: body, headers });
  assert.equal((await postEdit(edit, { Origin: base })).status(), 403);
  assert.equal((await postEdit(edit, { ...writeHeaders, 'X-CSRF-Token': 'wrong' })).status(), 403);
  assert.equal((await postEdit(edit, { ...writeHeaders, Origin: 'https://other.example' })).status(), 403);
  assert.equal((await postEdit(edit, { 'X-CSRF-Token': 'sample-csrf' })).status(), 403);
  assert.equal((await postEdit({ ...edit, atomic: [{ start: 0, end: 3, text: '[both hands] rinse plate' }] })).status(), 400);
  assert.equal((await postEdit([])).status(), 400);
  assert.equal((await postEdit({ ...edit, version: false })).status(), 400);
  assert.equal((await postEdit({ ...edit, review_flag: 'true' })).status(), 400);
  assert.equal((await postEdit({ ...edit, review_flag: true, review_reason: '   ' })).status(), 400);
  assert.equal((await postEdit({ ...edit, crop: { start: 1, end: 0.5 } })).status(), 400);
  assert.equal(fs.existsSync(`${tmp}/edits.jsonl`), false);
  const media = await context.request.get(`${base}/captioning_data/video?${query}`, { headers: { Range: 'bytes=2-5' } });
  assert.equal(media.status(), 206); assert.equal(await media.text(), '2345');
  assert.equal(media.headers()['cache-control'], 'no-store');
  assert.equal((await context.request.get(`${base}/captioning_data/video?p=../../etc/passwd`)).status(), 404);
  assert.equal((await context.request.get(`${base}/captioning_data/vtt?${query}`)).status(), 200);
  await page.locator('#edSave').click();
  await page.waitForFunction(() => EDIT?.version === 1);
  assert.equal(JSON.parse(fs.readFileSync(`${tmp}/edits.jsonl`, 'utf8').trim()).editor_id, 'user-worker');
  const otherPage = await context.newPage(); await otherPage.goto(`${base}/captioning_data/#${encodeURIComponent(id)}`); await otherPage.locator('#edSave').waitFor();
  await page.locator('input[data-l="ins"]').fill('Rinse a plate'); await page.locator('#edSave').click();
  await page.waitForFunction(() => EDIT?.version === 2);
  await otherPage.locator('input[data-l="ins"]').fill('Concurrent draft'); await otherPage.locator('#edSave').click();
  await otherPage.getByText('Someone saved this episode after you opened it; reload it first', { exact: true }).waitFor();
  assert.equal(await otherPage.locator('input[data-l="ins"]').inputValue(), 'Concurrent draft');
  otherPage.on('dialog', dialog => dialog.accept()); await otherPage.close();
  await page.locator('input[data-l="ins"]').fill('Unsaved draft'); expired = true; await page.locator('#edSave').click();
  await page.getByRole('link', { name: 'Sign in in another tab' }).waitFor();
  const loginTabPromise = page.waitForEvent('popup'); await page.getByRole('link', { name: 'Sign in in another tab' }).click();
  const loginTab = await loginTabPromise; await loginTab.locator('#login-button:not([disabled])').waitFor();
  await loginTab.locator('#identifier').fill('worker'); await loginTab.locator('#password').fill('sample-password'); await loginTab.locator('#login-button').click();
  await loginTab.waitForURL(`${base}/captioning_data/**`); await loginTab.locator('#edSave').waitFor();
  await page.getByRole('button', { name: 'Resume session' }).click(); await page.locator('#login').waitFor({ state: 'hidden' });
  assert.equal(await page.locator('input[data-l="ins"]').inputValue(), 'Unsaved draft');
  await page.locator('#edSave').click(); await page.waitForFunction(() => EDIT?.version === 3); await loginTab.close();
  authDown = true; assert.equal((await context.request.get(`${base}/captioning_data/video?${query}`)).status(), 503); authDown = false;
  const races = await Promise.all([postEdit({ ...edit, version: 3, editor: 'forged' }), postEdit({ ...edit, version: 3, editor: 'forged' })]);
  assert.deepEqual(races.map(response => response.status()).sort(), [200, 409]);
  await stopDatasets(); await startDatasets();
  const restored = await (await context.request.get(`${base}/captioning_data/api/episode?${query}`)).json();
  assert.equal(restored.version, 4); assert.equal(restored.instruction, 'Rinse a plate');
  const reverted = await context.request.post(`${base}/captioning_data/api/revert`, { data: { id, version: 4 }, headers: writeHeaders });
  assert.equal(reverted.status(), 200); assert.equal((await reverted.json()).instruction, episode.instruction);
  const records = fs.readFileSync(`${tmp}/edits.jsonl`, 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(records.length, 5); assert.ok(records.every(row => row.editor_id === 'user-worker' && row.editor === 'Sample Staff')); assert.equal(records.at(-1).revert, true);
  assert.equal(JSON.parse(fs.readFileSync(`${tmp}/manifest.jsonl`, 'utf8')).instruction, episode.instruction);
  // A second writer cannot start on the same correction history.
  assert.throws(() => execFileSync(python, datasetArgs, { env: datasetEnv, stdio: 'pipe', timeout: 2000 }), error => error.status === 1 && /BlockingIOError/.test(error.stderr.toString()));
  await page.reload(); await page.locator('#edSave').waitFor();
  await page.locator('input[data-l="ins"]').fill('<img src=x onerror=alert(1)>'); await page.locator('#edSave').click();
  await page.waitForFunction(() => EDIT?.version === 6);
  assert.equal(await page.locator('input[data-l="ins"]').inputValue(), '<img src=x onerror=alert(1)>');
  assert.equal(await page.locator('img').count(), 0);
  await page.setViewportSize({ width: 1365, height: 1000 });
  await page.screenshot({ path: `${scratch}/internal-datasets-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${scratch}/internal-datasets-mobile.png`, fullPage: true });
  fs.renameSync(`${tmp}/edits.jsonl`, `${tmp}/saved-edits.jsonl`); fs.mkdirSync(`${tmp}/edits.jsonl`);
  assert.equal((await postEdit({ ...edit, version: 6 })).status(), 503);
  fs.rmdirSync(`${tmp}/edits.jsonl`); fs.renameSync(`${tmp}/saved-edits.jsonl`, `${tmp}/edits.jsonl`);
  assert.equal((await postEdit({ ...edit, version: 6 })).status(), 503);
  await stopDatasets(); await startDatasets();
  assert.equal((await (await context.request.get(`${base}/captioning_data/api/episode?${query}`)).json()).version, 6);
  await page.locator('#logout').click(); await page.locator('#login-view').waitFor();
  assert.equal((await context.request.get(`${base}/captioning_data/video?${query}`)).status(), 401);
  await page.locator('#identifier').fill('staff'); await page.locator('#password').fill('sample-password'); await page.locator('#login-button').click();
  await page.locator('.recording-row').first().waitFor(); expired = true;
  await page.locator('#refresh').click(); await page.getByText('Your session expired. Sign in again.', { exact: true }).waitFor();
  assert.equal(await page.locator('.recording-row').count(), 0); assert.deepEqual(errors, []);
  console.log('PASS: recording library; contributor datasets; protected metadata/assets/media; CSRF/origin; range video; validation; save/revert; concurrent edits; stable audit; replay; singleton writer; storage failure; safe dataset text; expiry keeps drafts; login return; Nginx restrictions.');
})().catch(error => { console.error(error); console.error(nginxLog.slice(-1800)); process.exitCode = 1; }).finally(async () => {
  clearTimeout(reloadTimer);
  if (browser) await browser.close();
  if (nginx && nginx.exitCode === null) { const exit = new Promise(resolve => nginx.once('exit', resolve)); nginx.kill('SIGTERM'); await exit; }
  if (datasetsServer && datasetsServer.exitCode === null) { const exit = new Promise(resolve => datasetsServer.once('exit', resolve)); datasetsServer.kill('SIGTERM'); await exit; }
  await new Promise(resolve => api.close(resolve));
  fs.rmSync(tmp, { recursive: true, force: true });
});
