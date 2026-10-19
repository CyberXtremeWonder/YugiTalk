// 本文件只切换设计草图的视图与交互，不会伪造真实任务执行结果。
const sidebar = document.getElementById('sidebar');
const menuButton = document.getElementById('menu');
const inspector = document.getElementById('inspector');
const panelButton = document.getElementById('panel');
const promptInput = document.getElementById('prompt');
const mobileViewport = window.matchMedia('(max-width: 640px)');
let previousFocus = null;

// 区分鼠标和键盘操作，避免点击输入框产生黑边，同时保留键盘焦点提示。
document.addEventListener('pointerdown', () => { document.documentElement.dataset.focusInput = 'pointer'; });
document.addEventListener('keydown', (event) => {
  if (event.key === 'Tab') document.documentElement.dataset.focusInput = 'keyboard';
});

const views = {
  chat: { title: '对话空间' },
  agents: { title: 'Agent', symbol: '✳', description: '为不同任务，选择合适的智能助手。', empty: '尚未创建 Agent', hint: '后续在这里配置模型、工具、提示词和知识库。' },
  knowledge: { title: '知识库', symbol: '▤', description: '把你的资料，变成有据可循的答案。', empty: '知识库还是空的', hint: '后续在这里上传文档、管理资料和查看索引状态。' },
  tasks: { title: '任务记录', symbol: '✓', description: '执行过程与结果，在这里清晰呈现。', empty: '尚无任务记录', hint: '任务运行后，可在这里查看状态、恢复流程与结果。' },
  settings: { title: '设置', symbol: '⚙', description: '管理你的工作空间与偏好。', empty: '个人工作空间', hint: '模型、授权工具、成员与预算配置将在后续阶段提供。' },
};

function setMenu(open) {
  sidebar.classList.toggle('open', open);
  menuButton.setAttribute('aria-expanded', String(open));
}

function showView(key) {
  const view = views[key] || views.chat;
  document.getElementById('page-title').textContent = view.title;
  document.getElementById('chat-view').hidden = key !== 'chat';
  document.getElementById('secondary-view').hidden = key === 'chat';
  document.querySelectorAll('[data-view]').forEach((button) => {
    if (button.dataset.view === key) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  if (key !== 'chat') {
    document.getElementById('view-title').textContent = view.title;
    document.getElementById('view-symbol').textContent = view.symbol;
    document.getElementById('view-description').textContent = view.description;
    document.getElementById('empty-title').textContent = view.empty;
    document.getElementById('empty-description').textContent = view.hint;
  }
  window.history.replaceState(null, '', `#${key}`);
  setMenu(false);
}

document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => showView(button.dataset.view)));
document.querySelectorAll('.brand,.mobile-brand').forEach((link) => link.addEventListener('click', (event) => { event.preventDefault(); showView('chat'); }));
document.getElementById('new-chat').addEventListener('click', () => { showView('chat'); promptInput.value = ''; promptInput.focus(); });
document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => { promptInput.value = button.dataset.prompt; promptInput.focus(); }));
menuButton.addEventListener('click', () => setMenu(!sidebar.classList.contains('open')));
document.getElementById('theme').addEventListener('click', (event) => {
  const light = document.documentElement.dataset.theme === 'dark';
  document.documentElement.dataset.theme = light ? 'light' : 'dark';
  document.getElementById('theme-label').textContent = light ? '深色主题' : '浅色主题';
  event.currentTarget.setAttribute('aria-label', light ? '切换到深色主题' : '切换到浅色主题');
});

function setPanel(open) {
  inspector.hidden = !open;
  document.getElementById('backdrop').hidden = !open;
  panelButton.setAttribute('aria-expanded', String(open));
  document.getElementById('panel-label').textContent = open ? '收起详情' : '展开详情';
  if (open) {
    previousFocus = document.activeElement;
    inspector.setAttribute('role', 'dialog');
    inspector.setAttribute('aria-modal', 'true');
    sidebar.inert = true;
    document.getElementById('main-panel').inert = true;
    document.getElementById('close-panel').focus();
  } else {
    inspector.removeAttribute('role');
    inspector.removeAttribute('aria-modal');
    sidebar.inert = false;
    document.getElementById('main-panel').inert = false;
    if (previousFocus) previousFocus.focus();
    previousFocus = null;
  }
}

panelButton.addEventListener('click', () => setPanel(inspector.hidden));
document.getElementById('close-panel').addEventListener('click', () => setPanel(false));
document.getElementById('backdrop').addEventListener('click', () => setPanel(false));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') { if (!inspector.hidden) setPanel(false); else setMenu(false); }
  if (event.key === 'Tab' && !inspector.hidden) {
    // 抽屉打开时约束焦点，关闭后回到左侧触发按钮。
    const items = [...inspector.querySelectorAll('a[href],button')];
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
mobileViewport.addEventListener('change', () => { setPanel(false); setMenu(false); });
showView(window.location.hash.slice(1) in views ? window.location.hash.slice(1) : 'chat');
