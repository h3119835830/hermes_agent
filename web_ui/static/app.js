/**
 * Hermes Agent Web UI — Frontend Logic
 * Handles: theme switching, sessions, SSE streaming, file upload, memory polling
 */

'use strict';

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  currentSessionId: null,
  isStreaming: false,
  attachment: null,          // { name, content, is_text }
  activeMemoryTab: 'MEMORY.md',
  memoryData: { 'MEMORY.md': '', 'USER.md': '' },
  theme: localStorage.getItem('hermes-theme') || 'gold',
};

// ── DOM refs ────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const messagesContainer = $('messages-container');
const messageInput      = $('message-input');
const sendBtn           = $('send-btn');
const stopBtn           = $('stop-btn');
const sessionList       = $('session-list');
const newSessionBtn     = $('new-session-btn');
const fileInput         = $('file-input');
const attachPreview     = $('attachment-preview');
const attachName        = $('attach-name');
const attachRemove      = $('attach-remove');
const statusDot         = $('status-dot');
const statusText        = $('status-text');
const memoryContent     = $('memory-content');
const thinkingTpl       = $('thinking-tpl');

// ── Init ────────────────────────────────────────────────────────────────────
async function init() {
  applyTheme(state.theme, false);
  bindEvents();
  setStatus('connecting');
  await loadSessions();
  startMemoryPolling();
}

// ── Theme ────────────────────────────────────────────────────────────────────
function applyTheme(theme, animate = true) {
  state.theme = theme;
  localStorage.setItem('hermes-theme', theme);
  document.body.className = `theme-${theme}`;
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === theme);
  });
}

// ── Status ───────────────────────────────────────────────────────────────────
function setStatus(status, text) {
  statusDot.className = 'status-dot';
  if (status === 'connecting') {
    statusDot.classList.add('connecting');
    statusText.textContent = '连接中…';
  } else if (status === 'ok') {
    statusText.textContent = text || '已连接';
  } else if (status === 'streaming') {
    statusText.textContent = 'Agent 思考中…';
  } else if (status === 'error') {
    statusDot.classList.add('error');
    statusText.textContent = '连接失败';
  }
}

// ── Session Management ───────────────────────────────────────────────────────
async function loadSessions() {
  try {
    const res  = await fetch('/api/sessions');
    const list = await res.json();
    renderSessionList(list);

    if (list.length > 0) {
      await selectSession(list[0].id);
      restoreTokenFromSession(list[0].id, list);
      setStatus('ok', '已连接');
    } else {
      await createNewSession();
    }
  } catch (e) {
    setStatus('error');
    console.error('Failed to load sessions:', e);
  }
}

// 全局存储完整会话列表（用于「更多」弹框）
let _allSessions = [];

function renderSessionList(list) {
  _allSessions = list;
  sessionList.innerHTML = '';

  const visible = list.slice(0, 5);
  const rest    = list.slice(5);

  visible.forEach(s => {
    sessionList.appendChild(buildSessionItem(s, list));
  });

  // 更多会话按钮
  const moreBtn = $('more-sessions-btn');
  if (rest.length > 0) {
    moreBtn.textContent = `··· 还有 ${rest.length} 条会话`;
    moreBtn.classList.remove('hidden');
  } else {
    moreBtn.classList.add('hidden');
  }
}

function buildSessionItem(s, allList) {
  const item = document.createElement('div');
  item.className = 'session-item';
  item.dataset.id = s.id;
  item.setAttribute('role', 'listitem');
  const title = s.title || '未命名会话';
  const msgCount = s.message_count || 0;
  const lastMsg = s.last_message ? ' · ' + escHtml(s.last_message) : '';
  item.innerHTML = `
    <div class="session-info">
      <div class="session-title">${escHtml(title)}</div>
      <div class="session-meta">${msgCount} 条消息${lastMsg}</div>
    </div>
    <div class="session-actions">
      <button class="delete-session-btn" title="删除会话" onclick="event.stopPropagation(); deleteSession('${s.id}')">✕</button>
    </div>
  `;
  item.addEventListener('click', (e) => {
    e.stopPropagation();
    selectSession(s.id);
    restoreTokenFromSession(s.id, allList || _allSessions);
    // 如果是从「更多」弹框点击的，关闭它
    const popup = document.getElementById('more-sessions-popup');
    if (popup) popup.classList.add('hidden');
  });
  return item;
}

