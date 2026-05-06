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
  activeMemoryTab: 'SESSION.md',
  memoryData: { 'SESSION.md': '', 'MEMORY.md': '', 'USER.md': '' },
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

function renderSessionList(list) {
  sessionList.innerHTML = '';
  list.forEach(s => {
    const item = document.createElement('div');
    item.className = 'session-item';
    item.dataset.id = s.id;
    item.setAttribute('role', 'listitem');
    item.innerHTML = `
      <div class="session-title">${escHtml(s.title)}</div>
      <div class="session-meta">${s.message_count} 条消息${s.last_message ? ' · ' + escHtml(s.last_message) : ''}</div>
    `;
    item.addEventListener('click', () => {
      selectSession(s.id);
      restoreTokenFromSession(s.id, list);
    });
    sessionList.appendChild(item);
  });
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
      badge.querySelector('.tool-spinner')?.remove();
      // 完成后 2 秒淡出
      setTimeout(() => {
        badge.style.transition = 'opacity 0.5s';
        badge.style.opacity = '0';
        setTimeout(() => badge.remove(), 500);
      }, 2000);
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
      'SESSION.md': ['📝', '本会话暂无摘要', '对话满 10 条后自动生成专属记忆'],
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

  // 先处理代码块（保护不被其他替换破坏）
  const codeBlocks = [];
  let html = text.replace(/```([a-z]*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const id = `cb-${codeBlocks.length}`;
    const highlighted = (lang && window.hljs && hljs.getLanguage(lang))
      ? hljs.highlight(code.trimEnd(), { language: lang }).value
      : (window.hljs ? hljs.highlightAuto(code.trimEnd()).value : escHtml(code.trimEnd()));
    const langBadge = lang ? `<span class="code-lang">${escHtml(lang)}</span>` : '';
    codeBlocks.push(
      `<div class="code-block-wrap">` +
      `<div class="code-header">${langBadge}<button class="copy-btn" onclick="copyCode(this)" title="复制代码">复制</button></div>` +
      `<pre><code class="hljs${lang ? ` language-${lang}` : ''}">${highlighted}</code></pre>` +
      `</div>`
    );
    return `%%CODEBLOCK_${id}%%`;
  });

  // 内联代码
  html = escHtml(html);
  html = html.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');

  // 粗体、斜体
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g,     '<em>$1</em>');

  // 标题
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm,   '<h1>$1</h1>');

  // 分割线
  html = html.replace(/^---+$/gm, '<hr/>');

  // 列表
  html = html.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
  html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

  // 换行
  html = html.replace(/\n\n/g, '<br/><br/>');
  html = html.replace(/\n/g,   '<br/>');

  // 还原代码块
  html = html.replace(/%%CODEBLOCK_cb-(\d+)%%/g, (_, i) => codeBlocks[+i]);

  return html;
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
