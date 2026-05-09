"""
Hermes Agent Web UI - Flask Backend
深度重构版：薄 Web 层 + Hermes 原生能力全开
- 会话存储：Hermes SessionDB（SQLite，带全文搜索）
- 记忆系统：AIAgent 原生 Memory Manager（逐轮同步 + 语义预取）
- 上下文压缩：ContextCompressor 按 Token 预算自动触发
- 死循环拦截：ToolCallGuardrailController 原生守护
- Skill 注入：自动加载 ~/.hermes/skills/（已在 config.yaml 过滤无效 Skill）
"""
import sys, os, json, uuid, threading, queue, time, traceback, signal
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, Response, jsonify
from flask_cors import CORS

# ── 将仓库根目录加入 sys.path ────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ── 静默导入（抑制 Hermes 启动时的 stderr 噪音）──────────────────────────
import io, contextlib
with contextlib.redirect_stderr(io.StringIO()):
    from run_agent import AIAgent, load_hermes_dotenv
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB


load_hermes_dotenv()

app = Flask(__name__)
CORS(app)

HERMES_HOME = Path(get_hermes_home())

# ── 原生 SessionDB（单例，应用级共享）────────────────────────────────────
_session_db = SessionDB()

# ── 运行时 Agent 缓存（内存，重启后重新懒加载）───────────────────────────
# { session_id: { agent, web_search_enabled, approval_q, current_thread_id } }
_runtime: dict = {}


# ---------------------------------------------------------------------------
# SessionDB 封装辅助
# ---------------------------------------------------------------------------

def _get_db() -> SessionDB:
    return _session_db


def _ensure_runtime(session_id: str):
    """确保 _runtime 里有这个会话的运行时槽位。"""
    if session_id not in _runtime:
        _runtime[session_id] = {
            "agent": None,
            "web_search_enabled": None,
            "approval_q": queue.Queue(),
            "current_thread_id": None,
        }


# ---------------------------------------------------------------------------
# Agent 工厂
# ---------------------------------------------------------------------------

def get_or_create_agent(session_id: str, web_search: bool,
                        deep_thinking: bool, event_q: queue.Queue):
    """懒加载并复用 AIAgent 实例。"""
    db = _get_db()

    # 确保 SessionDB 里存在该会话记录
    db.create_session(session_id, source="web")  # 如果已存在，内部会忽略

    _ensure_runtime(session_id)
    rt = _runtime[session_id]

    # ── Callbacks 工厂（每次请求都绑定到当前 event_q）──────────────────
    def _tool_start(tool_call_id, function_name, function_args, **kw):
        # 实时打到 stderr，崩溃前最后一条工具调用一定可见
        try:
            args_preview = str(function_args)[:120]
            sys.stderr.write(f"[TOOL→] {function_name}({args_preview})\n")
            sys.stderr.flush()
        except Exception:
            pass
        if event_q:
            label = _make_tool_label(function_name, function_args)
            event_q.put(("tool_start", {"name": tool_call_id, "label": label}))

    def _tool_complete(tool_call_id, function_name, function_args, function_result, **kw):
        try:
            result_preview = str(function_result)[:80]
            sys.stderr.write(f"[TOOL✓] {function_name} → {result_preview}\n")
            sys.stderr.flush()
        except Exception:
            pass
        if event_q:
            event_q.put(("tool_done", {"name": tool_call_id}))

    def _reasoning(text, **kw):
        if event_q:
            event_q.put(("reasoning", {"text": text}))


    if rt["agent"] is None:
        # ── 首次创建 Agent（对应此会话）──────────────────────────────
        initial_model = "deepseek-reasoner" if deep_thinking else "deepseek-v4-pro"
        # code_execution: 在 Flask 进程内执行 Python 代码，
        # 会被 Agent 用来调 asyncio.run(WeComAdapter...) 之类的代码，
        # 直接接管 Flask 事件循环导致进程崩溃 —— 必须禁用！
        _always_disabled = ["code_execution", "process"]
        disabled = _always_disabled if web_search else _always_disabled + ["web"]

        agent = AIAgent(
            provider="deepseek",
            model=initial_model,
            quiet_mode=True,
            platform="web",
            session_id=session_id,
            skip_context_files=False,
            skip_memory=False,
            max_iterations=30,
            disabled_toolsets=disabled,
            tool_start_callback=_tool_start,
            tool_complete_callback=_tool_complete,
            reasoning_callback=_reasoning,
            ephemeral_system_prompt=(
                "【Web UI 环境限制】你正在 Hermes Web UI 中运行，Flask 服务进程正在运行。\n"
                "严禁执行以下命令，否则会直接杀死当前 Web 服务进程：\n"
                "- hermes gateway / python -m hermes gateway\n"
                "- python gateway/run.py 或任何 run.py\n"
                "- 任何会调用 asyncio.run() 或接管 SIGTERM 信号的长驻进程\n"
                "如果用户要求启动网关，请告知他们在另一个独立终端中手动运行：\n"
                "  cd herms_agent && venv\\Scripts\\activate && python -m hermes gateway"
            ),
        )
        rt["agent"] = agent
        rt["web_search_enabled"] = web_search
    else:
        agent = rt["agent"]

        # ── 联网开关变更：热更新工具集，不重建 Agent ──────────────────
        if rt["web_search_enabled"] != web_search:
            from model_tools import get_tool_definitions
            _always_disabled = ["code_execution", "process"]
            disabled = _always_disabled if web_search else _always_disabled + ["web"]
            agent.tools = get_tool_definitions(disabled_toolsets=disabled, quiet_mode=True)
            agent.valid_tool_names = (
                {t["function"]["name"] for t in agent.tools} if agent.tools else set()
            )
            rt["web_search_enabled"] = web_search

        # ── 每次请求更新 callbacks（指向本次的 event_q）──────────────
        agent.tool_start_callback = _tool_start
        agent.tool_complete_callback = _tool_complete
        agent.reasoning_callback = _reasoning

    # ── 深度思考模式：热切换模型 ──────────────────────────────────────
    target_model = "deepseek-reasoner" if deep_thinking else "deepseek-v4-pro"
    agent.model = target_model
    agent.reasoning_config = None if deep_thinking else {"enabled": False, "effort": "none"}
    if hasattr(agent, "_primary_runtime") and isinstance(agent._primary_runtime, dict):
        agent._primary_runtime["model"] = target_model

    return agent