async function deleteSession(sessionId) {
  if (!confirm('确定要删除此会话吗？')) return;
  try {
    await fetch('/api/sessions/' + sessionId, { method: 'DELETE' });
    if (state.currentSessionId === sessionId) {
      state.currentSessionId = null;
      messagesContainer.innerHTML = '';
      showWelcome();
    }
    loadSessions(); // 重新加载列表
  } catch (e) {
    console.error('Failed to delete session:', e);
  }
}


async function selectSession(sessionId) {
  state.currentSessionId = sessionId;

  // Highlight active
  document.querySelectorAll('.session-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === sessionId);
  });

  // Load messages
  try {
    const res  = await fetch(`/api/sessions/${sessionId}/messages`);
    const msgs = await res.json();

    // Clear chat
    messagesContainer.innerHTML = '';

    if (msgs.length === 0) {
      showWelcome();
    } else {
      msgs.forEach(m => addMessage(m.role, m.content, m.attachment, false));
      scrollToBottom();
    }
    setStatus('ok', '已连接');

    // 切换会话后立刻刷新该会话的专属记忆
    fetchMemories();
  } catch (e) {
    console.error('Failed to load messages:', e);
  }
}

// 从会话列表数据中恢复 token 显示
function restoreTokenFromSession(sessionId, sessionList) {
  const s = sessionList.find(s => s.id === sessionId);
  if (s && s.token_total) {
    updateTokenDisplay(s.token_total, s.token_in, s.token_out);
  } else {
    // 没有历史则重置
    const countEl = document.getElementById('token-count');
    const boxEl   = document.getElementById('token-box');
    if (countEl) countEl.textContent = '0';
    if (boxEl) { boxEl.classList.remove('active'); boxEl.title = '本次会话累计消耗 Token 数'; }
  }
}

async function createNewSession() {
  try {
    const res  = await fetch('/api/sessions/new', { method: 'POST' });
    const data = await res.json();
    state.currentSessionId = data.session_id;

    // Reload session list and select new
    const listRes  = await fetch('/api/sessions');
    const list     = await listRes.json();
    renderSessionList(list);

    // Select
    document.querySelectorAll('.session-item').forEach(el => {
      el.classList.toggle('active', el.dataset.id === state.currentSessionId);
    });

    messagesContainer.innerHTML = '';
    showWelcome();
    messageInput.focus();
  } catch (e) {
    console.error('Failed to create session:', e);
  }
}

// ── Messages ─────────────────────────────────────────────────────────────────
function showWelcome() {
  messagesContainer.innerHTML = `
    <div id="welcome-screen">
      <div class="welcome-icon">⚕</div>
      <h1 class="welcome-title">Hermes Agent</h1>
      <p class="welcome-sub">由 DeepSeek 驱动 · 支持文件上传 · 自动生成记忆</p>
      <div class="welcome-tips">
        <div class="tip-card">💬 发送任意消息开始对话</div>
        <div class="tip-card">📎 点击附件按钮上传文件</div>
        <div class="tip-card">🧠 右侧面板查看 Agent 记忆</div>
      </div>
    </div>`;
}

function addMessage(role, content, attachmentName, animate = true) {
  // Remove welcome screen if present
  const welcome = $('welcome-screen');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = `message ${role}`;

  const avatar = role === 'user'
    ? '<div class="avatar">👤</div>'
    : '<div class="avatar">⚕</div>';

  const attachChip = attachmentName
    ? `<div class="attach-chip">📎 ${escHtml(attachmentName)}</div>`
    : '';

  const actions = role === 'assistant'
    ? `<div class="message-actions">
         <button class="action-btn" onclick="regenerateLast()" title="重新生成此回复">🔄 重新生成</button>
       </div>`
    : '';

  div.innerHTML = `
    ${avatar}
    <div class="bubble-wrapper">
      <div class="bubble">
        ${attachChip}
        <div class="bubble-text">${renderMarkdown(content)}</div>
      </div>
      ${actions}
    </div>`;

  messagesContainer.appendChild(div);
  if (animate) scrollToBottom();
  return div;
}

