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
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_FILE = MEMORY_DIR / "USER.md"

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
                model="deepseek-v4-pro",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0.3,
            )
            summary = resp.choices[0].message.content.strip()

            # 确保目录存在
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)

            # 追加到 MEMORY.md
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            section = f"\n\n## [{now}] 会话 {session_id[:8]} 摘要\n\n{summary}\n"
            with open(MEMORY_FILE, "a", encoding="utf-8") as f:
                f.write(section)

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

def get_or_create_agent(session_id: str):
    if session_id not in sessions:
        return None
    sd = sessions[session_id]
    if sd["agent"] is None:
        agent = AIAgent(
            provider="deepseek",
            model="deepseek-v4-pro",
            quiet_mode=True,
            platform="web",
            session_id=session_id,
            skip_context_files=True,
            # 方案C：开启原生记忆体系（不再 skip_memory）
            max_iterations=10,
        )
        sd["agent"] = agent
    return sd["agent"]


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
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify(result)


@app.route("/api/sessions/new", methods=["POST"])
def new_session():
    sid = str(uuid.uuid4())
    sessions[sid] = {
        "agent": None,
        "title": "新会话",
        "messages": [],
        "created_at": datetime.now().isoformat(),
        "summarized_count": 0,
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
    result = {}
    for fname, fpath in [("MEMORY.md", MEMORY_FILE), ("USER.md", USER_FILE)]:
        result[fname] = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
    return jsonify(result)


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    # strip() 消除前后空白行，这是气泡"虚大"的根本原因
    message: str = data.get("message", "").strip()
    session_id: str = data.get("session_id", "")
    attachment: dict = data.get("attachment")

    if not session_id or session_id not in sessions:
        return jsonify({"error": "无效的会话 ID"}), 400
    if not message and not attachment:
        return jsonify({"error": "消息不能为空"}), 400

    # Build full prompt (with attachment injected)
    full_message = message
    if attachment:
        if attachment.get("is_text"):
            full_message = (
                f"[文件: {attachment['name']}]\n```\n{attachment['content'][:8000]}\n```\n\n"
                + message
            )
        else:
            full_message = f"[图片已上传: {attachment['name']}] {message}"

    # Persist user message
    sessions[session_id]["messages"].append({
        "role": "user",
        "content": message,
        "attachment": attachment.get("name") if attachment else None,
    })

    # Auto-title from first message
    if len(sessions[session_id]["messages"]) == 1:
        sessions[session_id]["title"] = message[:30] + ("…" if len(message) > 30 else "")

    result_q: queue.Queue = queue.Queue()
    status_q: queue.Queue = queue.Queue()

    def run_agent():
        try:
            status_q.put(("status", "⚙️ 初始化 Agent 工具集..."))
            agent = get_or_create_agent(session_id)
            if agent is None:
                result_q.put(("error", "会话不存在"))
                return
            status_q.put(("status", "🧠 加载记忆与上下文..."))
            status_q.put(("status", "🔗 连接 DeepSeek API..."))
            response = agent.chat(full_message)
            result_q.put(("ok", response or "（无回复）"))
        except Exception as exc:
            result_q.put(("error", str(exc)))

    t = threading.Thread(target=run_agent, daemon=True)
    t.start()

    def generate():
        while True:
            # 先消费状态队列
            try:
                _, msg = status_q.get_nowait()
                yield f"data: {json.dumps({'type': 'status', 'content': msg})}\n\n"
                continue
            except queue.Empty:
                pass

            try:
                kind, content = result_q.get(timeout=0.3)
                break
            except queue.Empty:
                if not t.is_alive():
                    kind, content = "error", "Agent 线程意外退出"
                    break
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

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

        yield f"data: {json.dumps({'type': 'done', 'session_title': sessions[session_id]['title']})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
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
