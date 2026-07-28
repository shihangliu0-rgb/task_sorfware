const vscode = require('vscode');
const http = require('http');
const { URL } = require('url');

function base() {
  return vscode.workspace.getConfiguration('devlog').get('serverUrl', 'http://127.0.0.1:8765');
}

function post(path, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(path, base());
    const data = Buffer.from(JSON.stringify(body || {}), 'utf8');
    const req = http.request(
      { hostname: u.hostname, port: u.port || 80, path: u.pathname + u.search,
        method: 'POST', timeout: 8000,
        headers: { 'Content-Type': 'application/json', 'Content-Length': data.length } },
      (res) => {
        let out = '';
        res.on('data', (c) => (out += c));
        res.on('end', () => {
          try { resolve(JSON.parse(out)); } catch (e) { reject(new Error('返回内容异常')); }
        });
      }
    );
    req.on('error', () => reject(new Error('连接不上 DevLog，请先启动桌面应用')));
    req.on('timeout', () => { req.destroy(); reject(new Error('请求超时')); });
    req.write(data);
    req.end();
  });
}

function get(path) {
  return new Promise((resolve, reject) => {
    const u = new URL(path, base());
    http.get({ hostname: u.hostname, port: u.port || 80, path: u.pathname + u.search, timeout: 8000 },
      (res) => {
        let out = '';
        res.on('data', (c) => (out += c));
        res.on('end', () => {
          try { resolve(JSON.parse(out)); } catch (e) { reject(new Error('返回内容异常')); }
        });
      }).on('error', () => reject(new Error('连接不上 DevLog，请先启动桌面应用')));
  });
}

function selection() {
  const ed = vscode.window.activeTextEditor;
  if (!ed) return '';
  return ed.document.getText(ed.selection);
}

