#!/usr/bin/env python3
"""
Hermes MCP Server — 反重力 AI Agent 通过此服务器调用本地工具
协议：Model Context Protocol (JSON-RPC 2.0 over stdio)
"""

import json
import sys
import subprocess
import os
import traceback
from pathlib import Path

TOOLS = [
    {
        "name": "read_file",
        "description": "读取文件内容",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（绝对路径或 ~/ 开头）"},
                "limit": {"type": "integer", "description": "最多读取行数", "default": 200}
            },
            "required": ["path"]
        }
    },
    {
        "name": "search_files",
        "description": "按文件名或内容搜索文件",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索模式（glob 或正则）"},
                "path": {"type": "string", "description": "搜索目录", "default": "."},
                "target": {"type": "string", "enum": ["files", "content"], "description": "搜索目标", "default": "files"}
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "run_python",
        "description": "执行 Python 代码，返回 stdout",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"},
                "timeout": {"type": "integer", "description": "超时秒数", "default": 30}
            },
            "required": ["code"]
        }
    },
    {
        "name": "run_bash",
        "description": "执行 Bash 命令（Git Bash），返回输出",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "workdir": {"type": "string", "description": "工作目录"},
                "timeout": {"type": "integer", "description": "超时秒数", "default": 30}
            },
            "required": ["command"]
        }
    },
    {
        "name": "list_directory",
        "description": "列出目录内容",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径", "default": "."},
                "pattern": {"type": "string", "description": "过滤模式，如 *.py", "default": ""}
            },
            "required": []
        }
    }
]


def resolve_path(p: str) -> str:
    """解析 ~ 和相对路径"""
    p = p.strip()
    if p.startswith("~"):
        p = str(Path.home() / p[1:].lstrip("/\\"))
    return os.path.abspath(p)


def handle_read_file(params):
    path = resolve_path(params["path"])
    limit = params.get("limit", 200)
    if not os.path.isfile(path):
        return {"error": f"文件不存在: {path}"}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        content = "".join(lines[:limit])
        if limit < total:
            content += f"\n... (已截断，共 {total} 行，显示前 {limit} 行)"
        return {"content": content, "total_lines": total, "path": path}
    except Exception as e:
        return {"error": str(e)}


def handle_search_files(params):
    pattern = params["pattern"]
    search_path = params.get("path", ".")
    target = params.get("target", "files")
    search_path = resolve_path(search_path)

    if target == "files":
        # 用 glob 匹配文件名
        result = []
        # 递归查找
        for p in Path(search_path).rglob(pattern):
            result.append(str(p))
        return {"matches": result[:100], "total": len(result)}
    else:
        # 用 grep 搜索文件内容
        cmd = ["grep", "-r", "-n", pattern, search_path, "--include=*.py", "--include=*.js", "--include=*.ts",
               "--include=*.json", "--include=*.yaml", "--include=*.yml", "--include=*.md", "--include=*.txt",
               "-l"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, shell=False)
            matches = [m for m in result.stdout.strip().split("\n") if m]
            return {"matches": matches[:100], "total": len(matches)}
        except subprocess.TimeoutExpired:
            return {"error": "搜索超时"}
        except Exception as e:
            return {"error": str(e)}


def handle_run_python(params):
    code = params["code"]
    timeout = params.get("timeout", 30)
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"error": f"执行超时（{timeout}s）"}
    except Exception as e:
        return {"error": str(e)}


def handle_run_bash(params):
    command = params["command"]
    timeout = params.get("timeout", 30)
    workdir = params.get("workdir", None)

    # 检测 Git Bash 路径
    bash_paths = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ]
    bash = None
    for b in bash_paths:
        if os.path.isfile(b):
            bash = b
            break
    if not bash:
        bash = "bash"

    try:
        result = subprocess.run(
            [bash, "-c", command],
            capture_output=True, text=True, timeout=timeout,
            cwd=resolve_path(workdir) if workdir else None
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"error": f"执行超时（{timeout}s）"}
    except Exception as e:
        return {"error": str(e)}


def handle_list_directory(params):
    path = resolve_path(params.get("path", "."))
    pattern = params.get("pattern", "")
    try:
        entries = os.listdir(path)
        if pattern:
            import fnmatch
            entries = [e for e in entries if fnmatch.fnmatch(e, pattern)]
        items = []
        for e in sorted(entries):
            full = os.path.join(path, e)
            is_dir = os.path.isdir(full)
            size = os.path.getsize(full) if os.path.isfile(full) else 0
            items.append({"name": e, "is_dir": is_dir, "size": size})
        return {"items": items, "path": path}
    except Exception as e:
        return {"error": str(e)}


HANDLERS = {
    "read_file": handle_read_file,
    "search_files": handle_search_files,
    "run_python": handle_run_python,
    "run_bash": handle_run_bash,
    "list_directory": handle_list_directory,
}


def send_message(msg):
    """向 stdout 发送 JSON-RPC 消息"""
    line = json.dumps(msg, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main():
    # 先发服务器信息到 stderr 以便调试
    sys.stderr.write("[hermes-mcp] MCP Server starting...\n")
    sys.stderr.write(f"[hermes-mcp] Python: {sys.executable}\n")
    sys.stderr.flush()

    initialized = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[hermes-mcp] JSON parse error: {e}\n")
            sys.stderr.flush()
            continue

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        # 处理通知（无 id）
        if method == "notifications/initialized":
            initialized = True
            sys.stderr.write("[hermes-mcp] Client initialized notification received\n")
            sys.stderr.flush()
            continue

        if not msg_id:
            continue

        try:
            if method == "initialize":
                send_message({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "0.5.0",
                        "capabilities": {
                            "tools": {}
                        },
                        "serverInfo": {
                            "name": "hermes-mcp",
                            "version": "1.0.0"
                        }
                    }
                })
                sys.stderr.write("[hermes-mcp] Initialized successfully\n")
                sys.stderr.flush()
                initialized = True

            elif method == "tools/list":
                send_message({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": TOOLS
                    }
                })

            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {})

                handler = HANDLERS.get(tool_name)
                if not handler:
                    send_message({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"未知工具: {tool_name}"}
                    })
                    continue

                result = handler(tool_args)
                send_message({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result, ensure_ascii=False, indent=2)
                            }
                        ]
                    }
                })

            else:
                send_message({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"未知方法: {method}"}
                })

        except Exception as e:
            sys.stderr.write(f"[hermes-mcp] Error handling {method}: {traceback.format_exc()}\n")
            sys.stderr.flush()
            send_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": str(e)}
            })


if __name__ == "__main__":
    main()
