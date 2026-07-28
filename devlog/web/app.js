/* DevLog 前端逻辑 —— 无框架依赖，纯 ES6 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const S = {
  tasks: [], issues: [], shortcuts: [], timeline: [], attachments: [],
  commits: [], logs: [], settings: {}, stats: {}, gitStatus: null,
  page: 'today', taskFilter: 'all', issueFilter: 'open',
  logFilter: 'all', tlFilter: 'all', search: '',
  activeTask: null, timer: { on: false, start: 0, acc: 0, tid: null },
};

/* ---------------- 基础工具 ---------------- */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, body) {
  const opt = body !== undefined
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({ ok: false, error: '返回内容不是 JSON' }));
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, 2600);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000), n = new Date();
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (d.toDateString() === n.toDateString()) return hm;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}
function fmtDate(ts) {
  const d = new Date(ts * 1000), n = new Date();
  const y = new Date(n.getTime() - 86400000);
  if (d.toDateString() === n.toDateString()) return '今天';
  if (d.toDateString() === y.toDateString()) return '昨天';
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}
function dur(sec) {
  if (!sec) return '';
  if (sec < 3600) return `${Math.floor(sec / 60)}分`;
  return `${Math.floor(sec / 3600)}时${Math.floor((sec % 3600) / 60)}分`;
}

const P_LABEL = { 1: '高', 2: '中', 3: '低' };
const ST_LABEL = { todo: '待办', doing: '进行中', blocked: '受阻', done: '已完成' };
const SEV_LABEL = { blocker: '阻塞', high: '高', normal: '普通', low: '低' };
const KIND_ICON = { task: '📋', issue: '🐛', git: '🌿', log: '📊', note: '📝', attachment: '🖼', focus: '⏱', shortcut: '⌨️' };

/* ---------------- 数据加载 ---------------- */
async function boot() {
  try {
    const d = await api('/api/bootstrap');
    Object.assign(S, {
      tasks: d.tasks, issues: d.issues, shortcuts: d.shortcuts, timeline: d.timeline,
      attachments: d.attachments, commits: d.commits, logs: d.logs,
      settings: d.settings, gitStatus: d.git_status,
    });
    S.activeTask = d.settings.active_task ? Number(d.settings.active_task) : null;
    $('#git-repo').value = d.settings.git_repo || '';
    $('#set-base').value = d.settings.ai_base_url || '';
    $('#set-model').value = d.settings.ai_model || '';
    if (d.settings.has_key) $('#set-key').placeholder = '已保存（留空则不修改）';
    const st = await api('/api/stats');
    S.stats = st.stats;
    renderAll();
  } catch (e) {
    toast('加载失败：' + e.message, 'err');
  }
}

async function refresh() {
  const d = await api('/api/bootstrap');
  Object.assign(S, {
    tasks: d.tasks, issues: d.issues, shortcuts: d.shortcuts, timeline: d.timeline,
    attachments: d.attachments, commits: d.commits, logs: d.logs,
    settings: d.settings, gitStatus: d.git_status,
  });
  S.activeTask = d.settings.active_task ? Number(d.settings.active_task) : null;
  S.stats = (await api('/api/stats')).stats;
  renderAll();
}

/* ---------------- 渲染 ---------------- */
function renderAll() {
  renderBadges(); renderStats(); renderToday(); renderTasks();
  renderIssues(); renderTimeline(); renderShots(); renderShortcuts();
  renderLogHistory(); renderGit(); renderNowCard();
}

function renderBadges() {
  const open = S.tasks.filter(t => t.status !== 'done').length;
  const iss = S.issues.filter(i => i.status === 'open').length;
  const err = S.logs.filter(l => l.level === 'error').length;
  $('#bd-tasks').textContent = open;
  $('#bd-issues').textContent = iss;
  $('#bd-issues').classList.toggle('hot', iss > 0);
  $('#bd-logs').textContent = err;
  $('#bd-logs').classList.toggle('hot', err > 0);
}

function renderStats() {
  const s = S.stats;
  const cards = [
    { n: s.tasks_doing || 0, l: '进行中', c: 'info' },
    { n: s.tasks_todo || 0, l: '待办', c: '' },
    { n: s.done_today || 0, l: '今日完成', c: 'ok' },
    { n: s.tasks_blocked || 0, l: '受阻', c: s.tasks_blocked ? 'bad' : '' },
    { n: s.issues_open || 0, l: '未解决问题', c: s.issues_open ? 'warn' : '' },
    { n: s.errors_today || 0, l: '今日错误', c: s.errors_today ? 'bad' : '' },
    { n: dur(s.focus_today) || '0分', l: '今日专注', c: 'info' },
    { n: s.commits_today || 0, l: '今日提交', c: '' },
  ];
  $('#stats').innerHTML = cards.map(c =>
    `<div class="stat ${c.c}"><div class="n">${esc(c.n)}</div><div class="l">${c.l}</div></div>`).join('');
}