function addThinkingIndicator() {
  const clone = thinkingTpl.content.cloneNode(true);
  messagesContainer.appendChild(clone);
  scrollToBottom();
  return $('thinking-indicator');
}

function scrollToBottom() {
  messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// ── SSE Event Helpers ─────────────────────────────────────────────────────────

// 工具调用追踪映射 { toolName -> badgeElement }
const activeToolBadges = {};

function handleSseEvent(evt, ctx) {
  // ctx = { thinkEl, getBubble, appendBubbleText, bubbleWrapper }
  if (evt.type === 'heartbeat') {
    // still waiting

  } else if (evt.type === 'status') {
    const statusEl = document.querySelector('#thinking-indicator .thinking-status');
    if (statusEl) statusEl.textContent = evt.content;

  } else if (evt.type === 'tool_start') {
    // 在思考气泡下插入工具调用 badge（不移除 thinking indicator）
    const badge = document.createElement('div');
    badge.className = 'tool-call-badge running';
    badge.dataset.tool = evt.name;
    badge.innerHTML = '<span class="tool-spinner"></span>' + escHtml(evt.label || evt.name);
    // 插入到 thinking indicator 之前（保持顺序）
    const thinkEl = $('thinking-indicator');
    if (thinkEl) {
      thinkEl.parentNode.insertBefore(badge, thinkEl);
    } else {
      messagesContainer.appendChild(badge);
    }
    activeToolBadges[evt.name] = badge;
    scrollToBottom();

  } else if (evt.type === 'tool_done') {
    const badge = activeToolBadges[evt.name];
    if (badge) {
      badge.classList.remove('running');
      badge.classList.add('done');
      const spinner = badge.querySelector('.tool-spinner');
      if (spinner) spinner.outerHTML = '✅ ';
      
      // 不再淡出移除，保留在聊天记录中以供查看
      delete activeToolBadges[evt.name];
    }

  } else if (evt.type === 'reasoning') {
    // 创建或更新思维链折叠面板
    let box = ctx.reasoningBox;
    if (!box) {
      box = document.createElement('details');
      box.className = 'reasoning-box';
      box.setAttribute('open', '');
      box.innerHTML = '<summary>💭 深度思考中…</summary><div class="reasoning-content"></div>';
      const thinkEl = $('thinking-indicator');
      if (thinkEl) {
        thinkEl.parentNode.insertBefore(box, thinkEl);
      } else {
        messagesContainer.appendChild(box);
      }
      ctx.reasoningBox = box;
    }
    const content = box.querySelector('.reasoning-content');
    if (content) {
      content.textContent += evt.text;
      content.scrollTop = content.scrollHeight;
    }
    scrollToBottom();

  } else if (evt.type === 'chunk') {
    // 思维链完成 — 锁定折叠
    if (ctx.reasoningBox) {
      ctx.reasoningBox.removeAttribute('open');
      const summary = ctx.reasoningBox.querySelector('summary');
      if (summary) summary.textContent = '💭 已完成思考（点击展开）';
    }
    // Remove thinking indicator and create AI bubble on first chunk
    const thinkEl = $('thinking-indicator');
    if (thinkEl && thinkEl.parentNode) thinkEl.remove();
    ctx.appendBubbleText(evt.content);
    scrollToBottom();

  } else if (evt.type === 'done') {
    // Update token usage
    if (evt.token_total !== undefined) {
      updateTokenDisplay(evt.token_total, evt.token_in, evt.token_out);
    }
    // Update session title in sidebar
    if (evt.session_title) {
      const activeItem = document.querySelector('.session-item.active .session-title');
      if (activeItem) activeItem.textContent = evt.session_title;
    }

  } else if (evt.type === 'error') {
    const thinkEl = $('thinking-indicator');
    if (thinkEl && thinkEl.parentNode) thinkEl.remove();
    addMessage('assistant', '⚠️ 错误：' + (evt.content || '未知错误'));
  }
}

function updateTokenDisplay(total, inTok, outTok) {
  const countEl = document.getElementById('token-count');
  const boxEl   = document.getElementById('token-box');
  if (!countEl) return;
  const fmt = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
  countEl.textContent = fmt(total || 0);
  if (boxEl) {
    boxEl.classList.add('active');
    const tip = `本次会话：输入 ${inTok || 0} + 输出 ${outTok || 0} = ${total || 0} tokens`;
    boxEl.title = tip;
  }
}

// ── Send Message (SSE streaming) ─────────────────────────────────────────────
async function sendMessage() {
  const text = messageInput.value.trim();
  if (!text && !state.attachment) return;
  if (state.isStreaming) return;
  if (!state.currentSessionId) return;

  state.isStreaming = true;
  sendBtn.style.display = 'none';
  stopBtn.style.display = 'flex';
  stopBtn.disabled = false;
  setStatus('streaming');

  // Display user message
  addMessage('user', text || '（附件）', state.attachment?.name);
  messageInput.value = '';
  autoResizeTextarea();

  // Capture & clear attachment
  const attachment = state.attachment;
  clearAttachment();

  // Clear stale tool badges
  Object.keys(activeToolBadges).forEach(k => delete activeToolBadges[k]);

  // Show thinking indicator
  const thinkEl = addThinkingIndicator();
  const webSearch = document.getElementById('web-search-toggle')?.checked || false;
  const deepThinking = document.getElementById('deep-thinking-toggle')?.checked ?? true;

  // SSE fetch
  let aiBubbleEl = null;
  let aiBubbleText = '';

  const ctx = {
    thinkEl,
    reasoningBox: null,
    appendBubbleText(chunk) {
      if (!aiBubbleEl) {
        aiBubbleEl = addMessage('assistant', '', null, true);
      }
      aiBubbleText += chunk;
      const textDiv = aiBubbleEl.querySelector('.bubble-text');
      if (textDiv) textDiv.innerHTML = renderMarkdown(aiBubbleText);
    }
  };

  try {
    const body = JSON.stringify({
      message:    text,
      session_id: state.currentSessionId,
      attachment: attachment,
      web_search: webSearch,
      deep_thinking: deepThinking
    });

    const response = await fetch('/api/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });

    const reader  = response.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';
    let   streamDone = false;

    while (!streamDone) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const evt = JSON.parse(line.slice(6));
          if (evt.type === 'done') {
            handleSseEvent(evt, ctx);
            streamDone = true;
            // Refresh session list
            const listRes = await fetch('/api/sessions');
            renderSessionList(await listRes.json());
            document.querySelectorAll('.session-item').forEach(el => {
              el.classList.toggle('active', el.dataset.id === state.currentSessionId);
            });
            break; // 跳出 for 循环，while 条件为 false 自动退出
          } else {
            handleSseEvent(evt, ctx);
          }
        } catch (_) { /* ignore JSON parse errors */ }
      }
    }
  } catch (e) {
    const thinkElNow = $('thinking-indicator');
    if (thinkElNow && thinkElNow.parentNode) thinkElNow.remove();
    addMessage('assistant', '⚠️ 网络错误：' + e.message);
  } finally {
    state.isStreaming = false;
    sendBtn.style.display = 'flex';
    stopBtn.style.display = 'none';
    setStatus('ok', '已连接');
    messageInput.focus();
  }
}


