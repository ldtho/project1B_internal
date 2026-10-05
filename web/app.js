'use strict';

const $ = id => document.getElementById(id);
const state = { user: null, videos: [], selected: null, epoch: 0 };
let listRequest, detailRequest;

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function message(id, text) {
  $(id).textContent = text;
  $(id).hidden = !text;
}

function bytes(value) {
  if (!Number.isFinite(Number(value)) || value == null) return '—';
  const size = Number(value);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const unit = size > 0 ? Math.min(Math.floor(Math.log(size) / Math.log(1024)), 4) : 0;
  return `${(size / 1024 ** unit).toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function duration(value) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const seconds = Math.max(0, Math.round(Number(value)));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function date(value) {
  if (!value) return 'Date unavailable';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Date unavailable' : parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function badge(status) {
  const item = node('span', 'status', (status || 'unknown').replaceAll('_', ' '));
  if (['uploaded', 'exported', 'qc', 'cutting', 'captioning', 'rejected'].includes(status)) item.classList.add(status);
  return item;
}

function showLogin(text = '') {
  state.epoch++;
  state.user = null;
  state.videos = [];
  state.selected = null;
  listRequest?.abort();
  detailRequest?.abort();
  $('recording-list').replaceChildren();
  $('recording-detail').replaceChildren();
  $('user-name').textContent = '';
  $('user-name').hidden = $('logout').hidden = $('library-view').hidden = $('datasets-view').hidden = true;
  $('login-view').hidden = false;
  $('password').value = '';
  message('login-error', text);
  message('account-notice', '');
}

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.body) headers['Content-Type'] = 'application/json';
  if (options.method === 'POST' && path !== '/api/auth/web/login') {
    const cookie = document.cookie.split('; ').find(value => value.startsWith('__Host-rbt_csrf='));
    if (cookie) headers['X-CSRF-Token'] = decodeURIComponent(cookie.slice(cookie.indexOf('=') + 1));
  }
  const response = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const text = typeof body?.detail === 'string' ? body.detail : `Request failed (${response.status}). Please try again.`;
    if (response.status === 401 && state.user) showLogin('Your session expired. Sign in again.');
    throw Object.assign(new Error(text), { status: response.status });
  }
  return response.status === 204 ? null : response.json();
}

async function openLibrary(user) {
  if (new URLSearchParams(location.search).get('next') === '/captioning_data/') {
    location.assign('/captioning_data/' + location.hash);
    return;
  }
  state.user = user;
  state.epoch++;
  $('user-name').textContent = user.full_name || user.email || 'Staff account';
  $('user-name').hidden = $('logout').hidden = $('datasets-view').hidden = false;
  $('library-view').hidden = !user.roles?.includes('admin');
  $('login-view').hidden = true;
  $('password').value = '';
  message('library-notice', '');
  message('account-notice', '');
  if (user.roles?.includes('admin')) await loadRecordings();
}

function renderList() {
  const query = $('search').value.trim().toLowerCase();
  const visible = state.videos.filter(item => [item.video_id, item.worker, item.task, item.pk].some(value => String(value ?? '').toLowerCase().includes(query)));
  $('recording-count').textContent = `${visible.length} of ${state.videos.length} loaded`;
  $('recording-list').replaceChildren();
  $('list-state').hidden = Boolean(visible.length);
  $('list-state').textContent = query ? 'No recordings match this search.' : 'No recordings in this status.';
  for (const item of visible) {
    const row = node('button', 'recording-row');
    row.type = 'button';
    row.classList.toggle('selected', item.pk === state.selected);
    row.setAttribute('aria-pressed', String(item.pk === state.selected));
    row.append(node('span', 'recording-title', item.task || item.description || item.video_id || `Recording #${item.pk}`));
    row.append(node('span', 'recording-worker', `${item.worker || 'Unknown worker'} · #${item.pk}`));
    const bottom = node('span', 'recording-bottom');
    bottom.append(badge(item.status), node('span', 'recording-meta', `${duration(item.duration_s)} · ${date(item.uploaded_at)}`));
    row.append(bottom);
    row.addEventListener('click', () => selectRecording(item.pk));
    $('recording-list').append(row);
  }
}

async function loadRecordings() {
  listRequest?.abort();
  detailRequest?.abort();
  const request = listRequest = new AbortController();
  const epoch = state.epoch;
  state.selected = null;
  state.videos = [];
  $('recording-detail').replaceChildren(node('p', 'empty-state', 'Select a recording to inspect its files.'));
  $('recording-list').replaceChildren();
  $('recording-count').textContent = '';
  $('list-state').hidden = false;
  $('list-state').textContent = 'Loading recordings…';
  $('refresh').disabled = true;
  message('library-notice', '');
  try {
    const status = $('status-filter').value;
    const payload = await api(`/api/admin/videos?limit=500${status ? `&status=${encodeURIComponent(status)}` : ''}`, { signal: request.signal });
    if (epoch !== state.epoch || request.signal.aborted) return;
    state.videos = payload.videos;
    renderList();
  } catch (error) {
    if (error.name === 'AbortError' || epoch !== state.epoch) return;
    $('list-state').textContent = 'Could not load recordings.';
    message('library-notice', error.message);
  } finally {
    if (listRequest === request) $('refresh').disabled = false;
  }
}

async function selectRecording(pk) {
  detailRequest?.abort();
  const request = detailRequest = new AbortController();
  const epoch = state.epoch;
  state.selected = pk;
  renderList();
  $('recording-detail').replaceChildren(node('p', 'empty-state', 'Loading recording…'));
  try {
    const item = await api(`/api/admin/videos/${pk}`, { signal: request.signal });
    if (epoch === state.epoch && !request.signal.aborted) renderDetail(item);
  } catch (error) {
    if (error.name !== 'AbortError' && epoch === state.epoch) $('recording-detail').replaceChildren(node('p', 'empty-state', error.message));
  }
}

