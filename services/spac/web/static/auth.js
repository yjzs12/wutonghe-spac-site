/* ==========================================================================
   梧桐河灌区模型平台 —— 登录遮罩（两个独立网站共用）
   源文件：assets/web/auth.js   同步到各服务：python 工具/同步前端资源.py

   为什么用「前端遮罩」而不是服务端跳转：
   同一份页面要同时兼容 `/`（独立网站）与 `/crop/`（网关内）两种部署形态，
   服务端 302 去登录页会多一层前缀与回跳路径的坑。页面 HTML 本身不含任何业务
   数据，真正的数据（任务、日志、产物、上传批次）全部由 /api/* 与 /files/* 提供，
   服务端已在中间件里逐个校验归属 —— 遮罩只负责「让人先登录」这一件事。
   ========================================================================== */
(function () {
  'use strict';

  const BASE = (document.querySelector('meta[name="app-base"]') || {}).content || '';
  const SITE = (document.title || '').split('·').pop().trim() || '梧桐河灌区模型平台';

  let mask = null, capId = '', busy = false, everIn = false;
  const HISTORY_KEY = 'wth.login-accounts.v1:' + (BASE || '/');
  let accountHistory = [], historyIndex = -1, historyMatches = [];

  function savedAccounts() {
    try {
      const stored = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
      if (Array.isArray(stored)) {
        accountHistory = [...new Set(stored.filter(v => typeof v === 'string' && v.trim()).map(v => v.trim()))];
      }
    } catch (e) { /* 存储受限或记录损坏时，仍可正常登录 */ }
    return accountHistory;
  }

  function storeAccounts(accounts) {
    accountHistory = accounts;
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(accounts)); } catch (e) { /* 保留本页记录 */ }
  }

  function rememberAccount(username) {
    if (typeof username !== 'string' || !username.trim()) return;
    const name = username.trim();
    storeAccounts([name, ...savedAccounts().filter(v => v !== name)]);
  }

  function closeAccountHistory() {
    const panel = document.getElementById('authHistoryPanel');
    const input = document.getElementById('authUser');
    if (panel) panel.hidden = true;
    if (input) {
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
    }
    historyIndex = -1;
    historyMatches = [];
  }

  function chooseAccount(username) {
    document.getElementById('authUser').value = username;
    // 切换账号时清空未提交的凭据，避免带上另一个账号的密码。
    document.getElementById('authPwd').value = '';
    document.getElementById('authCode').value = '';
    closeAccountHistory();
    document.getElementById('authPwd').focus();
  }

  function highlightAccount(index) {
    historyIndex = index;
    const options = document.querySelectorAll('#authUserHistory [role="option"]');
    options.forEach((option, i) => option.setAttribute('aria-selected', String(i === index)));
    const input = document.getElementById('authUser');
    if (index >= 0 && options[index]) {
      input.setAttribute('aria-activedescendant', options[index].id);
      options[index].scrollIntoView({ block: 'nearest' });
    } else input.removeAttribute('aria-activedescendant');
  }

  function showAccountHistory(filter = '') {
    const panel = document.getElementById('authHistoryPanel');
    const list = document.getElementById('authUserHistory');
    const input = document.getElementById('authUser');
    if (!panel || !savedAccounts().length) { closeAccountHistory(); return; }
    historyMatches = savedAccounts().filter(v => v.toLowerCase().includes(filter.toLowerCase()));
    list.replaceChildren();
    historyMatches.forEach((username, i) => {
      const option = document.createElement('button');
      option.type = 'button'; option.className = 'auth-history-option';
      option.id = 'authHistoryOption' + i; option.tabIndex = -1;
      option.setAttribute('role', 'option'); option.setAttribute('aria-selected', 'false');
      option.textContent = username;
      option.addEventListener('mousedown', e => e.preventDefault());
      option.addEventListener('click', () => chooseAccount(username));
      list.appendChild(option);
    });
    document.getElementById('authHistoryEmpty').hidden = historyMatches.length > 0;
    panel.hidden = false; input.setAttribute('aria-expanded', 'true');
    highlightAccount(-1);
  }

  function wireAccountHistory(el) {
    const input = el.querySelector('#authUser');
    const account = el.querySelector('.auth-account');
    input.addEventListener('focus', () => showAccountHistory());
    input.addEventListener('click', () => showAccountHistory());
    input.addEventListener('input', () => showAccountHistory(input.value));
    input.addEventListener('keydown', e => {
      const open = !el.querySelector('#authHistoryPanel').hidden;
      if (e.key === 'Escape') { closeAccountHistory(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!open) showAccountHistory(input.value);
        if (historyMatches.length) {
          const step = e.key === 'ArrowDown' ? 1 : -1;
          highlightAccount(historyIndex < 0 ? (step > 0 ? 0 : historyMatches.length - 1) : (historyIndex + step + historyMatches.length) % historyMatches.length);
        }
      } else if (e.key === 'Enter' && open && historyIndex >= 0) {
        e.preventDefault(); chooseAccount(historyMatches[historyIndex]);
      }
    });
    account.addEventListener('focusout', () => setTimeout(() => {
      if (!account.contains(document.activeElement)) closeAccountHistory();
    }, 0));
    el.addEventListener('click', e => { if (!account.contains(e.target)) closeAccountHistory(); });
    el.querySelector('#authForgetAccounts').addEventListener('click', () => {
      storeAccounts([]); closeAccountHistory(); input.focus();
    });
    window.addEventListener('storage', e => {
      if (e.key === HISTORY_KEY || e.key === null) {
        accountHistory = []; savedAccounts(); closeAccountHistory();
      }
    });
  }

  function ui(html) {
    const d = document.createElement('div');
    d.innerHTML = html.trim();
    return d.firstElementChild;
  }

  function buildMask() {
    const el = ui(`
      <div class="auth-mask" id="authMask">
        <form class="auth-card" id="authForm" autocomplete="on">
          <div class="auth-head">
            <span class="auth-logo">梧</span>
            <div>
              <div class="auth-title">梧桐河灌区</div>
              <div class="auth-sub">${SITE}</div>
            </div>
          </div>
          <div class="auth-field auth-account">
            <label for="authUser">账号</label>
            <input id="authUser" name="username" autocomplete="username" spellcheck="false" required
                   role="combobox" aria-expanded="false" aria-controls="authUserHistory"
                   aria-autocomplete="list" aria-haspopup="listbox">
            <div class="auth-history-panel" id="authHistoryPanel" hidden>
              <div class="auth-history-head">
                <span>此浏览器登录过的账号</span>
                <button id="authForgetAccounts" type="button">清除记录</button>
              </div>
              <div id="authUserHistory" role="listbox" aria-label="已登录账号"></div>
              <div class="auth-history-empty" id="authHistoryEmpty" hidden>没有匹配的账号</div>
            </div>
          </div>
          <label class="auth-field">密码
            <span class="auth-pwd">
              <input id="authPwd" name="password" type="password" autocomplete="current-password" required>
              <button class="auth-eye" id="authEye" type="button"
                      aria-controls="authPwd" aria-pressed="false" aria-label="显示密码" title="显示密码">
                <svg class="eye-open" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M2 12s3.6-6.6 10-6.6S22 12 22 12s-3.6 6.6-10 6.6S2 12 2 12z"/>
                  <circle cx="12" cy="12" r="2.7"/>
                </svg>
                <svg class="eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M3 3l18 18"/>
                  <path d="M10.6 6.1A10.6 10.6 0 0 1 12 6c6.4 0 10 6 10 6a17.9 17.9 0 0 1-3.3 3.9"/>
                  <path d="M6.6 7.7C3.9 9.6 2 12 2 12s3.6 6.6 10 6.6c1.5 0 2.9-.4 4.1-1"/>
                  <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>
                </svg>
              </button>
            </span>
          </label>
          <label class="auth-field">验证码
            <span class="auth-cap">
              <input id="authCode" name="captcha" maxlength="4" autocomplete="off"
                     inputmode="latin" spellcheck="false" required>
              <img id="authCapImg" alt="验证码" title="看不清？点击换一张">
            </span>
          </label>
          <div class="auth-err" id="authErr"></div>
          <button class="btn btn-primary auth-submit" id="authSubmit" type="submit">登 录</button>
          <p class="auth-foot">登录成功后在此浏览器记住账号，不保存密码。<br>账号由管理员统一分配，不开放自助注册。</p>
        </form>
      </div>`);
    document.body.appendChild(el);
    wireAccountHistory(el);
    return el;
  }

  function showMask() {
    if (!mask) mask = buildMask();
    mask.style.display = 'flex';
    window.__authPending = true;
    document.documentElement.classList.add('auth-locked');
    resetEye();                            // 每次弹回登录框都回到「隐藏密码」状态
    loadCaptcha();
    setTimeout(() => { const u = document.getElementById('authUser'); if (u) u.focus(); }, 60);
  }

  /* 密码可见性：默认隐藏，点右侧小眼睛切换 */
  function resetEye() {
    const pwd = document.getElementById('authPwd');
    const eye = document.getElementById('authEye');
    if (pwd) pwd.type = 'password';
    if (eye) {
      eye.classList.remove('is-on');
      eye.setAttribute('aria-pressed', 'false');
      eye.setAttribute('aria-label', '显示密码');
      eye.title = '显示密码';
    }
  }

  function toggleEye(btn) {
    const pwd = document.getElementById('authPwd');
    if (!pwd) return;
    const show = pwd.type === 'password';
    pwd.type = show ? 'text' : 'password';
    btn.classList.toggle('is-on', show);
    btn.setAttribute('aria-pressed', show ? 'true' : 'false');
    btn.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    btn.title = show ? '隐藏密码' : '显示密码';
    const pos = pwd.value.length;          // 切换后光标归位到末尾
    pwd.focus();
    try { pwd.setSelectionRange(pos, pos); } catch (e) { /* 部分浏览器不支持则忽略 */ }
  }

  function hideMask() {
    closeAccountHistory();
    if (mask) mask.style.display = 'none';
    window.__authPending = false;
    everIn = true;
    document.documentElement.classList.remove('auth-locked');
  }

  function err(msg) {
    const box = document.getElementById('authErr');
    if (box) box.textContent = msg || '';
  }

  async function loadCaptcha() {
    const img = document.getElementById('authCapImg');
    const code = document.getElementById('authCode');
    if (code) code.value = '';
    try {
      const r = await fetch(BASE + '/api/auth/captcha', { cache: 'no-store' });
      const d = await r.json();
      if (d && d.ok) {
        capId = d.id;
        if (img) img.src = d.image;
      } else {
        err('验证码加载失败，请刷新页面');
      }
    } catch (e) {
      err('网络异常，验证码加载失败');
    }
  }

  async function submit(ev) {
    ev.preventDefault();
    if (busy) return;
    busy = true;
    err('');
    const btn = document.getElementById('authSubmit');
    if (btn) { btn.disabled = true; btn.textContent = '登录中…'; }
    try {
      const body = {
        username: (document.getElementById('authUser') || {}).value || '',
        password: (document.getElementById('authPwd') || {}).value || '',
        captcha_id: capId,
        captcha: (document.getElementById('authCode') || {}).value || '',
      };
      const r = await fetch(BASE + '/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        rememberAccount((d.user || {}).user || body.username);
        location.reload();
        return;
      }
      err(d.error || '登录失败');
      loadCaptcha();                       // 验证码一次性，失败必然要换
    } catch (e) {
      err('网络异常：' + e.message);
      loadCaptcha();
    } finally {
      busy = false;
      if (btn) { btn.disabled = false; btn.textContent = '登 录'; }
    }
  }

  function paintUser(user) {
    const right = document.querySelector('.navbar .nav-right');
    if (!right || document.getElementById('authWho')) return;
    const wrap = ui(`
      <span class="auth-who" id="authWho">
        <span class="auth-name">${user.name || user.user}</span>
        <span class="auth-role">${user.role === 'admin' ? '管理员' : '普通用户'}</span>
        <button class="btn btn-sm" id="authOut" type="button">退出</button>
      </span>`);
    right.insertBefore(wrap, right.firstChild);
    document.getElementById('authOut').addEventListener('click', async () => {
      await fetch(BASE + '/api/auth/logout', { method: 'POST' });
      location.reload();
    });
  }

  async function boot() {
    showMask();                            // 先盖上，别让人看到「能点但会 401」的界面
    try {
      const r = await fetch(BASE + '/api/auth/me', { cache: 'no-store' });
      if (r.ok) {
        const d = await r.json();
        if (d && d.ok) {
          rememberAccount((d.user || {}).user);
          hideMask();
          paintUser(d.user);
          return;
        }
      }
    } catch (e) { /* 网络异常 → 留在登录界面 */ }
    showMask();
  }

  /* 任何接口返回 401 都把登录框拉回来（会话过期时不用手动刷新） */
  window.__authNeedLogin = function () {
    /* 首次访问也是 401，别一上来就喊「过期」——只有登录过才谈得上过期 */
    err(everIn ? '登录已过期，请重新登录' : '');
    showMask();
  };

  document.addEventListener('DOMContentLoaded', boot);

  document.addEventListener('click', function (e) {
    if (!e.target || !e.target.closest) return;
    if (e.target.id === 'authCapImg') loadCaptcha();
    const eye = e.target.closest('#authEye');
    if (eye) toggleEye(eye);
  });
  document.addEventListener('submit', function (e) {
    if (e.target && e.target.id === 'authForm') submit(e);
  });
})();
