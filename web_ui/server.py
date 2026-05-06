"""
Hermes Agent Web UI - Flask Backend
方案C：开启 Hermes 原生记忆体系 + 超过阈值自动摘要写入 MEMORY.md
"""
import sys, os, json, uuid, threading, queue, time
from pathlib import Path
from datetime import datetime
from openai import OpenAI
from flask import Flask, render_template, request, Response, jsonify
from flask_cors import CORS

# Add parent dir so we can import hermes modules
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Suppress noisy stderr during import
import io, contextlib
with contextlib.redirect_stderr(io.StringIO()):
    from run_agent import AIAgent, load_hermes_dotenv
    from hermes_constants import get_hermes_home

load_hermes_dotenv()

app = Flask(__name__)
CORS(app)

HERMES_HOME = Path(get_hermes_home())
SESSIONS_FILE = Path(__file__).parent / "sessions_data.json"
MEMORY_DIR = HERMES_HOME / "memories"
SESSION_MEMORY_DIR = MEMORY_DIR / "sessions"  # 会话专属记忆目录
GLOBAL_MEMORY_FILE = MEMORY_DIR / "MEMORY.md"  # 通用记忆（全局）
USER_FILE = MEMORY_DIR / "USER.md"             # 用户画像（全局）

# 触发摘要的消息阈值（每隔 N 条 assistant 消息触发一次）
SUMMARY_EVERY_N = 10

# In-memory session store
# { session_id: { agent, title, messages, created_at, summarized_count } }
sessions: dict = {}


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

def load_sessions():
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for sid, sd in data.items():
                sessions[sid] = {
                    "agent": None,
                    "title": sd.get("title", "未命名会话"),
                    "messages": sd.get("messages", []),
                    "created_at": sd.get("created_at", ""),
                    "summarized_count": sd.get("summarized_count", 0),
                    "token_total": sd.get("token_total", 0),
                    "token_in":    sd.get("token_in", 0),
                    "token_out":   sd.get("token_out", 0),
                }
        except Exception:
            pass


def save_sessions():
    data = {
        sid: {
            "title": sd["title"],
            "messages": sd["messages"],
            "created_at": sd["created_at"],
            "summarized_count": sd.get("summarized_count", 0),
            "token_total": sd.get("token_total", 0),
            "token_in":    sd.get("token_in", 0),
            "token_out":   sd.get("token_out", 0),
        }
        for sid, sd in sessions.items()
    }
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Memory summarization (Plan C core)
# ---------------------------------------------------------------------------

def _build_summary_prompt(session_id: str, messages: list) -> str:
    """构建给 DeepSeek 的摘要 prompt"""
    history_text = ""
    for m in messages:
        role = "用户" if m["role"] == "user" else "助手"
        history_text += f"【{role}】{m['content'][:300]}\n\n"

    return f"""请对以下对话内容生成一份简洁的结构化记忆摘要，以 Markdown 格式输出。
包含：关键事实、用户需求、重要决定、待办事项（如有）。不要复述原文，只保留核心信息。

会话ID: {session_id[:8]}...
---
{history_text}
---
请输出结构化摘要："""


