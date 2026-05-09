"""可插拔记忆提供者的抽象基类。

记忆提供者赋予 Agent 跨会话持久回忆的能力。每次最多只能激活一个外部记忆提供者，
同时与始终开启的内置记忆（MEMORY.md / USER.md）并存。MemoryManager 负责强制执行此限制。

内置记忆永远作为第一个提供者处于激活状态，并且无法被移除。
外部提供者（如 Honcho, Hindsight, Mem0 等）是叠加的 —— 它们永远不会禁用内置存储。
每次只运行一个外部提供者，以防止工具模式（schema）过度膨胀和记忆后端之间的冲突。

注册方式:
  1. 内置: BuiltinMemoryProvider — 始终存在，不可移除。
  2. 插件: 位于 plugins/memory/<name>/ 目录下，通过 memory.provider 配置项激活。

生命周期 (由 MemoryManager 调用，在 run_agent.py 中集成):
  initialize()          — 连接、创建资源、预热
  system_prompt_block()  — 用于系统提示词的静态文本
  prefetch(query)        — 每回合前的后台回忆（预取）
  sync_turn(user, asst)  — 每回合后的异步写入
  get_tool_schemas()     — 暴露给大模型的工具模式（schemas）
  handle_tool_call()     — 分发并执行工具调用
  shutdown()             — 清理并安全退出

可选钩子 (覆盖这些方法以启用相关功能):
  on_turn_start(turn, message, **kwargs) — 每回合开始时的触发器，带有运行时上下文
  on_session_end(messages)               — 会话结束时的信息提取
  on_session_switch(new_session_id, **kwargs) — 进程运行中途的 session_id 切换
  on_pre_compress(messages) -> str       — 在上下文压缩前提取信息
  on_memory_write(action, target, content, metadata=None) — 镜像内置记忆的写入操作
  on_delegation(task, result, **kwargs)  — 在父 Agent 侧观察子 Agent 的工作结果
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MemoryProvider(ABC):
    """记忆提供者的抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """此提供者的简短标识符（例如 'builtin', 'honcho', 'hindsight'）。"""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """如果此提供者已配置好、具备凭证且已准备就绪，则返回 True。

        在 agent 初始化期间被调用，以决定是否激活该提供者。
        不应该在此处进行网络请求 —— 只需要检查本地配置和已安装的依赖即可。
        """

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """为当前会话进行初始化。

        在 agent 启动时调用一次。可能会创建资源（知识库、数据库表），
        建立连接，启动后台线程等。

        kwargs 必然包含:
          - hermes_home (str): 激活的 HERMES_HOME 目录路径。请使用此路径
            进行基于配置文件的存储，而不是硬编码使用 ``~/.hermes``。
          - platform (str): 运行平台，如 "cli", "telegram", "discord", "cron" 等。

        kwargs 可能包含:
          - agent_context (str): 运行上下文，如 "primary" (主), "subagent" (子), "cron" (定时), 或 "flush"。
            非主上下文（比如 cron 定时任务的系统提示词，如果记录进记忆会污染用户画像）时，
            提供者应该跳过写入操作。
          - agent_identity (str): 配置文件名称 (如 "coder")。用于对提供者身份进行作用域限制。
          - agent_workspace (str): 共享工作区名称 (如 "hermes")。
          - parent_session_id (str): 对于子 Agent，这是它父 Agent 的 session_id。
          - user_id (str): 平台用户标识符（用于网关会话）。
        """

    def system_prompt_block(self) -> str:
        """返回要包含在系统提示词中的文本块。

        在组装系统提示词时被调用。如果不想包含，返回空字符串。
        这用于提供【静态】的提供者信息（如使用说明、固定状态）。
        而动态预取的对话回忆上下文，是通过 prefetch() 单独注入的。
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """为即将到来的对话回合回忆相关的上下文。

        在每次调用大模型 API 之前调用。返回格式化好的文本以作为上下文注入，
        如果没有相关的回忆则返回空字符串。该方法的实现应该非常快 ——
        通常建议在后台线程中进行实际的搜索回忆，并在此处只返回缓存的结果。

        session_id 用于服务多个并发会话的提供者（如网关群聊、缓存 agent）。
        如果提供者不需要按会话作用域来隔离，可以忽略此参数。
        """
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """为【下一回合】排队执行后台回忆任务。

        在每回合对话结束后被调用。产生的结果将在下一回合的 prefetch() 中被消费。
        默认不执行任何操作 —— 支持后台预取的提供者应当覆盖实现此方法。
        """

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """将已完成的对话回合持久化保存到后端。

        在每回合结束后调用。该方法应当是非阻塞的 —— 
        如果后端数据库有延迟，请将其放入队列进行后台处理。
        """

    @abstractmethod
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """返回此提供者暴露给大模型的工具模式（tool schemas）。

        每个 schema 遵循 OpenAI 函数调用（function calling）的格式:
        {"name": "...", "description": "...", "parameters": {...}}

        如果此提供者没有提供工具（仅提供上下文功能），则返回空列表。
        """

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """处理该提供者所拥有工具的一次工具调用。

        必须返回一个 JSON 格式的字符串（作为工具的执行结果）。
        只有在 get_tool_schemas() 中返回的工具名称才会被路由到此方法。
        """
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """清理并安全退出 —— 刷新队列，关闭网络连接。"""

    # -- Optional hooks (override to opt in) ---------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """在每回合开始时被调用，并传入用户的消息。

        用于计算回合数、作用域管理或定期的维护任务。

        kwargs 可能包含: remaining_tokens, model, platform, tool_count。
        提供者只取自己需要的数据即可，忽略多余的参数。
        """

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """当一个会话结束时（显式退出或超时）被调用。

        用于会话结束时的信息提取、总结等。
        messages 包含了完整的对话历史记录。

        【注意】并不是每个回合后都会调用 —— 只有在真正的会话边界
        （如 CLI 退出、执行 /reset、或者网关会话过期）时才会被调用。
        """

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """当 agent 在运行中途切换 session_id 时被调用。

        触发场景包括：``/resume``（恢复）, ``/branch``（分支）, 
        ``/reset``（重置）, ``/new``（新建会话）（CLI 端），以及网关端的同等操作
        和上下文压缩时 —— 也就是任何会重新分配 ``AIAgent.session_id`` 
        但不销毁提供者的代码路径。

        如果提供者在 ``initialize()`` 时缓存了会话级别的状态（如
        ``_session_id``, ``_document_id``, 累积的回合缓冲区, 计数器），
        应当在此处更新或重置该状态，以确保后续的写入操作落在正确的会话记录中。

        参数
        ----------
        new_session_id:
            agent 刚刚切换到的新 session_id。
        parent_session_id:
            如果有关联的话，这是前一个 session_id —— 对于 ``/branch``（派生血缘）、
            上下文压缩（延续血缘）以及 ``/resume``（刚离开的会话）会有值。
            如果没有关联，则为空字符串。
        reset:
            当这是一个真正的新对话，而不是恢复现有对话时为 ``True``（由 ``/reset`` 或 ``/new`` 触发）。
            如果是 ``True``，提供者应该清空累积的会话缓冲区（如 ``_session_turns``, ``_turn_counter`` 等）。
            当为 ``False`` 时，代表逻辑上的对话在新的 session_id 下继续（如恢复、分支、压缩）。

        为了向后兼容，默认不执行任何操作。
        """

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """在上下文压缩准备丢弃旧消息之前调用。

        用于从即将被压缩丢弃的消息中提取深刻见解。
        messages 列表即为将要被总结/丢弃的完整历史列表。

        返回的文本将被包含在压缩总结的提示词中，以确保压缩器能够
        保留提供者提取出来的见解。如果没有需要提供的，返回空字符串
        （向后兼容的默认行为）。
        """
        return ""

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """当一个子 Agent（子智能体）完成任务时，在【父 Agent】端被调用。

        父 Agent 的记忆提供者会获得 "任务+结果" 这样一对数据，
        作为对“派发了什么任务，又收到了什么结果”的观察记录。
        子 Agent 自身是不会有记忆提供者会话的（因为 skip_memory=True）。

        task: 派发的任务指令（提示词）
        result: 子 Agent 给出的最终回复
        child_session_id: 子 Agent 的 session_id
        """

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """返回该提供者进行设置时所需的配置字段列表。

        用于 `hermes memory setup` 命令，在终端一步步引导用户配置。
        每个字段都是一个包含以下属性的字典:
          key:         配置键名（例如 'api_key', 'mode'）
          description: 人类可读的描述
          secret:      如果是隐私数据需要放入 .env 文件则为 True (默认: False)
          required:    是否必填 (默认: False)
          default:     默认值 (可选)
          choices:     可选的有效值列表 (可选)
          url:         引导用户去哪获取该凭证的链接 (可选)
          env_var:     用于存放 secret 的显式环境变量名称 (默认: 自动生成)

        如果不需要配置（比如仅限本地读取的提供者），请返回空列表。
        """
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """将非机密（非 secret）的配置写入该提供者的原生存储位置。

        在收集完用户输入后，由 `hermes memory setup` 调用。
        ``values`` 中只包含非机密的字段（机密信息会自动存入 .env）。
        ``hermes_home`` 是当前处于激活状态的 HERMES_HOME 目录路径。

        使用自有配置文件（如 JSON, YAML）的提供者应该重写此方法，
        将其写入预期位置。如果只依赖环境变量的提供者，则保留空实现即可。

        所有新的记忆提供者插件【必须】实现以下两者之一：
        - 对于原生的配置文件格式，实现 save_config()，或者
        - 仅使用环境变量（这种情况下 get_config_schema() 的字段应全部设置了 ``env_var``，且本方法不作任何操作）。
        """

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """当系统内置记忆工具写入了一条条目时被调用。

        action: 操作行为（'add'-添加, 'replace'-替换, 或 'remove'-移除）
        target: 目标对象（'memory' 或 'user'）
        content: 写入的内容
        metadata: 关于此次写入的结构化来源信息（如果有）。常见的键名包括：
          ``write_origin``, ``execution_context``, ``session_id``,
          ``parent_session_id``, ``platform``, 和 ``tool_name``。

        可利用此钩子将内置记忆的写入同步（镜像）到你自定义的后端中。
        """