// ── Regenerate ───────────────────────────────────────────────────────────────
async function regenerateLast() {
  if (state.isStreaming || !state.currentSessionId) return;

  // 1. Remove the last assistant message from UI
  const messages = Array.from(messagesContainer.querySelectorAll('.message'));
  const lastMessage = messages[messages.length - 1];
  if (!lastMessage || !lastMessage.classList.contains('assistant')) {
    return;
  }
  lastMessage.remove();

  state.isStreaming = true;
  sendBtn.disabled = true;
  setStatus('streaming');

  // Show thinking indicator
  const thinkEl = addThinkingIndicator();
  const webSearch = document.getElementById('web-search-toggle')?.checked || false;

  try {
    const response = await fetch('/api/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.currentSessionId,
        regenerate: true,
        web_search: webSearch
      }),
    });

    const reader  = response.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';
    let   aiBubbleEl = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split('\\n\\n');
      buffer = parts.pop() || '';

      for (const part of parts) {
        if (!part.startsWith('data: ')) continue;
        const jsonStr = part.substring(6).trim();
        if (!jsonStr) continue;

        try {
          const evt = JSON.parse(jsonStr);

          if (evt.type === 'heartbeat') {
            // still thinking
          } else if (evt.type === 'status') {
            const statusEl = document.querySelector('#thinking-indicator .thinking-status');
            if (statusEl) statusEl.textContent = evt.content;
          } else if (evt.type === 'approval_required') {
            const approvalModal = document.getElementById('approval-modal');
            const approvalCommand = document.getElementById('approval-command');
            const approvalDesc = document.getElementById('approval-desc');
            approvalCommand.textContent = evt.content.command || '未知命令';
            approvalDesc.textContent = evt.content.description || '危险操作警告';
            approvalModal.classList.remove('hidden');
          } else if (evt.type === 'chunk') {
            if (thinkEl && thinkEl.parentNode) thinkEl.remove();
            if (!aiBubbleEl) {
              const msgDiv = addMessage('assistant', '', null, false);
              aiBubbleEl = msgDiv.querySelector('.bubble-text');
            }
            aiBubbleEl.dataset.raw = (aiBubbleEl.dataset.raw || '') + evt.content;
            aiBubbleEl.innerHTML = renderMarkdown(aiBubbleEl.dataset.raw);
            scrollToBottom();
          } else if (evt.type === 'done') {
            // trigger sidebar refresh if needed
          } else if (evt.type === 'error') {
            if (thinkEl && thinkEl.parentNode) thinkEl.remove();
            addMessage('assistant', '⚠️ 发生错误: ' + evt.content);
          }
        } catch (err) {
          console.error('SSE JSON parse error:', err, jsonStr);
        }
      }
    }
  } catch (err) {
    console.error('Chat request failed:', err);
    addMessage('assistant', '⚠️ 网络请求失败，请检查后端服务。');
  } finally {
    state.isStreaming = false;
    sendBtn.disabled = false;
    setStatus('ok', '已连接');
    messageInput.focus();
    const thinkElFinal = $('thinking-indicator');
    if (thinkElFinal) thinkElFinal.remove();
  }
}

