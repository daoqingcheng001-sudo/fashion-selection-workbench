/* Account/session UI. The server remains authoritative for roles, leases and revisions. */
window.SelectionAuth = (() => {
  let user = null, onReady = async () => {}, gateKind = 'loading', checking = false;
  const clientId = crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
  const $ = s => document.querySelector(s);
  const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const roles = {admin:'管理员', editor:'编辑成员', viewer:'只读成员'};
  async function request(path, data, options = {}) {
    const headers = data === undefined ? {} : {'Content-Type':'application/json', 'X-CSRF-Token':user?.csrf_token || '', 'X-Selection-Client':clientId};
    const res = await fetch(path, {credentials:'same-origin', cache:'no-store', ...options,
      ...(data === undefined ? {} : {method:'POST', headers:{...headers,...options.headers}, body:JSON.stringify(data)})});
    let body;
    try { body = await res.json(); } catch { body = {error:'后台返回异常，请刷新后重试'}; }
    if (!res.ok) {
      const error = Object.assign(new Error(body.error || '请求失败'), {status:res.status, code:body.code, data:body});
      if (res.status === 401 && !path.startsWith('/api/auth/')) showLogin('登录已失效，可能已在另一设备登录。请重新登录。');
      throw error;
    }
    return body;
  }
  function setIdentity(value) {
    user = value;
    $('#reviewer').value = user?.display_name || user?.username || '';
    $('#account-role').textContent = roles[user?.role] || '';
    $('#admin-nav').hidden = user?.role !== 'admin';
    $('#notice').textContent = user?.role === 'viewer' ? '当前为只读账号：可以查看图文和下载已有表格；修改、采集与生图由编辑成员操作。' : '';
  }
  function gate(title, markup, kind) {
    gateKind = kind;
    $('#auth-gate').hidden = false;
    $('aside').inert = true;
    $('main').inert = true;
    if ($('#detail').open) $('#detail').close();
    $('#auth-gate').innerHTML = `<div class="auth-card"><img class="auth-logo" src="/favicon.svg" alt="Fashion Studio Logo"><h1>${esc(title)}</h1>${markup}<p id="auth-error" class="auth-error" role="alert"></p></div>`;
  }
  function showLogin(message = '', setup = false, localAllowed = false) {
    window.dispatchEvent(new Event('selection-auth-lost'));
    setIdentity(null);
    $('#content').innerHTML = '';
    if (setup && !localAllowed) {
      gate('后台尚未初始化', '<p>请管理员先在运行后台的这台 Mac 上打开工作台，创建首个管理员账号。设置完成后，员工即可通过内网地址登录。</p><button id="auth-retry">重新检查</button>', 'setup');
      $('#auth-retry').onclick = () => bootstrap(onReady);
      return;
    }
    gate(setup ? '创建管理员账号' : '登录Fashion Studio选款工作台', `${message ? `<p class="warning">${esc(message)}</p>` : ''}<p class="hint">${setup ? '首次设置仅允许在后台所在的 Mac 上完成。' : '使用管理员分配的账号登录。一个账号同一时间只能在一台设备使用。'}</p><form id="auth-form" class="auth-form"><label>账号<input id="auth-username" autocomplete="username" required maxlength="48" spellcheck="false"></label>${setup ? '<label>姓名<input id="auth-name" autocomplete="name" required maxlength="80"></label>' : ''}<label>密码<input id="auth-password" type="password" autocomplete="${setup?'new-password':'current-password'}" required ${setup?'minlength="12"':''}></label>${setup ? '<label>再次输入密码<input id="auth-confirm" type="password" autocomplete="new-password" required minlength="12"></label><p class="hint">密码至少 12 个字符，建议使用独立的长密码。</p>' : ''}<button class="primary" type="submit">${setup?'创建并登录':'登录'}</button><button type="button" id="replace-login" hidden>结束另一设备的会话并登录</button></form>`, setup ? 'setup' : 'login');
    let busy = false;
    const submit = async replace => {
      if (busy) return;
      if (!$('#auth-form').reportValidity()) return;
      if (setup && $('#auth-password').value !== $('#auth-confirm').value) { $('#auth-error').textContent = '两次输入的密码不一致'; return; }
      busy = true;
      $('#auth-error').textContent = '';
      $('#auth-form button[type="submit"]').disabled = true;
      try {
        await request(setup ? '/api/auth/setup' : '/api/auth/login', {username:$('#auth-username').value.trim(),password:$('#auth-password').value,
          ...(setup ? {display_name:$('#auth-name').value.trim()} : {replace_session:replace})});
        await bootstrap(onReady);
      } catch(e) {
        $('#auth-error').textContent = e.message;
        if (!setup && e.status === 409) $('#replace-login').hidden = false;
      } finally { busy = false; const b=$('#auth-form button[type="submit"]'); if(b)b.disabled=false; }
    };
    $('#auth-form').onsubmit = e => { e.preventDefault(); submit(false); };
    $('#replace-login').onclick = () => submit(true);
    $('#auth-username').focus();
  }
  function showPassword(required = false) {
    gate(required ? '首次登录，请修改密码' : '修改登录密码', `<p class="hint">${required?'临时密码仅用于首次登录。设置自己的密码后进入工作台。':'修改后，当前会话继续保留。请使用至少 12 个字符的密码。'}</p><form id="password-form" class="auth-form"><label>当前密码<input id="current-password" type="password" autocomplete="current-password" required></label><label>新密码<input id="new-password" type="password" autocomplete="new-password" minlength="12" required></label><label>再次输入新密码<input id="confirm-password" type="password" autocomplete="new-password" minlength="12" required></label><button class="primary" type="submit">保存新密码</button><button id="cancel-password" type="button">${required?'退出登录':'取消'}</button></form>`, 'password');
    $('#password-form').onsubmit = async e => {
      e.preventDefault();
      if ($('#new-password').value !== $('#confirm-password').value) { $('#auth-error').textContent='两次输入的新密码不一致'; return; }
      const button=$('#password-form button[type="submit"]');button.disabled=true;
      try {
        await request('/api/auth/password',{current_password:$('#current-password').value,new_password:$('#new-password').value});
        await bootstrap(onReady);
      } catch(err) { $('#auth-error').textContent=err.message; }
      finally { button.disabled=false; }
    };
    $('#cancel-password').onclick = required ? logout : hideGate;
  }
  function hideGate() { gateKind=''; $('#auth-gate').hidden=true; $('#auth-gate').innerHTML=''; $('aside').inert=false; $('main').inert=false; }
  async function logout() {
    try { await request('/api/auth/logout',{}); showLogin('已退出登录'); }
    catch(e) { if(e.status === 401)showLogin('已退出登录'); else $('#auth-error') ? $('#auth-error').textContent=e.message : window.alert(e.message); }
  }
  async function bootstrap(callback) {
    if (callback) onReady=callback;
    try {
      const auth=await request('/api/auth/me');
      if (!auth.user) { showLogin('',auth.needs_setup,auth.local_setup_allowed); return; }
      setIdentity(auth.user);
      if (user.must_change_password) { showPassword(true); return; }
      hideGate();
      await onReady();
    } catch(e) {
      gate('暂时无法连接后台', `<p class="warning">${esc(e.message)}</p><p class="hint">请确认运行后台的 Mac 已开机，并已连接同一个内网。</p><button id="auth-retry">重新连接</button>`, 'offline');
      $('#auth-retry').onclick=()=>bootstrap(onReady);
    }
  }
  async function checkSession() {
    if (!user || gateKind || checking) return;
    checking=true;
    try {
      const auth=await request('/api/auth/me');
      if (!auth.user)showLogin('登录已失效，请重新登录。');
      else { setIdentity(auth.user); if(user.must_change_password)showPassword(true); }
    } catch(e) { if(e.status===401)showLogin('登录已失效，请重新登录。'); }
    finally { checking=false; }
  }
  $('#logout').onclick=logout;
  $('#change-password').onclick=()=>showPassword(false);
  window.addEventListener('focus',checkSession);
  setInterval(checkSession,60000);
  return {request,bootstrap,clientId,get user(){return user;},get ready(){return !!user&&!gateKind;},get canEdit(){return !!user&&user.role!=='viewer';},roles};
})();