function renderDetail(item) {
  const panel = $('recording-detail');
  panel.replaceChildren();
  const heading = node('div', 'detail-heading');
  heading.append(badge(item.status), node('span', 'detail-id', `#${item.pk}`), node('h2', '', item.task?.name || item.description || 'Recording'));
  heading.append(node('p', 'quiet', `${item.worker?.name || 'Unknown worker'} · ${date(item.uploaded_at)}`), node('span', 'quiet', item.session_uid || item.video_id));
  panel.append(heading);
  const roles = (item.media_roles || []).filter(role => ['primary', 'reference'].includes(role.role));
  if (roles.length) {
    const preview = node('div', 'preview');
    const video = node('video');
    video.controls = true;
    video.preload = 'metadata';
    video.playsInline = true;
    video.src = `/api/admin/media/${item.pk}?role=${roles[0].role}&variant=auto`;
    const controls = node('div', 'preview-controls');
    controls.append(node('span', '', 'Video preview · original ZIP keeps full quality'));
    if (roles.length > 1) {
      const select = node('select');
      select.setAttribute('aria-label', 'Video stream');
      for (const role of roles) {
        const option = node('option', '', role.label || role.role);
        option.value = role.role;
        select.append(option);
      }
      select.addEventListener('change', () => { video.src = `/api/admin/media/${item.pk}?role=${select.value}&variant=auto`; video.load(); });
      controls.append(select);
    }
    const error = node('p', 'preview-error', 'Preview unavailable. You can still download an available original bundle.');
    error.hidden = true;
    video.addEventListener('error', () => { error.hidden = false; });
    video.addEventListener('loadedmetadata', () => { error.hidden = true; });
    preview.append(video, controls, error);
    panel.append(preview);
  }
  const files = item.files || [];
  const metrics = node('dl', 'metrics');
  for (const [label, value] of [['Duration', duration(item.duration_s)], ['Resolution', item.resolution || '—'], ['Session files', bytes(files.reduce((sum, file) => sum + Number(file.size || 0), 0))]]) {
    const metric = node('div', 'metric');
    metric.append(node('dt', '', label), node('dd', '', value));
    metrics.append(metric);
  }
  panel.append(metrics);
  const block = node('div', 'section-block');
  block.append(node('h3', '', `Session files (${files.length})`));
  const list = node('ul', 'file-list');
  for (const file of files) {
    const entry = node('li');
    entry.append(node('span', 'file-name', file.name), node('span', 'file-size', bytes(file.size)));
    list.append(entry);
  }
  block.append(list);
  const hasMcap = files.some(file => /\.mcap$/i.test(file.name));
  const sensor = node('div', 'sensor-note');
  if (hasMcap) sensor.append(node('span', 'sensor-indicator'));
  sensor.append(node('span', '', hasMcap ? `MCAP present${item.sensors ? ` · ${item.sensors.topics?.length || 0} topics · ${Number(item.sensors.message_count || 0).toLocaleString()} messages` : ''}` : 'No MCAP file in this session.'));
  block.append(sensor);
  if (item.device) block.append(node('p', 'details-note', `${item.device.model || item.device.platform || 'Device'} · ${item.capture_profile || item.capture_tier || 'Capture profile unavailable'}`));
  panel.append(block);
  const download = node('div', 'download-block');
  download.append(node('h3', '', 'Full original recording'), node('p', '', 'Download every file in the finalized phone upload: video, MCAP, metadata, and companion recordings when captured. No recompression.'));
  const button = node('a', 'button primary', 'Download original ZIP');
  const available = Boolean(item.original_bundle_available);
  button.setAttribute('role', 'link');
  button.setAttribute('aria-disabled', String(!available));
  button.target = '_blank';
  button.rel = 'noopener noreferrer';
  if (available) button.href = `/api/admin/videos/${item.pk}/original-bundle`;
  else button.tabIndex = -1;
  const notice = node('p', 'download-state', available ? 'Your browser downloads the ZIP directly. Large recordings may take time.' : 'Full original download unavailable: this recording has no complete finalized phone upload.');
  button.addEventListener('click', event => {
    if (!available) {
      event.preventDefault();
      return;
    }
    notice.textContent = 'Download requested. Check your browser’s downloads; any server error opens in a separate tab.';
  });
  download.append(button, notice);
  panel.append(download);
}

$('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('login-button').disabled = true;
  $('login-button').textContent = 'Signing in…';
  message('login-error', '');
  try {
    const user = await api('/api/auth/web/login', { method: 'POST', body: JSON.stringify({ email: $('identifier').value.trim(), password: $('password').value }) });
    await openLibrary(user);
  } catch (error) {
    message('login-error', error.message);
  } finally {
    $('login-button').disabled = false;
    $('login-button').textContent = 'Sign in';
  }
});

$('logout').addEventListener('click', async () => {
  $('logout').disabled = true;
  try {
    await api('/api/auth/logout', { method: 'POST' });
    showLogin();
  } catch (error) {
    if (error.status !== 401) message('account-notice', `Could not sign out: ${error.message}`);
  } finally {
    $('logout').disabled = false;
  }
});
$('refresh').addEventListener('click', loadRecordings);
$('status-filter').addEventListener('change', loadRecordings);
$('search').addEventListener('input', renderList);

(async () => {
  $('login-button').disabled = true;
  $('login-button').textContent = 'Checking session…';
  try { await openLibrary(await api('/api/auth/me')); }
  catch (error) { showLogin(error.status === 401 ? '' : error.message); }
  finally { $('login-button').disabled = false; $('login-button').textContent = 'Sign in'; }
})();