// ── File Upload ───────────────────────────────────────────────────────────────
function handleFileSelect(file) {
  if (!file) return;
  const MAX_SIZE = 1024 * 1024 * 5; // 5 MB

  if (file.size > MAX_SIZE) {
    alert('文件太大，最大支持 5 MB');
    return;
  }

  const isText = isTextFile(file.name);

  if (isText) {
    const reader = new FileReader();
    reader.onload = e => {
      state.attachment = { name: file.name, content: e.target.result, is_text: true };
      showAttachmentPreview(file.name);
    };
    reader.readAsText(file, 'utf-8');
  } else {
    // For images / binary: just send filename as context
    state.attachment = { name: file.name, content: '', is_text: false };
    showAttachmentPreview(file.name);
  }
}

function isTextFile(name) {
  const textExts = [
    '.txt', '.md', '.py', '.js', '.ts', '.json', '.yaml', '.yml',
    '.csv', '.html', '.css', '.java', '.go', '.rs', '.c', '.cpp',
    '.sh', '.bat', '.log', '.xml', '.sql', '.toml', '.ini', '.env',
  ];
  return textExts.some(ext => name.toLowerCase().endsWith(ext));
}

function showAttachmentPreview(name) {
  attachName.textContent = `📎 ${name}`;
  attachPreview.classList.remove('hidden');
}

function clearAttachment() {
  state.attachment = null;
  attachPreview.classList.add('hidden');
  attachName.textContent = '';
  fileInput.value = '';
}

// ── Memory Panel ──────────────────────────────────────────────────────────────
function startMemoryPolling() {
  fetchMemories();
  setInterval(fetchMemories, 30000); // 延长为每 30 秒请求一次
}

async function fetchMemories() {
  try {
    const sid = state.currentSessionId || '';
    const res  = await fetch('/api/memories?session_id=' + encodeURIComponent(sid));
    state.memoryData = await res.json();
    renderMemory(state.activeMemoryTab);
  } catch (_) { /* silent */ }
}