# ---------------------------------------------------------------------------
# 工具 Emoji 标签（借助原生皮肤引擎，带降级兜底）
# ---------------------------------------------------------------------------

def _make_tool_label(tool_name: str, args: dict) -> str:
    """生成工具调用的人类可读标签（优先读取 Hermes 皮肤配置中的 emoji）。"""
    try:
        from agent.display import get_active_skin
        skin = get_active_skin()
        emoji = (skin.tool_emojis or {}).get(tool_name, "🛠️")
    except Exception:
        emoji = "🛠️"

    # 附上关键参数摘要
    def _truncate(text: str, length: int = 60) -> str:
        s = str(text)
        return s if len(s) <= length else s[:length] + "..."

    arg_hints = {
        "web_search":       lambda a: f"「{_truncate(a.get('query', ''))}」",
        "terminal":         lambda a: f"「{_truncate(a.get('command', ''))}」",
        "read_file":        lambda a: f"「{_truncate(a.get('path', ''))}」",
        "write_file":       lambda a: f"「{_truncate(a.get('path', ''))}」",
        "browser_navigate": lambda a: f"「{_truncate(a.get('url', ''))}」",
        "delegate_task":    lambda a: f"「{_truncate(a.get('task', ''))}」",
    }
    hint = ""
    if tool_name in arg_hints:
        try:
            hint = arg_hints[tool_name](args or {})
        except Exception:
            pass

    return f"{emoji} {tool_name}{hint}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    """从 SessionDB 读取会话列表，按最近活跃排序。"""
    rows = _get_db().list_sessions_rich(
        source="web",
        limit=100,
        order_by_last_active=True,
    )
    result = []
    for r in rows:
        last_active = r.get("last_active") or r.get("started_at") or 0
        # last_active 是 Unix 时间戳浮点数
        result.append({
            "id":            r["id"],
            "title":         r.get("title") or "未命名会话",
            "message_count": r.get("message_count", 0),
            "created_at":    datetime.fromtimestamp(r["started_at"]).isoformat() if r.get("started_at") else "",
            "last_accessed": datetime.fromtimestamp(last_active).isoformat() if last_active else "",
            "last_message":  r.get("preview", "")[:60],
            "token_total":   (r.get("input_tokens", 0) or 0) + (r.get("output_tokens", 0) or 0),
            "token_in":      r.get("input_tokens", 0) or 0,
            "token_out":     r.get("output_tokens", 0) or 0,
        })
    return jsonify(result)


@app.route("/api/sessions/new", methods=["POST"])
def new_session():
    """创建新会话，写入 SessionDB，返回 session_id。"""
    try:
        sid = str(uuid.uuid4())
        db = _get_db()
        db.create_session(sid, source="web")
        # 用时间戳做初始标题，避免唯一性约束冲突
        # 第一轮对话后 Agent 会自动覆盖为语义标题
        ts = datetime.now().strftime("%m-%d %H:%M")
        title = f"新会话 {ts}"
        try:
            db.set_session_title(sid, title)
        except Exception:
            title = f"会话 {sid[:8]}"   # fallback：用 ID 前缀
        _ensure_runtime(sid)
        return jsonify({"session_id": sid, "title": title})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id):
    """删除指定会话。"""
    try:
        _get_db().delete_session(session_id)
        if session_id in _runtime:
            del _runtime[session_id]
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sessions/<session_id>/messages", methods=["GET"])
def get_messages(session_id):
    """从 SessionDB 读取会话消息历史。"""
    msgs = _get_db().get_messages(session_id)
    # 过滤为前端展示格式（只取 user/assistant 的文本消息）
    result = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content") or ""
        if role in ("user", "assistant") and content:
            result.append({"role": role, "content": content})
    return jsonify(result)


@app.route("/api/sessions/<session_id>/interrupt", methods=["POST"])
def interrupt_session(session_id):
    """中断正在运行的 Agent 线程。"""
    rt = _runtime.get(session_id)
    if rt:
        from tools.interrupt import set_interrupt
        set_interrupt(True, thread_id=rt.get("current_thread_id"))
        agent = rt.get("agent")
        if agent:
            agent._interrupt_requested = True
        return jsonify({"status": "ok"})
    return jsonify({"error": "not found"}), 404


@app.route("/api/memories", methods=["GET"])
def get_memories():
    """
    读取 Hermes 原生记忆文件。
    builtin provider 把记忆写在 ~/.hermes/memories/ 目录下。
    """
    session_id = request.args.get("session_id", "")
    mem_dir = HERMES_HOME / "memories"
    session_mem_dir = mem_dir / "sessions"

    session_mem = ""
    if session_id:
        for candidate in [
            session_mem_dir / f"{session_id}.md",
            session_mem_dir / f"{session_id[:8]}.md",
        ]:
            if candidate.exists():
                session_mem = candidate.read_text(encoding="utf-8")
                break

    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            return ""

    return jsonify({
        "SESSION.md": session_mem,
        "MEMORY.md":  _read(mem_dir / "MEMORY.md"),
        "USER.md":    _read(mem_dir / "USER.md"),
    })


@app.route("/api/usage_logs", methods=["GET"])
def get_usage_logs():
    """
    从 SessionDB 聚合使用日志（替代原来的 usage_log.json）。
    只过滤掉没有任何消息的空会话。
    """
    rows = _get_db().list_sessions_rich(
        source="web",
        limit=200,
        order_by_last_active=True,
    )
    logs = []
    for r in rows:
        # 只过滤完全空的会话
        if not r.get("message_count", 0):
            continue
        last_active = r.get("last_active") or r.get("started_at") or 0
        logs.append({
            "time":          datetime.fromtimestamp(last_active).strftime("%Y-%m-%d %H:%M:%S") if last_active else "",
            "model":         r.get("model") or "deepseek",
            "input_tokens":  r.get("input_tokens", 0) or 0,
            "output_tokens": r.get("output_tokens", 0) or 0,
            "session_id":    r["id"][:8],
            "title":         r.get("title") or "",
        })
    return jsonify(logs)


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    session_id: str  = data.get("session_id", "")
    is_regenerate: bool = data.get("regenerate", False)
    is_web_search: bool = data.get("web_search", False)
    deep_thinking: bool = data.get("deep_thinking", True)

    db = _get_db()

    # ── 确保 session 存在（可能是从旧版 JSON 迁移过来的 id）──────────────
    db.create_session(session_id, source="web")
    _ensure_runtime(session_id)

    _req_start_time = time.time()

    if is_regenerate:
        # 删掉最后一条 assistant 消息，重新生成
        msgs = db.get_messages(session_id)
        if msgs and msgs[-1]["role"] == "assistant":
            # SessionDB 目前无 delete_message，用 replace_messages 实现
            kept = [m for m in msgs[:-1]]
            db.replace_messages(session_id, [
                {"role": m["role"], "content": m["content"] or ""}
                for m in kept
                if m["role"] in ("user", "assistant") and (m.get("content") or "")
            ])
        full_message = "请忽略你的上一条回复，并尝试换一种方式或更详细地重新生成一次回复。"
    else:
        message: str   = data.get("message", "").strip()
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

        # ── 将用户消息写入 SessionDB ──────────────────────────────────
        db.append_message(session_id, "user", content=message)

        # ── 首条消息时自动设置标题 ────────────────────────────────────
        msg_count = db.message_count(session_id)
        if msg_count <= 1:
            title = message[:30] + ("…" if len(message) > 30 else "")
            db.set_session_title(session_id, title)

    result_q: queue.Queue = queue.Queue()
    event_q:  queue.Queue = queue.Queue()

    def run_agent():
        try:
            event_q.put(("status", "⚙️ 初始化 Agent 工具集..."))
            agent = get_or_create_agent(session_id, is_web_search, deep_thinking, event_q)
            event_q.put(("status", "🧠 加载记忆与上下文..."))
            event_q.put(("status", "🔗 连接 DeepSeek API..."))

            def _text_delta(text):
                event_q.put(("chunk", text))

            # ── 终端审批回调 ──────────────────────────────────────────
            from tools.terminal_tool import set_approval_callback
            def _approval_handler(command, description, **kwargs):
                event_q.put(("approval_required", {"command": command, "description": description}))
                try:
                    return _runtime[session_id]["approval_q"].get(timeout=300)
                except queue.Empty:
                    return False
            set_approval_callback(_approval_handler)

            # ── 构造历史记录给 AIAgent ────────────────────────────────
            # AIAgent(session_id=...) 已经自动从 SessionDB 加载了历史，
            # 但为了保证本次 run_conversation 前序消息正确，我们也传入。
            raw_msgs = db.get_messages(session_id)
            # 只取最近 30 条，避免超长历史撑爆上下文导致 API 超时
            # AIAgent 的内置 ContextCompressor 也会自动压缩，这里是双重保险
            HISTORY_LIMIT = 30
            eligible = [
                m for m in raw_msgs
                if m["role"] in ("user", "assistant") and (m.get("content") or "")
            ]
            history = [
                {"role": m["role"], "content": m["content"] or ""}
                for m in eligible[-HISTORY_LIMIT - 1:-1]  # 取倒数第 31~2 条，去掉刚 append 的最后一条
            ]

            result = agent.run_conversation(
                user_message=full_message,
                conversation_history=history,
                stream_callback=_text_delta,
                task_id=session_id,
            )
            response = result.get("final_response", "")

            # ── 将 assistant 回复写入 SessionDB ──────────────────────
            if response:
                db.append_message(session_id, "assistant", content=response)

            # ── 同步 Token 用量到 SessionDB ──────────────────────────
            token_in  = getattr(agent, "session_input_tokens", 0) or getattr(agent, "session_prompt_tokens", 0) or 0
            token_out = getattr(agent, "session_output_tokens", 0) or getattr(agent, "session_completion_tokens", 0) or 0
            if token_in or token_out:
                db.update_token_counts(session_id, input_tokens=token_in, output_tokens=token_out,
                                       model=agent.model, absolute=True)

            result_q.put(("ok", response or "（无回复）", token_in + token_out, token_in, token_out))
        except BaseException as exc:
            # BaseException 包括 SystemExit / KeyboardInterrupt
            # 不能让它们从 daemon 线程泄漏出去（虽然不会杀 Flask，但会丢失错误信息）
            err_msg = f"{type(exc).__name__}: {exc}"
            sys.stderr.write(f"[AGENT ERR] {err_msg}\n")
            sys.stderr.flush()
            result_q.put(("error", err_msg, 0, 0, 0))


    t = threading.Thread(target=run_agent, daemon=True)
    t.start()
    _runtime[session_id]["current_thread_id"] = t.ident

    def generate():
        SSE = "\n\n"   # SSE 帧分隔符，避免在 f-string 内嵌 \n 引发转义混乱

        def sse(payload: dict) -> str:
            return "data: " + json.dumps(payload, ensure_ascii=False) + SSE

        got_stream_chunks = False
        while True:
            # 消费事件队列
            while True:
                try:
                    ev = event_q.get_nowait()
                    ev_type = ev[0]
                    if ev_type == "status":
                        yield sse({"type": "status", "content": ev[1]})
                    elif ev_type == "tool_start":
                        yield sse({"type": "tool_start", "name": ev[1]["name"], "label": ev[1]["label"]})
                    elif ev_type == "tool_done":
                        yield sse({"type": "tool_done", "name": ev[1]["name"]})
                    elif ev_type == "reasoning":
                        yield sse({"type": "reasoning", "text": ev[1]["text"]})
                    elif ev_type == "chunk":
                        got_stream_chunks = True
                        yield sse({"type": "chunk", "content": ev[1]})
                    elif ev_type == "approval_required":
                        yield sse({"type": "approval_required", **ev[1]})
                except queue.Empty:
                    break

            try:
                result = result_q.get(timeout=0.3)
                # 冲刷剩余事件
                time.sleep(0.05)
                while not event_q.empty():
                    try:
                        ev = event_q.get_nowait()
                        if ev[0] == "reasoning":
                            yield sse({"type": "reasoning", "text": ev[1]["text"]})
                        elif ev[0] == "chunk":
                            got_stream_chunks = True
                            yield sse({"type": "chunk", "content": ev[1]})
                        elif ev[0] == "tool_done":
                            yield sse({"type": "tool_done", "name": ev[1]["name"]})
                    except queue.Empty:
                        break
                break
            except queue.Empty:
                if not t.is_alive() and event_q.empty():
                    result = ("error", "Agent 线程意外退出", 0, 0, 0)
                    break
                yield sse({"type": "heartbeat"})
                time.sleep(0.1)

        kind, content_text = result[0], result[1]
        token_total = result[2] if len(result) > 2 else 0
        token_in    = result[3] if len(result) > 3 else 0
        token_out   = result[4] if len(result) > 4 else 0

        if kind == "error":
            yield sse({"type": "error", "content": content_text})
            return

        # 如果没有实时 chunk，做打字机效果回显
        if not got_stream_chunks:
            for i in range(0, len(content_text), 5):
                yield sse({"type": "chunk", "content": content_text[i:i+5]})
                time.sleep(0.012)

        # 读取最新标题
        current_title = db.get_session_title(session_id) or "未命名会话"
        yield sse({
            "type": "done",
            "session_title": current_title,
            "token_total": token_total,
            "token_in": token_in,
            "token_out": token_out,
        })

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/approval_respond", methods=["POST"])
def approval_respond():
    data = request.json
    session_id = data.get("session_id")
    approved = data.get("approved", False)
    rt = _runtime.get(session_id)
    if rt and "approval_q" in rt:
        rt["approval_q"].put(approved)
        return jsonify({"status": "ok"})
    return jsonify({"error": "session not found"}), 404


@app.route("/api/sessions/<session_id>/reset_agent", methods=["POST"])
def reset_agent(session_id):
    """清除缓存的 Agent 实例，下次 chat 时用最新配置重建（不删会话历史）。"""
    rt = _runtime.get(session_id)
    if rt:
        rt["agent"] = None
        rt["web_search_enabled"] = None
        return jsonify({"status": "ok", "message": "Agent 已重置，下次对话将使用新实例"})
    return jsonify({"error": "session not found"}), 404



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 预热：从 SessionDB 加载已有的 web 会话到运行时槽位（不创建 Agent，只建槽）
    existing = _get_db().list_sessions_rich(source="web", limit=200)
    for row in existing:
        _ensure_runtime(row["id"])

    # 清除所有旧的 Agent 缓存实例
    # 原因：每次重启配置可能已变更（ephemeral_system_prompt / disabled_toolsets 等），
    # 旧实例不会感知到新配置，必须在下次 chat 时重建。
    for rt in _runtime.values():
        rt["agent"] = None
        rt["web_search_enabled"] = None

    if not existing:
        # 首次启动：建一个默认会话
        sid = str(uuid.uuid4())
        _get_db().create_session(sid, source="web")
        _get_db().set_session_title(sid, "默认会话")
        _ensure_runtime(sid)

    print("🔱 Hermes Agent Web UI（原生架构版）")
    print("🌐 访问地址: http://localhost:7860")
    app.run(host="0.0.0.0", port=7860, debug=False, threaded=True)
