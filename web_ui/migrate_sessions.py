"""
一次性迁移脚本：将 sessions_data.json 里的历史会话导入 Hermes SessionDB。
运行方式：
    cd C:/Users/happy/Desktop/herms_agent
    venv/Scripts/python web_ui/migrate_sessions.py
"""
import sys, json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import io, contextlib
with contextlib.redirect_stderr(io.StringIO()):
    from hermes_state import SessionDB
    from hermes_constants import get_hermes_home

OLD_FILE = Path(__file__).parent / "sessions_data.json"

if not OLD_FILE.exists():
    print("❌ 找不到 sessions_data.json，跳过迁移。")
    sys.exit(0)

with open(OLD_FILE, encoding="utf-8") as f:
    old_data: dict = json.load(f)

db = SessionDB()
migrated = 0
skipped  = 0

for sid, sd in old_data.items():
    messages = sd.get("messages", [])
    if not messages:
        skipped += 1
        continue

    # 解析 created_at（ISO 字符串）
    created_at_str = sd.get("created_at", "")
    try:
        created_ts = datetime.fromisoformat(created_at_str).timestamp()
    except Exception:
        created_ts = None

    # 在 SessionDB 里创建这个会话（若已存在则忽略）
    db.create_session(sid, source="web")

    # 设置标题
    title = sd.get("title") or "未命名会话"
    db.set_session_title(sid, title)

    # 写入所有消息
    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content") or ""
        if role not in ("user", "assistant") or not content.strip():
            continue
        db.append_message(sid, role, content=content)

    # 写入 Token 用量
    token_in  = sd.get("token_in", 0) or 0
    token_out = sd.get("token_out", 0) or 0
    if token_in or token_out:
        db.update_token_counts(sid, input_tokens=token_in, output_tokens=token_out, absolute=True)

    migrated += 1
    print(f"  ✅ [{sid[:8]}] {title!r}  ({len(messages)} 条消息)")

print(f"\n迁移完成：{migrated} 个会话导入成功，{skipped} 个空会话已跳过。")
print("现在可以重启 server.py，历史会话将在侧边栏正常显示。")