function activate(context) {
  const bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  bar.command = 'devlog.showTasks';
  bar.text = '$(checklist) DevLog';
  bar.tooltip = '点击查看未完成任务';
  bar.show();
  context.subscriptions.push(bar);

  async function updateBar() {
    try {
      const d = await get('/api/stats');
      const s = d.stats || {};
      const n = (s.tasks_doing || 0) + (s.tasks_todo || 0);
      bar.text = `$(checklist) ${n} 待办` + (s.issues_open ? ` $(bug) ${s.issues_open}` : '');
      bar.tooltip = `进行中 ${s.tasks_doing || 0} · 待办 ${s.tasks_todo || 0} · 未解决问题 ${s.issues_open || 0}`;
    } catch (e) {
      bar.text = '$(circle-slash) DevLog 未启动';
    }
  }
  updateBar();
  const timer = setInterval(updateBar, 30000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  const reg = (name, fn) =>
    context.subscriptions.push(vscode.commands.registerCommand(name, async () => {
      try { await fn(); await updateBar(); }
      catch (e) { vscode.window.showErrorMessage('DevLog: ' + e.message); }
    }));

  reg('devlog.newTask', async () => {
    const title = await vscode.window.showInputBox({ prompt: '任务标题' });
    if (!title) return;
    const pick = await vscode.window.showQuickPick(
      [{ label: '高', v: 1 }, { label: '中', v: 2 }, { label: '低', v: 3 }],
      { placeHolder: '优先级' });
    let branch = '';
    try {
      const git = vscode.extensions.getExtension('vscode.git');
      if (git && git.isActive) {
        const repo = git.exports.getAPI(1).repositories[0];
        branch = (repo && repo.state.HEAD && repo.state.HEAD.name) || '';
      }
    } catch (e) { /* 忽略 */ }
    await post('/api/tasks/create', {
      title, priority: pick ? pick.v : 2, branch,
      project: vscode.workspace.name || '',
    });
    vscode.window.showInformationMessage(`DevLog: 已创建「${title}」`);
  });

  reg('devlog.saveSelectionAsIssue', async () => {
    const text = selection();
    if (!text) { vscode.window.showWarningMessage('请先选中内容'); return; }
    const title = await vscode.window.showInputBox({
      prompt: '问题标题', value: text.split('\n')[0].slice(0, 100),
    });
    if (!title) return;
    const ed = vscode.window.activeTextEditor;
    const where = ed ? `\n\n— ${ed.document.fileName}:${ed.selection.start.line + 1}` : '';
    await post('/api/issues/create', { title, detail: text + where, severity: 'high' });
    vscode.window.showInformationMessage('DevLog: 已存为问题');
  });

  reg('devlog.analyzeSelection', async () => {
    const text = selection();
    if (!text) { vscode.window.showWarningMessage('请先选中日志内容'); return; }
    const d = await post('/api/logs/ingest', { text });
    const r = d.report;
    const lines = [`共 ${d.count} 行 · ${r.error_count} 条错误 · ${r.warn_count} 条警告`, '', r.headline];
    if (r.hints && r.hints.length) {
      lines.push('', '排查建议：');
      r.hints.forEach((h) => lines.push(`• ${h.name} — ${h.advice}`));
    }
    if (r.clusters && r.clusters.length) {
      lines.push('', '同类问题：');
      r.clusters.slice(0, 6).forEach((c) => lines.push(`  ×${c.count} [${c.level}] ${c.sample.slice(0, 100)}`));
    }
    const doc = await vscode.workspace.openTextDocument({
      content: lines.join('\n'), language: 'markdown',
    });
    vscode.window.showTextDocument(doc, { preview: true, viewColumn: vscode.ViewColumn.Beside });
  });

  reg('devlog.quickNote', async () => {
    const text = await vscode.window.showInputBox({ prompt: '记一笔（会进时间线）' });
    if (!text) return;
    await post('/api/notes/create', { text });
    vscode.window.showInformationMessage('DevLog: 已记录');
  });

  reg('devlog.syncGit', async () => {
    const f = vscode.workspace.workspaceFolders;
    if (!f || !f.length) { vscode.window.showWarningMessage('没有打开的工作区'); return; }
    const d = await post('/api/git/sync', { repo: f[0].uri.fsPath });
    vscode.window.showInformationMessage(
      `DevLog: 同步 ${d.total} 条提交，新增 ${d.added}，关联任务 ${d.linked}`);
  });

  reg('devlog.showTasks', async () => {
    const d = await get('/api/tasks');
    const items = (d.items || []).filter((t) => t.status !== 'done');
    if (!items.length) { vscode.window.showInformationMessage('DevLog: 没有未完成任务 🎉'); return; }
    const LBL = { todo: '待办', doing: '进行中', blocked: '受阻' };
    const pick = await vscode.window.showQuickPick(
      items.map((t) => ({
        label: `${t.status === 'doing' ? '$(play)' : t.status === 'blocked' ? '$(error)' : '$(circle-outline)'} ${t.title}`,
        description: `${LBL[t.status] || t.status} · ${['', '高', '中', '低'][t.priority]}优先`,
        detail: t.body ? t.body.slice(0, 120) : undefined,
        id: t.id,
      })),
      { placeHolder: '选一个任务标记为当前 / 完成' });
    if (!pick) return;
    const act = await vscode.window.showQuickPick(['设为当前任务', '标记完成', '取消'], { placeHolder: '要做什么？' });
    if (act === '设为当前任务') {
      await post('/api/tasks/activate', { id: pick.id });
      vscode.window.showInformationMessage('DevLog: 已切换当前任务');
    } else if (act === '标记完成') {
      await post('/api/tasks/update', { id: pick.id, status: 'done' });
      vscode.window.showInformationMessage('DevLog: 任务已完成 ✅');
    }
  });

  reg('devlog.openApp', async () => {
    vscode.env.openExternal(vscode.Uri.parse(base()));
  });
}

function deactivate() {}
module.exports = { activate, deactivate };