function renderMemory(tab) {
  state.activeMemoryTab = tab;
  const content = state.memoryData[tab] || '';

  document.querySelectorAll('.mem-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });

  if (!content.trim()) {
    const hints = {
      'MEMORY.md':  ['🧠', '通用记忆尚无内容', '可让 Agent 主动记录跨会话的重要信息'],
      'USER.md':    ['👤', '用户画像尚未建立', 'Agent 会随着对话逐渐了解你'],
    };
    const [icon, title, sub] = hints[tab] || ['💭', '记忆尚未生成', '对话几轮后 Agent 会自动记录'];
    memoryContent.innerHTML = `
      <div class="memory-empty">
        <div class="memory-empty-icon">${icon}</div>
        <p>${title}</p>
        <p class="memory-empty-sub">${sub}</p>
      </div>`;
    return;
  }

  memoryContent.innerHTML = renderMarkdown(content);
}


// ── Markdown renderer (highlight.js + copy button) ────────────────────────
function renderMarkdown(text) {
  if (!text) return '';
  if (typeof marked !== 'undefined') {
    const renderer = new marked.Renderer();
    renderer.code = function(code, lang) {
      // Support both (code, lang) and ({text, lang}) API
      if (typeof code === 'object') {
        lang = code.lang;
        code = code.text;
      }
      const langLabel = lang ? '<span class="code-lang">' + escHtml(lang) + '</span>' : '';
      let highlighted;
      try {
        highlighted = lang && hljs.getLanguage(lang)
          ? hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
          : hljs.highlightAuto(code).value;
      } catch(e) {
        highlighted = escHtml(code);
      }
      const langAttr = lang ? ' class="language-' + escHtml(lang) + '"' : '';
      return '<div class="code-block-wrap">'
        + '<div class="code-header">'
        +   langLabel
        +   '<button class="copy-btn" onclick="copyCode(this)">复制</button>'
        + '</div>'
        + '<pre><code' + langAttr + '>' + highlighted + '</code></pre>'
        + '</div>';
    };
    return marked.parse(text, { renderer });
  }
  return text;
}

// 复制代码按钮处理
function copyCode(btn) {
  const pre = btn.closest('.code-block-wrap').querySelector('pre code');
  const text = pre.innerText || pre.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = '已复制 ✓';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 2000);
  }).catch(() => {
    btn.textContent = '失败';
    setTimeout(() => { btn.textContent = '复制'; }, 2000);
  });
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── Auto-resize textarea ──────────────────────────────────────────────────────
function autoResizeTextarea() {
  messageInput.style.height = 'auto';
  messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
}

// ── Event bindings ────────────────────────────────────────────────────────────
function bindEvents() {
  // Theme switcher
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', () => applyTheme(btn.dataset.theme));
  });

  // New session
  newSessionBtn.addEventListener('click', createNewSession);

  // Send on Enter (Shift+Enter = newline)
  messageInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  messageInput.addEventListener('input', autoResizeTextarea);

  // Send button
  sendBtn.addEventListener('click', sendMessage);

  // Stop button
  stopBtn.addEventListener('click', async () => {
    if (!state.currentSessionId || !state.isStreaming) return;
    try {
      stopBtn.disabled = true;
      await fetch(`/api/sessions/${state.currentSessionId}/interrupt`, { method: 'POST' });
    } catch (e) {
      console.error('Failed to interrupt:', e);
    }
  });

  // File input
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) handleFileSelect(fileInput.files[0]);
  });

  // Remove attachment
  attachRemove.addEventListener('click', clearAttachment);

  // Memory tabs
  document.querySelectorAll('.mem-tab').forEach(btn => {
    btn.addEventListener('click', () => renderMemory(btn.dataset.tab));
  });

  // Drag-and-drop on chat panel
  const chatPanel = $('chat-panel');
  chatPanel.addEventListener('dragover', e => { e.preventDefault(); chatPanel.classList.add('drag-over'); });
  chatPanel.addEventListener('dragleave', () => chatPanel.classList.remove('drag-over'));
  chatPanel.addEventListener('drop', e => {
    e.preventDefault();
    chatPanel.classList.remove('drag-over');
    const file = e.dataTransfer?.files?.[0];
    if (file) handleFileSelect(file);
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);

// ── Approval Modal ───────────────────────────────────────────────────────────
const approvalModal = document.getElementById('approval-modal');
const btnApprove = document.getElementById('btn-approve');
const btnDeny = document.getElementById('btn-deny');

if (btnApprove && btnDeny) {
  btnApprove.addEventListener('click', () => submitApproval(true));
  btnDeny.addEventListener('click', () => submitApproval(false));
}

async function submitApproval(approved) {
  approvalModal.classList.add('hidden');
  try {
    await fetch('/api/approval_respond', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.currentSessionId, approved })
    });
  } catch (e) {
    console.error('Approval respond failed', e);
  }
}

