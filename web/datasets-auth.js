'use strict';

window.DATASET_AUTH = (() => {
  const csrf = () => {
    const cookie = document.cookie.split('; ').find(value => value.startsWith('__Host-rbt_csrf='));
    return cookie ? decodeURIComponent(cookie.slice(cookie.indexOf('=') + 1)) : '';
  };
  const identity = user => ({ name: user.full_name || user.email || user.user_id,
    role: user.roles?.some(role => ['admin', 'caption_data_admin'].includes(role)) ? 'admin' : user.roles?.includes('caption_data_reviewer') ? 'reviewer' : 'member' });
  function askLogin(text = 'Your session expired. Unsaved edits remain here.', resume) {
    const box = document.querySelector('#login .gbox');
    box.replaceChildren();
    const title = document.createElement('h3'); title.textContent = 'Project1B sign in';
    const message = document.createElement('p'); message.textContent = text;
    const link = document.createElement('a'); link.textContent = 'Sign in in another tab';
    link.href = '/?next=/captioning_data/'; link.target = '_blank'; link.rel = 'noopener noreferrer';
    const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Resume session';
    retry.onclick = async () => {
      retry.disabled = true;
      try {
        const response = await fetch('/api/auth/me', { credentials: 'same-origin', cache: 'no-store' });
        if (!response.ok) throw new Error('Sign in to Project1B first, then resume here.');
        const user = identity(await response.json());
        document.querySelector('#login').hidden = true;
        const onResume = resume || window.DATASET_AUTH.resume;
        if (onResume) onResume(user);
      } catch (error) { message.textContent = error.message; }
      finally { retry.disabled = false; }
    };
    box.append(title, message, link, document.createElement('br'), retry);
    document.querySelector('#login').hidden = false;
  }
  async function request(path, options = {}) {
    const headers = { ...options.headers };
    if (options.method === 'POST') headers['X-CSRF-Token'] = csrf();
    const response = await fetch(`/captioning_data${path}`, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 401 && options.method !== 'POST') askLogin();
    if (!response.ok && options.method !== 'POST') {
      const body = await response.json().catch(() => null);
      throw new Error(body?.error || `Request failed (${response.status}).`);
    }
    return response;
  }
  async function logout() {
    const response = await fetch('/api/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf() }, credentials: 'same-origin' });
    if (!response.ok && response.status !== 401) throw new Error('Could not sign out. Try again.');
  }
  return { request, askLogin, logout };
})();
