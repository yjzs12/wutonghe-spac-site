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
          <label class="auth-field">账号
            <input id="authUser" name="username" autocomplete="username" spellcheck="false" required>
          </label>
          <label class="auth-field">密码
            <input id="authPwd" name="password" type="password" autocomplete="current-password" required>
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
          <p class="auth-foot">账号由管理员统一分配，不开放自助注册。</p>
        </form>
      </div>`);
    document.body.appendChild(el);
    return el;
  }

  function showMask() {
    if (!mask) mask = buildMask();
    mask.style.display = 'flex';
    window.__authPending = true;
    document.documentElement.classList.add('auth-locked');
    loadCaptcha();
    setTimeout(() => { const u = document.getElementById('authUser'); if (u) u.focus(); }, 60);
  }

  function hideMask() {
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
    if (e.target && e.target.id === 'authCapImg') loadCaptcha();
  });
  document.addEventListener('submit', function (e) {
    if (e.target && e.target.id === 'authForm') submit(e);
  });
})();