// ── More Sessions Popup ───────────────────────────────────────────────────────
const moreSessionsBtn   = document.getElementById('more-sessions-btn');
const moreSessionsPopup = document.getElementById('more-sessions-popup');
const closeMoreSessions = document.getElementById('close-more-sessions');
const moreSessionsList  = document.getElementById('more-sessions-list');

if (moreSessionsBtn) {
  moreSessionsBtn.addEventListener('click', (e) => {
    e.stopPropagation(); // 防止被 document 监听到立刻关闭
    moreSessionsList.innerHTML = '';
    const rest = _allSessions.slice(5);
    rest.forEach(s => moreSessionsList.appendChild(buildSessionItem(s, _allSessions)));
    moreSessionsPopup.classList.toggle('hidden');
  });
}

if (closeMoreSessions) {
  closeMoreSessions.addEventListener('click', () => {
    moreSessionsPopup.classList.add('hidden');
  });
}

// 点击弹框外部关闭
document.addEventListener('click', (e) => {
  if (!moreSessionsPopup || moreSessionsPopup.classList.contains('hidden')) return;
  if (!moreSessionsPopup.contains(e.target) && e.target !== moreSessionsBtn) {
    moreSessionsPopup.classList.add('hidden');
  }
});

// ── Usage Log Modal ───────────────────────────────────────────────────────────
const usageLogBtn   = document.getElementById('usage-log-btn');
const usageLogModal = document.getElementById('usage-log-modal');
const closeUsageLog = document.getElementById('close-usage-log');
const usageLogTbody = document.getElementById('usage-log-tbody');

if (usageLogBtn) {
  usageLogBtn.addEventListener('click', async () => {
    usageLogModal.classList.toggle('hidden');
    if (!usageLogModal.classList.contains('hidden')) {
      await fetchUsageLogs();
    }
  });
}

if (closeUsageLog) {
  closeUsageLog.addEventListener('click', () => {
    usageLogModal.classList.add('hidden');
  });
}

// 点击弹框外部关闭
if (usageLogModal) {
  usageLogModal.addEventListener('click', e => {
    if (e.target === usageLogModal) usageLogModal.classList.add('hidden');
  });
}

async function fetchUsageLogs() {
  try {
    const res  = await fetch('/api/usage_logs');
    const logs = await res.json();
    renderUsageLogs(logs);
  } catch (_) {
    if (usageLogTbody) usageLogTbody.innerHTML = '<tr><td colspan="7" class="log-empty">加载失败</td></tr>';
  }
}

function renderUsageLogs(logs) {
  if (!usageLogTbody) return;
  if (!logs || logs.length === 0) {
    usageLogTbody.innerHTML = '<tr><td colspan="7" class="log-empty">暂无记录</td></tr>';
    return;
  }
  usageLogTbody.innerHTML = logs.map(log => {
    const dur  = log.duration_ms ? (log.duration_ms / 1000).toFixed(1) + 's' : '—';
    const ttft = log.ttft_ms    ? (log.ttft_ms / 1000).toFixed(2) + 's' : '—';
    const modelShort = (log.model || '').replace('deepseek-', 'ds-');
    const stream = log.streaming
      ? '<span class="log-yes">✓</span>'
      : '<span class="log-no">✗</span>';
    return `<tr>
      <td>${escHtml(log.time || '—')}</td>
      <td title="${escHtml(log.model || '')}"><span class="log-model">${escHtml(modelShort)}</span></td>
      <td>${dur}</td>
      <td>${ttft}</td>
      <td>${(log.input_tokens || 0).toLocaleString()}</td>
      <td>${(log.output_tokens || 0).toLocaleString()}</td>
      <td>${stream}</td>
    </tr>`;
  }).join('');
}