def trigger_memory_summary(session_id: str):
    """后台线程：调用 DeepSeek 生成摘要，追加写入 MEMORY.md"""
    def _do():
        try:
            sd = sessions.get(session_id)
            if not sd:
                return

            messages = sd["messages"]
            if not messages:
                return

            # 只摘要新增部分（避免重复摘要）
            already = sd.get("summarized_count", 0)
            new_msgs = messages[already:]
            if len(new_msgs) < 4:
                return

            api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                return

            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1/")
            prompt = _build_summary_prompt(session_id, new_msgs)

            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0.3,
            )
            summary = resp.choices[0].message.content.strip()

            # 写入会话专属摘要文件（覆盖写，始终是最新完整摘要）
            SESSION_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            session_file = SESSION_MEMORY_DIR / f"{session_id[:8]}.md"
            title = sd.get("title", "未命名会话")
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            tok_total = sd.get("token_total", 0)
            tok_in    = sd.get("token_in", 0)
            tok_out   = sd.get("token_out", 0)
            fmt = lambda n: f"{n:,}" if n else "0"
            token_line = f"⚡ 累计 Token：{fmt(tok_total)}（输入 {fmt(tok_in)} / 输出 {fmt(tok_out)}）"
            content = (
                f"# 会话摘要：{title}\n\n"
                f"> 最后更新：{now}  \n"
                f"> {token_line}\n\n"
                f"{summary}\n"
            )
            with open(session_file, "w", encoding="utf-8") as f:
                f.write(content)

            # 更新已摘要的消息数
            sd["summarized_count"] = len(messages)
            save_sessions()

        except Exception as e:
            print(f"[Memory Summary] 摘要失败: {e}")

    t = threading.Thread(target=_do, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------

def get_or_create_agent(session_id: str, web_search: bool = False,
                        deep_thinking: bool = True, event_q: queue.Queue = None):
    if session_id not in sessions:
        return None
    sd = sessions[session_id]

    def _make_callbacks():
        def _tool_start(tool_name, args, **kw):
            if event_q:
                label = _tool_label(tool_name, args)
                event_q.put(("tool_start", {"name": tool_name, "label": label}))

        def _tool_complete(tool_name, result, **kw):
            if event_q:
                event_q.put(("tool_done", {"name": tool_name}))

        def _reasoning(text, **kw):
            if event_q:
                event_q.put(("reasoning", {"text": text}))

        return _tool_start, _tool_complete, _reasoning

    if sd["agent"] is None:
        _tool_start, _tool_complete, _reasoning = _make_callbacks()
        disabled = None if web_search else ["web"]

        # 初始化时动态选择模型
        initial_model = "deepseek-reasoner" if deep_thinking else "deepseek-v4-pro"

        agent = AIAgent(
            provider="deepseek",
            model=initial_model,
            quiet_mode=True,
            platform="web",
            session_id=session_id,
            skip_context_files=True,
            max_iterations=10,
            disabled_toolsets=disabled,
            tool_start_callback=_tool_start,
            tool_complete_callback=_tool_complete,
            reasoning_callback=_reasoning,
        )
        sd["agent"] = agent
        sd["web_search_enabled"] = web_search

    else:
        agent = sd["agent"]
        current_ws = sd.get("web_search_enabled", None)

        # ── 联网开关切换：直接更新工具列表，不重建 Agent ────────────────
        if current_ws != web_search:
            from model_tools import get_tool_definitions
            disabled = None if web_search else ["web"]
            agent.tools = get_tool_definitions(
                disabled_toolsets=disabled,
                quiet_mode=True,
            )
            agent.valid_tool_names = {
                t["function"]["name"] for t in agent.tools
            } if agent.tools else set()
            sd["web_search_enabled"] = web_search

        # ── 每次请求都更新 callbacks（指向本次的 event_q）───────────────
        _tool_start, _tool_complete, _reasoning = _make_callbacks()
        agent.tool_start_callback = _tool_start
        agent.tool_complete_callback = _tool_complete
        agent.reasoning_callback = _reasoning
        
        # ── 动态更新联网搜索开关 ──
        if is_web_search:
            if agent.disabled_toolsets and "web" in agent.disabled_toolsets:
                agent.disabled_toolsets.remove("web")
        else:
            if agent.disabled_toolsets is None:
                agent.disabled_toolsets = ["web"]
            elif "web" not in agent.disabled_toolsets:
                agent.disabled_toolsets.append("web")
        
    # ── 深度思考开关：动态切换模型 ──
    if deep_thinking:
        agent.model = "deepseek-reasoner"
        agent.reasoning_config = None
    else:
        agent.model = "deepseek-v4-pro"
        agent.reasoning_config = {"enabled": False, "effort": "none"}

    return agent



def _tool_label(tool_name: str, args: dict) -> str:
    """生成工具调用的人类可读标签"""
    labels = {
        "web_search": lambda a: f"🔍 搜索「{str(a.get('query', ''))[:30]}」",
        "web_extract": lambda a: f"📄 阅读网页",
        "web_crawl": lambda a: f"🕷️ 爬取网站",
        "terminal": lambda a: f"💻 执行命令「{str(a.get('command', ''))[:30]}」",
        "read_file": lambda a: f"📂 读取文件「{str(a.get('path', ''))[:30]}」",
        "write_file": lambda a: f"✏️ 写入文件「{str(a.get('path', ''))[:30]}」",
        "vision_analyze": lambda a: f"👁️ 分析图片",
        "memory": lambda a: f"🧠 访问记忆",
        "todo": lambda a: f"📋 操作待办",
        "browser_navigate": lambda a: f"🌐 浏览器导航「{str(a.get('url', ''))[:30]}」",
    }
    fn = labels.get(tool_name)
    if fn:
        try:
            return fn(args or {})
        except Exception:
            pass
    return f"🛠️ 调用工具 {tool_name}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    result = []
    for sid, sd in sessions.items():
        last = sd["messages"][-1]["content"][:60] if sd["messages"] else ""
        result.append({
            "id": sid,
            "title": sd["title"],
            "message_count": len(sd["messages"]),
            "created_at": sd["created_at"],
            "last_message": last,
            "token_total": sd.get("token_total", 0),
            "token_in":    sd.get("token_in", 0),
            "token_out":   sd.get("token_out", 0),
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify(result)


@app.route("/api/sessions/<session_id>/interrupt", methods=["POST"])
def interrupt_session(session_id):
    if session_id in sessions:
        sd = sessions[session_id]
        tid = sd.get("current_thread_id")
        if tid:
            from tools.interrupt import set_interrupt
            set_interrupt(True, thread_id=tid)
        agent = sd.get("agent")
        if agent:
            agent._interrupt_requested = True
        return jsonify({"status": "ok"})
    return jsonify({"error": "not found"}), 404

@app.route("/api/sessions/new", methods=["POST"])
def new_session():
    sid = str(uuid.uuid4())
    sessions[sid] = {
        "agent": None,
        "title": "新会话",
        "messages": [],
        "created_at": datetime.now().isoformat(),
        "summarized_count": 0,
        "token_total": 0,
        "token_in":    0,
        "token_out":   0,
    }
    save_sessions()
    return jsonify({"session_id": sid, "title": "新会话"})


@app.route("/api/sessions/<session_id>/messages", methods=["GET"])
def get_messages(session_id):
    if session_id not in sessions:
        return jsonify([])
    return jsonify(sessions[session_id]["messages"])


@app.route("/api/memories", methods=["GET"])
def get_memories():
    """返回三类记忆：会话专属、通用记忆、用户画像"""
    session_id = request.args.get("session_id", "")

    # 会话专属记忆
    if session_id:
        session_file = SESSION_MEMORY_DIR / f"{session_id[:8]}.md"
        session_mem = session_file.read_text(encoding="utf-8") if session_file.exists() else ""
    else:
        session_mem = ""

    return jsonify({
        "SESSION.md": session_mem,
        "MEMORY.md":  GLOBAL_MEMORY_FILE.read_text(encoding="utf-8") if GLOBAL_MEMORY_FILE.exists() else "",
        "USER.md":    USER_FILE.read_text(encoding="utf-8") if USER_FILE.exists() else "",
    })


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    session_id: str = data.get("session_id", "")
    is_regenerate: bool = data.get("regenerate", False)
    is_web_search: bool = data.get("web_search", False)
    deep_thinking: bool = data.get("deep_thinking", True)

    if not session_id or session_id not in sessions:
        return jsonify({"error": "无效的会话 ID"}), 400

    if is_regenerate:
        if sessions[session_id]["messages"] and sessions[session_id]["messages"][-1]["role"] == "assistant":
            sessions[session_id]["messages"].pop()
        full_message = "请忽略你的上一条回复，并尝试换一种方式或更详细地重新生成一次回复。"
    else:
        message: str = data.get("message", "").strip()
        attachment: dict = data.get("attachment")

        if not message and not attachment:
            return jsonify({"error": "消息不能为空"}), 400

        full_message = message
        if attachment:
            if attachment.get("is_text"):
                full_message = (
                    f"[文件: {attachment['name']}]\n```\n{attachment['content'][:8000]}\n```\n\n"
                    + message
                )
            else:
                full_message = f"[图片已上传: {attachment['name']}] {message}"

        sessions[session_id]["messages"].append({
            "role": "user",
            "content": message,
            "attachment": attachment.get("name") if attachment else None,
        })

        if len(sessions[session_id]["messages"]) == 1:
            sessions[session_id]["title"] = message[:30] + ("…" if len(message) > 30 else "")

    result_q: queue.Queue = queue.Queue()
    event_q: queue.Queue = queue.Queue()   # 工具调用 / reasoning 事件队列

    def run_agent():
        try:
            event_q.put(("status", "⚙️ 初始化 Agent 工具集..."))
            agent = get_or_create_agent(session_id, is_web_search, deep_thinking, event_q)
            if agent is None:
                result_q.put(("error", "会话不存在"))
                return
            event_q.put(("status", "🧠 加载记忆与上下文..."))
            event_q.put(("status", "🔗 连接 DeepSeek API..."))
            
            def _text_delta(text):
                event_q.put(("chunk", text))
                
            response = agent.chat(full_message, stream_callback=_text_delta)
            # 读取 token 用量（当前会话累计）
            token_total = getattr(agent, "session_total_tokens", 0)
            token_in    = getattr(agent, "session_input_tokens", 0) or getattr(agent, "session_prompt_tokens", 0)
            token_out   = getattr(agent, "session_output_tokens", 0) or getattr(agent, "session_completion_tokens", 0)
            # 回写到 session（持久化）
            sd = sessions.get(session_id)
            if sd:
                sd["token_total"] = token_total
                sd["token_in"]    = token_in
                sd["token_out"]   = token_out
            result_q.put(("ok", response or "（无回复）", token_total, token_in, token_out))
        except Exception as exc:
            result_q.put(("error", str(exc), 0, 0, 0))

    t = threading.Thread(target=run_agent, daemon=True)
    t.start()
    sessions[session_id]["current_thread_id"] = t.ident

    def generate():
        while True:
            # 先消费事件队列中的所有事件
            while True:
                try:
                    ev = event_q.get_nowait()
                    ev_type = ev[0]
                    if ev_type == "status":
                        yield f"data: {json.dumps({'type': 'status', 'content': ev[1]})}\n\n"
                    elif ev_type == "tool_start":
                        yield f"data: {json.dumps({'type': 'tool_start', 'name': ev[1]['name'], 'label': ev[1]['label']})}\n\n"
                    elif ev_type == "tool_done":
                        yield f"data: {json.dumps({'type': 'tool_done', 'name': ev[1]['name']})}\n\n"
                    elif ev_type == "reasoning":
                        yield f"data: {json.dumps({'type': 'reasoning', 'text': ev[1]['text']})}\n\n"
                    elif ev_type == "chunk":
                        yield f"data: {json.dumps({'type': 'chunk', 'content': ev[1]})}\n\n"
                except queue.Empty:
                    break

            try:
                result = result_q.get(timeout=0.3)
                break
            except queue.Empty:
                if not t.is_alive() and event_q.empty():
                    result = ("error", "Agent 线程意外退出", 0, 0, 0)
                    break
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                time.sleep(0.1)

        kind = result[0]
        content = result[1]
        token_total = result[2] if len(result) > 2 else 0
        token_in    = result[3] if len(result) > 3 else 0
        token_out   = result[4] if len(result) > 4 else 0

        if kind == "error":
            yield f"data: {json.dumps({'type': 'error', 'content': content})}\n\n"
            return

        # Persist assistant message
        sessions[session_id]["messages"].append({
            "role": "assistant",
            "content": content,
        })
        save_sessions()

        # ── 方案C：摘要触发器 ──
        assistant_count = sum(1 for m in sessions[session_id]["messages"] if m["role"] == "assistant")
        already_summarized = sessions[session_id].get("summarized_count", 0)
        total_msgs = len(sessions[session_id]["messages"])
        if assistant_count > 0 and assistant_count % SUMMARY_EVERY_N == 0 and total_msgs > already_summarized:
            trigger_memory_summary(session_id)

        # Stream in small chunks for typewriter effect
        chunk_size = 5
        for i in range(0, len(content), chunk_size):
            chunk = content[i: i + chunk_size]
            yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
            time.sleep(0.012)

        yield f"data: {json.dumps({'type': 'done', 'session_title': sessions[session_id]['title'], 'token_total': token_total, 'token_in': token_in, 'token_out': token_out})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    load_sessions()
    if not sessions:
        sid = str(uuid.uuid4())
        sessions[sid] = {
            "agent": None,
            "title": "默认会话",
            "messages": [],
            "created_at": datetime.now().isoformat(),
            "summarized_count": 0,
        }
        save_sessions()

    print("🔱 Hermes Agent Web UI")
    print("🌐 访问地址: http://localhost:7860")
    app.run(host="0.0.0.0", port=7860, debug=False, threaded=True)
