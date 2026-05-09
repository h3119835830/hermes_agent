# 用 Python 正确重写 server.py 中的 generate() 函数
# 策略：定义一个 sse() 辅助，让 yield 行只 yield 变量，彻底回避 f-string 内嵌 \n\n 的转义地狱

import re

with open("web_ui/server.py", encoding="utf-8") as f:
    content = f.read()

# 找到 generate() 函数的起止行，整块替换
OLD_GENERATE = '''    def generate():
        got_stream_chunks = False
        while True:
            # 消费事件队列
            while True:
                try:
                    ev = event_q.get_nowait()
                    ev_type = ev[0]
                    if ev_type == "status":
                        yield f"data: {json.dumps({'type': 'status', 'content': ev[1]})}'''

# 用标记定位生成函数的开始和结束
start_marker = "    def generate():"
end_marker = "    return Response("

start_idx = content.index(start_marker)
end_idx = content.index(end_marker)

NEW_GENERATE = '''    def generate():
        SSE = "\\n\\n"   # SSE 帧分隔符，避免在 f-string 内嵌 \\n 引发转义混乱

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

'''

fixed = content[:start_idx] + NEW_GENERATE + content[end_idx:]

with open("web_ui/server.py", "w", encoding="utf-8") as f:
    f.write(fixed)

print("generate() rewritten successfully")