function taskHTML(t, compact = false) {
  const done = t.status === 'done';
  const act = S.activeTask === t.id;
  return `<div class="task ${done ? 'is-done' : ''} ${act ? 'active-task' : ''}" data-id="${t.id}">
    <div class="check ${done ? 'on' : ''}" data-act="toggle" data-id="${t.id}">✓</div>
    <div class="t-body">
      <div class="t-title" data-act="edit" data-id="${t.id}" style="cursor:pointer">${esc(t.title)}</div>
      ${!compact && t.body ? `<div class="t-note">${esc(t.body)}</div>` : ''}
      <div class="t-meta">
        <span class="tag ${t.status}">${ST_LABEL[t.status] || t.status}</span>
        <span class="tag p${t.priority}">${P_LABEL[t.priority]}优先</span>
        ${t.project ? `<span class="tag">${esc(t.project)}</span>` : ''}
        ${t.branch ? `<span class="tag">🌿 ${esc(t.branch)}</span>` : ''}
        ${t.seconds ? `<span class="tag">⏱ ${dur(t.seconds)}</span>` : ''}
        ${(t.tags || '').split(',').filter(Boolean).map(x => `<span class="tag">#${esc(x.trim())}</span>`).join('')}
      </div>
    </div>
    <div class="t-actions">
      ${act ? '' : `<button class="btn ghost sm" data-act="activate" data-id="${t.id}" title="设为当前任务">🎯</button>`}
      <button class="btn ghost sm" data-act="edit" data-id="${t.id}" title="编辑">✎</button>
      <button class="btn ghost sm danger" data-act="del" data-id="${t.id}" title="删除">🗑</button>
    </div>
  </div>`;
}

const EMPTY = (icon, txt) => `<div class="empty"><span class="big">${icon}</span>${txt}</div>`;

function renderToday() {
  const doing = S.tasks.filter(t => t.status === 'doing');
  const todo = S.tasks.filter(t => t.status === 'todo').sort((a, b) => a.priority - b.priority).slice(0, 6);
  const iss = S.issues.filter(i => i.status === 'open').slice(0, 5);

  $('#today-doing').innerHTML = doing.length ? doing.map(t => taskHTML(t)).join('')
    : EMPTY('🎯', '还没有进行中的任务，从待办里挑一个开始吧');
  $('#today-todo').innerHTML = todo.length ? todo.map(t => taskHTML(t, true)).join('')
    : EMPTY('✨', '待办清空了');
  $('#today-issues').innerHTML = iss.length ? iss.map(issueHTML).join('')
    : EMPTY('✅', '没有未解决的问题');
  $('#today-timeline').innerHTML = tlHTML(S.timeline.slice(0, 14));
}

function renderTasks() {
  let list = S.tasks;
  if (S.taskFilter !== 'all') list = list.filter(t => t.status === S.taskFilter);
  if (S.search) {
    const q = S.search.toLowerCase();
    list = list.filter(t => (t.title + t.body + t.tags + t.project).toLowerCase().includes(q));
  }
  $('#task-list').innerHTML = list.length ? list.map(t => taskHTML(t)).join('')
    : EMPTY('📋', '没有匹配的任务');
}

function issueHTML(i) {
  const open = i.status === 'open';
  return `<div class="task" data-iid="${i.id}">
    <div class="check ${open ? '' : 'on'}" data-act="i-toggle" data-id="${i.id}">✓</div>
    <div class="t-body">
      <div class="t-title" data-act="i-edit" data-id="${i.id}" style="cursor:pointer">${esc(i.title)}</div>
      ${i.detail ? `<div class="t-note">${esc(i.detail.slice(0, 300))}</div>` : ''}
      ${i.resolution ? `<div class="t-note">✅ ${esc(i.resolution)}</div>` : ''}
      <div class="t-meta">
        <span class="tag ${i.severity === 'blocker' || i.severity === 'high' ? 'blocked' : ''}">${SEV_LABEL[i.severity] || i.severity}</span>
        <span class="tag ${open ? 'todo' : 'done'}">${open ? '未解决' : (i.status === 'wontfix' ? '不修' : '已解决')}</span>
        ${i.task_title ? `<span class="tag">📋 ${esc(i.task_title)}</span>` : ''}
        <span class="tag">${fmtTime(i.created_at)}</span>
      </div>
    </div>
    <div class="t-actions">
      <button class="btn ghost sm" data-act="i-edit" data-id="${i.id}">✎</button>
      <button class="btn ghost sm danger" data-act="i-del" data-id="${i.id}">🗑</button>
    </div>
  </div>`;
}

function renderIssues() {
  let list = S.issues;
  if (S.issueFilter === 'open') list = list.filter(i => i.status === 'open');
  if (S.search) {
    const q = S.search.toLowerCase();
    list = list.filter(i => (i.title + i.detail).toLowerCase().includes(q));
  }
  $('#issue-list').innerHTML = list.length ? list.map(issueHTML).join('')
    : EMPTY('🐛', '暂无问题记录');
}

function tlHTML(items) {
  if (!items.length) return EMPTY('🕒', '还没有活动记录');
  let out = '', lastDate = '';
  for (const e of items) {
    const d = fmtDate(e.ts);
    if (d !== lastDate) { out += `<div class="tl-date-sep">${d}</div>`; lastDate = d; }
    out += `<div class="tl-item ${e.kind}">
      <div class="tl-time">${KIND_ICON[e.kind] || '•'} ${fmtTime(e.ts)}</div>
      <div class="tl-title">${esc(e.title)}</div>
      ${e.detail ? `<div class="tl-detail">${esc(e.detail.slice(0, 400))}</div>` : ''}
    </div>`;
  }
  return out;
}

function renderTimeline() {
  let items = S.timeline;
  if (S.tlFilter !== 'all') items = items.filter(e => e.kind === S.tlFilter);
  if (S.search) {
    const q = S.search.toLowerCase();
    items = items.filter(e => (e.title + e.detail).toLowerCase().includes(q));
  }
  $('#timeline-full').innerHTML = tlHTML(items);
}

function renderShots() {
  $('#shot-list').innerHTML = S.attachments.length ? S.attachments.map(a =>
    `<div class="shot" data-shot="${esc(a.stored)}">
      <img src="/files/${encodeURIComponent(a.stored)}" alt="${esc(a.filename)}" loading="lazy">
      <div class="cap">${esc(a.note || a.filename)}</div>
    </div>`).join('') : EMPTY('🖼', '还没有图片，粘贴或拖拽截图试试');
}

function renderShortcuts() {
  $('#sc-list').innerHTML = S.shortcuts.length ? S.shortcuts.map(s =>
    `<div class="sc-row">
      <div class="sc-keys">${s.keys.split('+').map(k => `<kbd>${esc(k.trim())}</kbd>`).join('+')}</div>
      <div style="flex:1">${esc(s.description)}
        ${s.app ? `<span class="tag" style="margin-left:6px">${esc(s.app)}</span>` : ''}</div>
      <span class="sc-hits">×${s.hits}</span>
      <button class="btn ghost sm" data-act="sc-hit" data-id="${s.id}" title="用过一次">👍</button>
      <button class="btn ghost sm danger" data-act="sc-del" data-id="${s.id}">🗑</button>
    </div>`).join('') : EMPTY('⌨️', '记录下你常用的快捷键，慢慢就记住了');
}

function renderLogHistory() {
  let logs = S.logs;
  if (S.logFilter !== 'all') logs = logs.filter(l => l.level === S.logFilter);
  $('#log-history').innerHTML = logs.length ? logs.slice(0, 200).map(l =>
    `<div class="log-line ${l.level}">${esc(l.message)}</div>`).join('')
    : EMPTY('📜', '还没有日志记录');
}

function reportHTML(r) {
  const badge = { error: 'bad', warn: 'warn' };
  let h = `<div class="card"><h3>🔍 分析结果</h3>
    <div class="grid-stats">
      <div class="stat"><div class="n">${r.total}</div><div class="l">总行数</div></div>
      <div class="stat ${r.error_count ? 'bad' : 'ok'}"><div class="n">${r.error_count}</div><div class="l">错误</div></div>
      <div class="stat ${r.warn_count ? 'warn' : ''}"><div class="n">${r.warn_count}</div><div class="l">警告</div></div>
      <div class="stat"><div class="n">${r.clusters.length}</div><div class="l">问题类别</div></div>
    </div>
    <p style="margin:4px 0 12px;color:var(--muted);font-size:13px">${esc(r.headline)}</p>`;
  if (r.peak) h += `<p style="font-size:12px;color:var(--amber)">⚡ 错误高峰在 ${r.peak.hour}，共 ${r.peak.count} 条</p>`;
  h += '</div>';

  if (r.clusters.length) {
    h += '<div class="card"><h3>🧩 同类问题归并</h3>';
    for (const c of r.clusters) {
      h += `<div class="cluster ${c.level}">
        <div class="row">
          <span class="cnt">×${c.count}</span>
          <span class="tag ${c.level === 'error' ? 'blocked' : ''}">${c.level}</span>
          <span class="spacer"></span>
          <button class="btn sm" data-act="log2issue" data-msg="${esc(c.sample)}" data-raw="${esc((c.raw || '').slice(0, 1500))}">存为问题</button>
        </div>
        <div class="msg">${esc(c.raw && c.raw.length > c.sample.length ? c.raw : c.sample)}</div>
        ${c.hint ? `<div class="hint"><b>${esc(c.hint.name)}</b> — ${esc(c.hint.advice)}</div>` : ''}
      </div>`;
    }
    h += '</div>';
  }
  return h;
}

function renderGit() {
  const g = S.gitStatus;
  $('#git-status').innerHTML = g && g.ok
    ? `<div class="row wrap">
        <span class="tag doing">🌿 ${esc(g.branch)}</span>
        <span class="tag ${g.clean ? 'done' : 'p2'}">${g.clean ? '工作区干净' : g.changed + ' 个文件改动'}</span>
        ${g.staged ? `<span class="tag">${g.staged} 已暂存</span>` : ''}
        ${g.untracked ? `<span class="tag">${g.untracked} 未跟踪</span>` : ''}
      </div>
      ${g.files && g.files.length ? `<div style="margin-top:8px;font-family:var(--mono);font-size:11px;color:var(--muted)">${g.files.map(esc).join('<br>')}</div>` : ''}`
    : (g && g.error ? `<span class="tag blocked">${esc(g.error)}</span>` : '<span style="color:var(--dim);font-size:12px">填入仓库路径后点同步</span>');

  $('#commit-list').innerHTML = S.commits.length ? S.commits.map(c =>
    `<div class="task">
      <div class="t-body">
        <div class="t-title" style="font-size:13px">${esc(c.subject)}</div>
        <div class="t-meta">
          <span class="tag" style="font-family:var(--mono)">${esc(c.sha.slice(0, 8))}</span>
          <span class="tag">${esc(c.author)}</span>
          <span class="tag ok">+${c.insertions}</span><span class="tag">-${c.deletions}</span>
          ${c.task_title ? `<span class="tag doing">📋 ${esc(c.task_title)}</span>` : ''}
          <span class="tag">${fmtTime(c.ts)}</span>
        </div>
      </div>
    </div>`).join('') : EMPTY('📦', '还没有同步提交记录');
}

function renderNowCard() {
  const t = S.tasks.find(x => x.id === S.activeTask);
  $('#now-task').textContent = t ? t.title : '未选择任务';
}

/* ---------------- Markdown 极简渲染 ---------------- */
function md2html(md) {
  const lines = md.split('\n');
  let html = '', inList = false;
  const inline = s => esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
  for (const raw of lines) {
    const l = raw.trimEnd();
    if (/^###\s+/.test(l)) { if (inList) { html += '</ul>'; inList = false; } html += `<h3>${inline(l.replace(/^###\s+/, ''))}</h3>`; }
    else if (/^##\s+/.test(l)) { if (inList) { html += '</ul>'; inList = false; } html += `<h2>${inline(l.replace(/^##\s+/, ''))}</h2>`; }
    else if (/^#\s+/.test(l)) { if (inList) { html += '</ul>'; inList = false; } html += `<h2>${inline(l.replace(/^#\s+/, ''))}</h2>`; }
    else if (/^[-*]\s+/.test(l)) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${inline(l.replace(/^[-*]\s+/, ''))}</li>`; }
    else if (!l.trim()) { if (inList) { html += '</ul>'; inList = false; } }
    else { if (inList) { html += '</ul>'; inList = false; } html += `<p>${inline(l)}</p>`; }
  }
  if (inList) html += '</ul>';
  return html;
}

/* ---------------- 弹窗 ---------------- */
function modal(html) {
  $('#modal').innerHTML = html;
  $('#modal-bg').classList.add('on');
  const first = $('#modal input, #modal textarea');
  if (first) setTimeout(() => first.focus(), 40);
}
const closeModal = () => $('#modal-bg').classList.remove('on');

function taskModal(t) {
  const isNew = !t;
  t = t || { title: '', body: '', status: 'todo', priority: 2, project: '', tags: '', branch: '' };
  modal(`<h3>${isNew ? '新建任务' : '编辑任务'}</h3>
    <label class="field"><span>标题</span><input type="text" id="m-title" value="${esc(t.title)}"></label>
    <label class="field"><span>备注</span><textarea id="m-body">${esc(t.body)}</textarea></label>
    <div class="row" style="gap:10px">
      <label class="field" style="flex:1"><span>状态</span><select id="m-status">
        ${['todo', 'doing', 'blocked', 'done'].map(s => `<option value="${s}" ${t.status === s ? 'selected' : ''}>${ST_LABEL[s]}</option>`).join('')}
      </select></label>
      <label class="field" style="flex:1"><span>优先级</span><select id="m-pri">
        ${[1, 2, 3].map(p => `<option value="${p}" ${t.priority == p ? 'selected' : ''}>${P_LABEL[p]}</option>`).join('')}
      </select></label>
    </div>
    <div class="row" style="gap:10px">
      <label class="field" style="flex:1"><span>项目</span><input type="text" id="m-proj" value="${esc(t.project)}"></label>
      <label class="field" style="flex:1"><span>标签（逗号分隔）</span><input type="text" id="m-tags" value="${esc(t.tags)}"></label>
    </div>
    <label class="field"><span>关联分支（可选）</span><input type="text" id="m-branch" value="${esc(t.branch)}" placeholder="feature/login"></label>
    <div class="modal-foot">
      <button class="btn" data-act="close">取消</button>
      <button class="btn primary" data-act="save-task" data-id="${t.id || ''}">保存</button>
    </div>`);
}

function issueModal(i) {
  const isNew = !i;
  i = i || { title: '', detail: '', severity: 'normal', status: 'open', resolution: '', task_id: null };
  modal(`<h3>${isNew ? '记录问题' : '编辑问题'}</h3>
    <label class="field"><span>问题</span><input type="text" id="m-ititle" value="${esc(i.title)}"></label>
    <label class="field"><span>详情 / 报错内容</span><textarea id="m-idetail" style="min-height:110px;font-family:var(--mono);font-size:12px">${esc(i.detail)}</textarea></label>
    <div class="row" style="gap:10px">
      <label class="field" style="flex:1"><span>严重程度</span><select id="m-isev">
        ${['blocker', 'high', 'normal', 'low'].map(s => `<option value="${s}" ${i.severity === s ? 'selected' : ''}>${SEV_LABEL[s]}</option>`).join('')}
      </select></label>
      <label class="field" style="flex:1"><span>状态</span><select id="m-istatus">
        <option value="open" ${i.status === 'open' ? 'selected' : ''}>未解决</option>
        <option value="resolved" ${i.status === 'resolved' ? 'selected' : ''}>已解决</option>
        <option value="wontfix" ${i.status === 'wontfix' ? 'selected' : ''}>不修了</option>
      </select></label>
    </div>
    <label class="field"><span>关联任务</span><select id="m-itask">
      <option value="">（不关联）</option>
      ${S.tasks.map(t => `<option value="${t.id}" ${i.task_id == t.id ? 'selected' : ''}>${esc(t.title)}</option>`).join('')}
    </select></label>
    <label class="field"><span>解决方案</span><textarea id="m-ires" style="min-height:60px">${esc(i.resolution)}</textarea></label>
    <div class="modal-foot">
      <button class="btn" data-act="close">取消</button>
      <button class="btn primary" data-act="save-issue" data-id="${i.id || ''}">保存</button>
    </div>`);
}

/* ---------------- 页面切换 ---------------- */
const TITLES = {
  today: '当前任务', tasks: '任务清单', issues: '问题列表', logs: '日志分析',
  timeline: '时间线', shots: '图片附件', git: 'Git 关联', keys: '快捷键记录',
  report: 'AI 总结', settings: '设置',
};

function go(page) {
  S.page = page;
  $$('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + page));
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
  $('#page-title').textContent = TITLES[page] || page;
  if (page === 'settings') api('/api/ping').then(() => { });
}

/* ---------------- 计时器 ---------------- */
function tickTimer() {
  if (!S.timer.on) return;
  const sec = S.timer.acc + Math.floor((Date.now() - S.timer.start) / 1000);
  const m = String(Math.floor(sec / 60)).padStart(2, '0');
  const s = String(sec % 60).padStart(2, '0');
  $('#timer').textContent = `${m}:${s}`;
}
setInterval(tickTimer, 1000);

async function toggleTimer() {
  if (!S.activeTask) { toast('先选一个任务（点任务上的 🎯）', 'err'); return; }
  if (S.timer.on) {
    const sec = S.timer.acc + Math.floor((Date.now() - S.timer.start) / 1000);
    S.timer = { on: false, start: 0, acc: 0, tid: null };
    $('#timer-btn').textContent = '开始';
    $('#timer').textContent = '00:00';
    if (sec > 5) {
      await api('/api/tasks/focus', { id: S.activeTask, seconds: sec });
      toast(`已记录专注 ${dur(sec) || sec + '秒'}`, 'ok');
      await refresh();
    }
  } else {
    S.timer = { on: true, start: Date.now(), acc: 0, tid: S.activeTask };
    $('#timer-btn').textContent = '停止';
  }
}

/* ---------------- 命令面板 ---------------- */
const COMMANDS = [
  { t: '新建任务', h: 'Ctrl+N', run: () => taskModal(null) },
  { t: '记录问题', h: '', run: () => issueModal(null) },
  { t: '跳到：当前任务', h: '1', run: () => go('today') },
  { t: '跳到：任务清单', h: '2', run: () => go('tasks') },
  { t: '跳到：问题列表', h: '3', run: () => go('issues') },
  { t: '跳到：日志分析', h: 'Ctrl+L', run: () => go('logs') },
  { t: '跳到：时间线', h: '5', run: () => go('timeline') },
  { t: '跳到：图片附件', h: '6', run: () => go('shots') },
  { t: '跳到：Git 关联', h: '7', run: () => go('git') },
  { t: '跳到：快捷键', h: '8', run: () => go('keys') },
  { t: '生成今日日报', h: '', run: () => { go('report'); genReport(false); } },
  { t: '用大模型总结', h: '', run: () => { go('report'); genReport(true); } },
  { t: '同步 Git 提交', h: '', run: () => { go('git'); syncGit(); } },
  { t: '切换深色 / 浅色', h: '', run: toggleTheme },
];
let palSel = 0, palItems = [];

function openPalette() {
  $('#palette-bg').classList.add('on');
  $('#palette-input').value = '';
  fillPalette('');
  setTimeout(() => $('#palette-input').focus(), 30);
}
const closePalette = () => $('#palette-bg').classList.remove('on');

function fillPalette(q) {
  q = q.toLowerCase().trim();
  palItems = COMMANDS.filter(c => !q || c.t.toLowerCase().includes(q));
  if (q) {
    S.tasks.filter(t => t.title.toLowerCase().includes(q)).slice(0, 5).forEach(t =>
      palItems.push({ t: '📋 ' + t.title, h: '打开任务', run: () => { go('tasks'); taskModal(t); } }));
    S.issues.filter(i => i.title.toLowerCase().includes(q)).slice(0, 5).forEach(i =>
      palItems.push({ t: '🐛 ' + i.title, h: '打开问题', run: () => { go('issues'); issueModal(i); } }));
  }
  palSel = 0;
  drawPalette();
}
function drawPalette() {
  $('#palette-list').innerHTML = palItems.length ? palItems.map((c, i) =>
    `<div class="p-item ${i === palSel ? 'sel' : ''}" data-pi="${i}">
      <span>${esc(c.t)}</span><span class="p-hint">${esc(c.h)}</span></div>`).join('')
    : '<div class="empty">没有匹配的命令</div>';
}

/* ---------------- 主题 ---------------- */
function toggleTheme() {
  const cur = document.documentElement.dataset.theme;
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('devlog-theme', next);
}

/* ---------------- 业务动作 ---------------- */
async function genReport(useAI) {
  const days = Number($('#rp-days').value);
  $('#report-out').innerHTML = '<div class="empty"><span class="big">⏳</span>正在生成…</div>';
  try {
    const d = useAI
      ? await api('/api/summary/ai', { days })
      : await api(`/api/summary?days=${days}`);
    const s = d.summary;
    $('#report-out').innerHTML =
      (s.note ? `<div class="hint" style="margin-bottom:12px">${esc(s.note)}</div>` : '') +
      `<p style="color:var(--muted);font-size:12px;margin-bottom:6px">
        ${s.mode === 'ai' ? '🤖 ' + esc(s.model || '大模型') : '⚙️ 离线规则引擎'} · ${esc(s.headline)}</p>` +
      md2html(s.markdown);
    $('#report-out').dataset.md = s.markdown;
  } catch (e) {
    $('#report-out').innerHTML = `<div class="empty">生成失败：${esc(e.message)}</div>`;
  }
}

async function analyzeLog(save) {
  const text = $('#log-input').value;
  if (!text.trim()) { toast('先粘贴一些日志', 'err'); return; }
  try {
    const d = save
      ? await api('/api/logs/ingest', { text, task_id: S.activeTask })
      : await api('/api/logs/analyze', { text });
    $('#log-report').innerHTML = reportHTML(d.report);
    toast(`分析完成：${d.count} 行，${d.report.error_count} 条错误`, d.report.error_count ? 'err' : 'ok');
    if (save) await refresh();
  } catch (e) { toast('分析失败：' + e.message, 'err'); }
}

async function syncGit() {
  const repo = $('#git-repo').value.trim();
  if (!repo) { toast('请先填写仓库路径', 'err'); return; }
  toast('正在同步…');
  try {
    const d = await api('/api/git/sync', { repo });
    toast(`同步完成：${d.total} 条提交，新增 ${d.added}，关联任务 ${d.linked}`, 'ok');
    await refresh();
  } catch (e) { toast('同步失败：' + e.message, 'err'); }
}

async function uploadImage(file, note) {
  if (!file.type.startsWith('image/')) { toast('只支持图片', 'err'); return; }
  const b64 = await new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
  try {
    await api('/api/attachments/upload', {
      data: b64, filename: file.name || '截图.png',
      note: note || '', task_id: S.activeTask,
    });
    toast('图片已保存', 'ok');
    await refresh();
  } catch (e) { toast('上传失败：' + e.message, 'err'); }
}

/* ---------------- 事件绑定 ---------------- */
document.addEventListener('click', async (ev) => {
  const nav = ev.target.closest('.nav-item');
  if (nav) return go(nav.dataset.page);

  const gt = ev.target.closest('[data-goto]');
  if (gt) return go(gt.dataset.goto);

  const chip = ev.target.closest('.chip');
  if (chip) {
    const g = chip.parentElement;
    $$('.chip', g).forEach(c => c.classList.remove('on'));
    chip.classList.add('on');
    if (chip.dataset.f) { S.taskFilter = chip.dataset.f; renderTasks(); }
    if (chip.dataset.if) { S.issueFilter = chip.dataset.if; renderIssues(); }
    if (chip.dataset.lf) { S.logFilter = chip.dataset.lf; renderLogHistory(); }
    if (chip.dataset.tf) { S.tlFilter = chip.dataset.tf; renderTimeline(); }
    return;
  }

  const pi = ev.target.closest('[data-pi]');
  if (pi) { const c = palItems[Number(pi.dataset.pi)]; closePalette(); c && c.run(); return; }

  const shot = ev.target.closest('[data-shot]');
  if (shot) {
    $('#lightbox-img').src = '/files/' + encodeURIComponent(shot.dataset.shot);
    $('#lightbox').classList.add('on');
    return;
  }

  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act, id = Number(el.dataset.id);
  try {
    switch (act) {
      case 'close': closeModal(); break;
      case 'toggle': {
        const t = S.tasks.find(x => x.id === id);
        await api('/api/tasks/update', { id, status: t.status === 'done' ? 'todo' : 'done' });
        await refresh();
        break;
      }
      case 'activate':
        await api('/api/tasks/activate', { id });
        S.activeTask = id; renderAll();
        toast('已切换当前任务', 'ok');
        break;
      case 'edit': taskModal(S.tasks.find(x => x.id === id)); break;
      case 'del':
        if (confirm('确定删除这个任务？')) { await api('/api/tasks/delete', { id }); await refresh(); }
        break;
      case 'save-task': {
        const payload = {
          title: $('#m-title').value.trim(), body: $('#m-body').value,
          status: $('#m-status').value, priority: Number($('#m-pri').value),
          project: $('#m-proj').value, tags: $('#m-tags').value, branch: $('#m-branch').value,
        };
        if (!payload.title) { toast('标题不能为空', 'err'); return; }
        if (el.dataset.id) await api('/api/tasks/update', { id: Number(el.dataset.id), ...payload });
        else await api('/api/tasks/create', payload);
        closeModal(); await refresh(); toast('已保存', 'ok');
        break;
      }
      case 'i-toggle': {
        const i = S.issues.find(x => x.id === id);
        await api('/api/issues/update', { id, status: i.status === 'open' ? 'resolved' : 'open' });
        await refresh();
        break;
      }
      case 'i-edit': issueModal(S.issues.find(x => x.id === id)); break;
      case 'i-del':
        if (confirm('删除这个问题记录？')) { await api('/api/issues/delete', { id }); await refresh(); }
        break;
      case 'save-issue': {
        const payload = {
          title: $('#m-ititle').value.trim(), detail: $('#m-idetail').value,
          severity: $('#m-isev').value, status: $('#m-istatus').value,
          resolution: $('#m-ires').value,
          task_id: $('#m-itask').value ? Number($('#m-itask').value) : null,
        };
        if (!payload.title) { toast('请填写问题标题', 'err'); return; }
        if (el.dataset.id) await api('/api/issues/update', { id: Number(el.dataset.id), ...payload });
        else await api('/api/issues/create', payload);
        closeModal(); await refresh(); toast('已保存', 'ok');
        break;
      }
      case 'sc-hit': await api('/api/shortcuts/hit', { id }); await refresh(); break;
      case 'sc-del': await api('/api/shortcuts/delete', { id }); await refresh(); break;
      case 'log2issue':
        await api('/api/logs/to-issue', {
          title: el.dataset.msg.slice(0, 150), detail: el.dataset.raw,
          severity: 'high', task_id: S.activeTask,
        });
        toast('已存为问题', 'ok'); await refresh();
        break;
    }
  } catch (e) { toast('操作失败：' + e.message, 'err'); }
});

$('#modal-bg').addEventListener('click', e => { if (e.target.id === 'modal-bg') closeModal(); });
$('#lightbox').addEventListener('click', () => $('#lightbox').classList.remove('on'));
$('#palette-bg').addEventListener('click', e => { if (e.target.id === 'palette-bg') closePalette(); });
$('#quick-add').addEventListener('click', () => taskModal(null));
$('#issue-add').addEventListener('click', () => issueModal(null));
$('#theme-btn').addEventListener('click', toggleTheme);
$('#timer-btn').addEventListener('click', toggleTimer);
$('#log-analyze').addEventListener('click', () => analyzeLog(true));
$('#log-preview').addEventListener('click', () => analyzeLog(false));
$('#git-sync').addEventListener('click', syncGit);
$('#rp-gen').addEventListener('click', () => genReport(false));
$('#rp-ai').addEventListener('click', () => genReport(true));

$('#log-clear').addEventListener('click', async () => {
  if (!confirm('清空所有历史日志？')) return;
  await api('/api/logs/clear', {}); await refresh(); toast('已清空', 'ok');
});
$('#log-load').addEventListener('click', async () => {
  const path = $('#log-file').value.trim();
  if (!path) { toast('请输入文件路径', 'err'); return; }
  try {
    const d = await api('/api/logs/ingest-file', { path });
    $('#log-report').innerHTML = reportHTML(d.report);
    toast(`读取 ${d.count} 行`, 'ok'); await refresh();
  } catch (e) { toast(e.message, 'err'); }
});

$('#note-save').addEventListener('click', async () => {
  const text = $('#quick-note').value.trim();
  if (!text) return;
  await api('/api/notes/create', { text });
  $('#quick-note').value = ''; await refresh(); toast('已记入时间线', 'ok');
});
$('#note-issue').addEventListener('click', async () => {
  const text = $('#quick-note').value.trim();
  if (!text) return;
  await api('/api/issues/create', {
    title: text.split('\n')[0].slice(0, 150), detail: text,
    severity: 'normal', task_id: S.activeTask,
  });
  $('#quick-note').value = ''; await refresh(); toast('已存为问题', 'ok');
});
$('#quick-note').addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); $('#note-save').click(); }
});

$('#rp-copy').addEventListener('click', async () => {
  const md = $('#report-out').dataset.md;
  if (!md) { toast('还没有内容', 'err'); return; }
  try { await navigator.clipboard.writeText(md); toast('已复制到剪贴板', 'ok'); }
  catch { toast('复制失败，请手动选中', 'err'); }
});
$('#rp-export').addEventListener('click', () => {
  window.location.href = `/api/export?days=${$('#rp-days').value}`;
});

$('#set-save').addEventListener('click', async () => {
  const payload = {
    ai_base_url: $('#set-base').value.trim(),
    ai_model: $('#set-model').value.trim(),
  };
  const k = $('#set-key').value.trim();
  if (k) payload.ai_api_key = k;
  await api('/api/settings', payload);
  $('#set-key').value = '';
  toast('设置已保存', 'ok');
});

/* 快捷键捕获 */
$('#sc-keys').addEventListener('keydown', e => {
  if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) return;
  e.preventDefault();
  const parts = [];
  if (e.ctrlKey) parts.push('Ctrl');
  if (e.metaKey) parts.push('Cmd');
  if (e.altKey) parts.push('Alt');
  if (e.shiftKey) parts.push('Shift');
  parts.push(e.key.length === 1 ? e.key.toUpperCase() : e.key);
  e.target.value = parts.join('+');
  $('#sc-desc').focus();
});
$('#sc-add').addEventListener('click', async () => {
  const keys = $('#sc-keys').value.trim();
  if (!keys) { toast('请先按下组合键', 'err'); return; }
  await api('/api/shortcuts/create', {
    keys, description: $('#sc-desc').value.trim(), app: $('#sc-app').value.trim(),
  });
  $('#sc-keys').value = ''; $('#sc-desc').value = '';
  await refresh(); toast('已记录', 'ok');
});

/* 搜索 */
$('#global-search').addEventListener('input', e => {
  S.search = e.target.value.trim();
  renderTasks(); renderIssues(); renderTimeline();
});

/* 图片：拖拽 / 选择 / 粘贴 */
const dz = $('#dropzone');
dz.addEventListener('click', () => $('#file-input').click());
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('hot'); });
dz.addEventListener('dragleave', () => dz.classList.remove('hot'));
dz.addEventListener('drop', async e => {
  e.preventDefault(); dz.classList.remove('hot');
  for (const f of e.dataTransfer.files) await uploadImage(f);
});
$('#file-input').addEventListener('change', async e => {
  for (const f of e.target.files) await uploadImage(f);
  e.target.value = '';
});
document.addEventListener('paste', async e => {
  const items = [...(e.clipboardData?.items || [])];
  const img = items.find(i => i.type.startsWith('image/'));
  if (!img) return;
  const file = img.getAsFile();
  if (file) { await uploadImage(file, '粘贴的截图'); go('shots'); }
});

/* 全局键盘 */
document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);

  if (e.key === 'Escape') {
    closeModal(); closePalette();
    $('#lightbox').classList.remove('on');
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') { e.preventDefault(); taskModal(null); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'l') { e.preventDefault(); go('logs'); return; }

  if ($('#palette-bg').classList.contains('on')) {
    if (e.key === 'ArrowDown') { e.preventDefault(); palSel = Math.min(palSel + 1, palItems.length - 1); drawPalette(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); palSel = Math.max(palSel - 1, 0); drawPalette(); }
    if (e.key === 'Enter') { e.preventDefault(); const c = palItems[palSel]; closePalette(); c && c.run(); }
    return;
  }

  if (!typing && /^[1-9]$/.test(e.key)) {
    const pages = ['today', 'tasks', 'issues', 'logs', 'timeline', 'shots', 'git', 'keys', 'report'];
    const p = pages[Number(e.key) - 1];
    if (p) go(p);
  }
});
$('#palette-input').addEventListener('input', e => fillPalette(e.target.value));

/* 启动 */
document.documentElement.dataset.theme = localStorage.getItem('devlog-theme') || 'dark';
boot();
setInterval(() => { if (!$('#modal-bg').classList.contains('on')) refresh().catch(() => { }); }, 45000);
