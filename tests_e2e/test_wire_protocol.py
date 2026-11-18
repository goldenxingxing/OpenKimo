from __future__ import annotations

import json
from typing import Any

from inline_snapshot import snapshot

from tests_e2e.wire_helpers import (
    collect_until_response,
    make_home_dir,
    make_work_dir,
    normalize_response,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def test_initialize_handshake(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        resp = send_initialize(wire)
        result = _as_dict(resp.get("result"))
        assert result.get("protocol_version") == "1.10"
        assert "slash_commands" in result
        assert normalize_response(resp) == snapshot({
    "result": {
        "protocol_version": "1.10",
        "server": {"name": "OpenKimo", "version": "1.49.0"},
        "slash_commands": [
            {
                "name": "init",
                "description": "Analyze the codebase and generate an `AGENTS.md` file",
                "aliases": [],
            },
            {
                "name": "compact",
                "description": "Compact the context (optionally with a custom focus, e.g. /compact keep db discussions)",
                "aliases": [],
            },
            {"name": "clear", "description": "Clear the context", "aliases": ["reset"]},
            {
                "name": "yolo",
                "description": "Toggle YOLO mode (auto-approve all actions)",
                "aliases": [],
            },
            {
                "name": "afk",
                "description": "Toggle afk mode (auto-dismiss AskUserQuestion, auto-approve tool calls)",
                "aliases": [],
            },
            {
                "name": "plan",
                "description": "Toggle plan mode. Usage: /plan [on|off|view|clear]",
                "aliases": [],
            },
            {
                "name": "add-dir",
                "description": "Add a directory to the workspace. Usage: /add-dir <path>. Run without args to list added dirs",
                "aliases": [],
            },
            {
                "name": "export",
                "description": "Export current session context to a markdown file",
                "aliases": [],
            },
            {
                "name": "import",
                "description": "Import context from a file or session ID",
                "aliases": [],
            },
            {
                "name": "skill:daily-report",
                "description": "写或更新工作日报时使用。本工作区日报的固定骨架、位置与命名、三扫描发现当天工作、写什么不写什么、写入与校验的全部规则都在这里。当用户说「形成今天的日报」「写日报」「更新日报」「日报」「补一下今天的日报」「整理今天的工作记录」，或在 output/reports/daily/ 下要新建/修改 D-YYYY-MM-DD.md 时触发。",
                "aliases": [],
            },
            {
                "name": "skill:dashi-ppt",
                "description": "制作 PPT、演示文稿、幻灯片、汇报材料时使用。Dashi PPT 基于预置视觉主题组合页面,生成可离线打开、可在浏览器编辑的 HTML 演示,支持导出 PPTX / PDF 文件。",
                "aliases": [],
            },
            {
                "name": "skill:diagram-design",
                "description": "Create branded architecture, IT current-state, flowchart, sequence, state machine, ER/data model, timeline, swimlane, quadrant, radar/spider, polar chart (polar/radial lollipop), loop/flywheel, nested, tree, org chart, layer stack, Venn, pyramid/funnel, treemap, heatmap, bar, waterfall, line, Gantt and scatter charts, high-level, process, medallion, data flow, DP integration, DP security matrix, Sankey, fishbone, Wardley map, kanban, user journey, deployment, dependency graph, UML class, story map, or database schema diagrams as HTML/SVG/PNG, with .drawio and .excalidraw import support, plus lifecycle phase maps and onboarding guidance.",
                "aliases": [],
            },
            {
                "name": "skill:docx-media-aware",
                "description": "处理任何 Microsoft Word `.docx` 输入任务时，先识别并处理文档中的图片、图注、截图和嵌入式媒体，再执行主任务——纯文本提取会漏掉图注里的错误、截图中的 UI 文字和流程图标注。适用于翻译审校、文档提取、内容总结、格式转换、合规审查等任何涉及 Word 文档的工作流。当输入包含 .docx/.doc 文件，或用户提及“审校翻译”、“翻译 review”、“提取文档内容”、“总结文档”、“转换格式”、“文档对比”、“Word 里有图片”、“文档里有截图”、“图注也要看”时触发。",
                "aliases": [],
            },
            {
                "name": "skill:email-manager",
                "description": "收发和管理邮件，支持查看未读邮件、搜索邮件、下载附件、发送带附件的邮件，并提供交互式配置向导。兼容Gmail、Outlook、163.com、126.com、QQ邮箱等主流服务商。当用户需要收发邮件、查看收件箱、搜索特定邮件、管理附件，或提及配置邮箱时触发。",
                "aliases": [],
            },
            {
                "name": "skill:grill-me",
                "description": "就某个计划、决策或想法对用户进行连环追问（grill），直到双方达成共识。当用户想压力测试自己的思路、设计方案或需求，或说出「grill me」「拷问我」「盘问我」「压力测试一下这个方案」「challenge my plan」等触发语时使用。源自 Matt Pocock 的开源技能（mattpocock/skills），提问形式已改为 AskUserQuestion 结构化提问。",
                "aliases": [],
            },
            {
                "name": "skill:humanizer-zh",
                "description": """\
去除文本中的 AI 生成痕迹。适用于编辑或审阅文本，使其听起来更自然、更像人类书写。
基于维基百科的"AI 写作特征"综合指南。检测并修复以下模式：夸大的象征意义、
宣传性语言、以 -ing 结尾的肤浅分析、模糊的归因、破折号过度使用、三段式法则、
AI 词汇、否定式排比、过多的连接性短语。
""",
                "aliases": [],
            },
            {
                "name": "skill:ppt-master",
                "description": "AI-driven presentation workflow for generating editable PPTX decks and slides, reconstructing page visuals, creating reusable Brand/Style/Layout/Deck workspaces, filling native PPTX templates, and enhancing finished PPTX files. Use when the user asks to create, generate, reconstruct, regenerate, beautify, redesign, template, fill, or enhance a presentation, PPT, PPTX, slide deck, or courseware — including adding narration or animation to one — requests a presentation-authored narrated/self-running video, or mentions ppt-master.\n",
                "aliases": [],
            },
            {
                "name": "skill:weekly-report",
                "description": "写工作周报时使用。本工作区周报的固定骨架、位置与命名、以日报为地基的周报合并法、写什么不写什么、对空档日补三扫描、写入与校验的全部规则都在这里。当用户说「形成这周的周报」「写周报」「出周报」「更新周报」，或要在 output/reports/weekly/ 下新建/修改 W-YYYY-MM-DD_to_YYYY-MM-DD.md 时触发。",
                "aliases": [],
            },
        ],
        "yolo": True,
        "afk": False,
        "approval_requests": [],
        "question_requests": [],
        "hooks": {
            "supported_events": [
                "PreToolUse",
                "PostToolUse",
                "PostToolUseFailure",
                "UserPromptSubmit",
                "Stop",
                "StopFailure",
                "SessionStart",
                "SessionEnd",
                "SubagentStart",
                "SubagentStop",
                "PreCompact",
                "PostCompact",
                "Notification",
            ],
            "configured": {},
        },
        "capabilities": {"supports_question": True},
    }
})
    finally:
        wire.close()


def test_initialize_external_tool_conflict(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    external_tools = [
        {
            "name": "Shell",
            "description": "Conflicts with built-in",
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        resp = send_initialize(wire, external_tools=external_tools)
        result = _as_dict(resp.get("result"))
        external_tools_result = _as_dict(result.get("external_tools"))
        rejected = external_tools_result.get("rejected")
        assert isinstance(rejected, list)
        assert any(isinstance(item, dict) and item.get("name") == "Shell" for item in rejected)
        assert normalize_response(resp) == snapshot({
    "result": {
        "protocol_version": "1.10",
        "server": {"name": "OpenKimo", "version": "1.49.0"},
        "slash_commands": [
            {
                "name": "init",
                "description": "Analyze the codebase and generate an `AGENTS.md` file",
                "aliases": [],
            },
            {
                "name": "compact",
                "description": "Compact the context (optionally with a custom focus, e.g. /compact keep db discussions)",
                "aliases": [],
            },
            {"name": "clear", "description": "Clear the context", "aliases": ["reset"]},
            {
                "name": "yolo",
                "description": "Toggle YOLO mode (auto-approve all actions)",
                "aliases": [],
            },
            {
                "name": "afk",
                "description": "Toggle afk mode (auto-dismiss AskUserQuestion, auto-approve tool calls)",
                "aliases": [],
            },
            {
                "name": "plan",
                "description": "Toggle plan mode. Usage: /plan [on|off|view|clear]",
                "aliases": [],
            },
            {
                "name": "add-dir",
                "description": "Add a directory to the workspace. Usage: /add-dir <path>. Run without args to list added dirs",
                "aliases": [],
            },
            {
                "name": "export",
                "description": "Export current session context to a markdown file",
                "aliases": [],
            },
            {
                "name": "import",
                "description": "Import context from a file or session ID",
                "aliases": [],
            },
            {
                "name": "skill:daily-report",
                "description": "写或更新工作日报时使用。本工作区日报的固定骨架、位置与命名、三扫描发现当天工作、写什么不写什么、写入与校验的全部规则都在这里。当用户说「形成今天的日报」「写日报」「更新日报」「日报」「补一下今天的日报」「整理今天的工作记录」，或在 output/reports/daily/ 下要新建/修改 D-YYYY-MM-DD.md 时触发。",
                "aliases": [],
            },
            {
                "name": "skill:dashi-ppt",
                "description": "制作 PPT、演示文稿、幻灯片、汇报材料时使用。Dashi PPT 基于预置视觉主题组合页面,生成可离线打开、可在浏览器编辑的 HTML 演示,支持导出 PPTX / PDF 文件。",
                "aliases": [],
            },
            {
                "name": "skill:diagram-design",
                "description": "Create branded architecture, IT current-state, flowchart, sequence, state machine, ER/data model, timeline, swimlane, quadrant, radar/spider, polar chart (polar/radial lollipop), loop/flywheel, nested, tree, org chart, layer stack, Venn, pyramid/funnel, treemap, heatmap, bar, waterfall, line, Gantt and scatter charts, high-level, process, medallion, data flow, DP integration, DP security matrix, Sankey, fishbone, Wardley map, kanban, user journey, deployment, dependency graph, UML class, story map, or database schema diagrams as HTML/SVG/PNG, with .drawio and .excalidraw import support, plus lifecycle phase maps and onboarding guidance.",
                "aliases": [],
            },
            {
                "name": "skill:docx-media-aware",
                "description": "处理任何 Microsoft Word `.docx` 输入任务时，先识别并处理文档中的图片、图注、截图和嵌入式媒体，再执行主任务——纯文本提取会漏掉图注里的错误、截图中的 UI 文字和流程图标注。适用于翻译审校、文档提取、内容总结、格式转换、合规审查等任何涉及 Word 文档的工作流。当输入包含 .docx/.doc 文件，或用户提及“审校翻译”、“翻译 review”、“提取文档内容”、“总结文档”、“转换格式”、“文档对比”、“Word 里有图片”、“文档里有截图”、“图注也要看”时触发。",
                "aliases": [],
            },
            {
                "name": "skill:email-manager",
                "description": "收发和管理邮件，支持查看未读邮件、搜索邮件、下载附件、发送带附件的邮件，并提供交互式配置向导。兼容Gmail、Outlook、163.com、126.com、QQ邮箱等主流服务商。当用户需要收发邮件、查看收件箱、搜索特定邮件、管理附件，或提及配置邮箱时触发。",
                "aliases": [],
            },
            {
                "name": "skill:grill-me",
                "description": "就某个计划、决策或想法对用户进行连环追问（grill），直到双方达成共识。当用户想压力测试自己的思路、设计方案或需求，或说出「grill me」「拷问我」「盘问我」「压力测试一下这个方案」「challenge my plan」等触发语时使用。源自 Matt Pocock 的开源技能（mattpocock/skills），提问形式已改为 AskUserQuestion 结构化提问。",
                "aliases": [],
            },
            {
                "name": "skill:humanizer-zh",
                "description": """\
去除文本中的 AI 生成痕迹。适用于编辑或审阅文本，使其听起来更自然、更像人类书写。
基于维基百科的"AI 写作特征"综合指南。检测并修复以下模式：夸大的象征意义、
宣传性语言、以 -ing 结尾的肤浅分析、模糊的归因、破折号过度使用、三段式法则、
AI 词汇、否定式排比、过多的连接性短语。
""",
                "aliases": [],
            },
            {
                "name": "skill:ppt-master",
                "description": "AI-driven presentation workflow for generating editable PPTX decks and slides, reconstructing page visuals, creating reusable Brand/Style/Layout/Deck workspaces, filling native PPTX templates, and enhancing finished PPTX files. Use when the user asks to create, generate, reconstruct, regenerate, beautify, redesign, template, fill, or enhance a presentation, PPT, PPTX, slide deck, or courseware — including adding narration or animation to one — requests a presentation-authored narrated/self-running video, or mentions ppt-master.\n",
                "aliases": [],
            },
            {
                "name": "skill:weekly-report",
                "description": "写工作周报时使用。本工作区周报的固定骨架、位置与命名、以日报为地基的周报合并法、写什么不写什么、对空档日补三扫描、写入与校验的全部规则都在这里。当用户说「形成这周的周报」「写周报」「出周报」「更新周报」，或要在 output/reports/weekly/ 下新建/修改 W-YYYY-MM-DD_to_YYYY-MM-DD.md 时触发。",
                "aliases": [],
            },
        ],
        "yolo": True,
        "afk": False,
        "approval_requests": [],
        "question_requests": [],
        "external_tools": {
            "accepted": [],
            "rejected": [{"name": "Shell", "reason": "conflicts with builtin tool"}],
        },
        "hooks": {
            "supported_events": [
                "PreToolUse",
                "PostToolUse",
                "PostToolUseFailure",
                "UserPromptSubmit",
                "Stop",
                "StopFailure",
                "SessionStart",
                "SessionEnd",
                "SubagentStart",
                "SubagentStop",
                "PreCompact",
                "PostCompact",
                "Notification",
            ],
            "configured": {},
        },
        "capabilities": {"supports_question": True},
    }
})
    finally:
        wire.close()


def test_external_tool_call(tmp_path) -> None:
    tool_args = json.dumps({"path": "README.md"})
    tool_call = json.dumps({"id": "tc-1", "name": "ext_tool", "arguments": tool_args})
    scripts = [
        "\n".join(
            [
                "text: calling external tool",
                f"tool_call: {tool_call}",
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    external_tools = [
        {
            "name": "ext_tool",
            "description": "External tool",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        send_initialize(wire, external_tools=external_tools)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run external tool"},
            }
        )

        def handle_request(msg: dict[str, Any]) -> dict[str, Any]:
            params = msg.get("params")
            payload = params.get("payload") if isinstance(params, dict) else None
            tool_call_id = payload.get("id") if isinstance(payload, dict) else None
            assert isinstance(tool_call_id, str)
            return {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {
                    "tool_call_id": tool_call_id,
                    "return_value": {
                        "is_error": False,
                        "output": "Opened",
                        "message": "Opened README.md",
                        "display": [],
                    },
                },
            }

        resp, messages = collect_until_response(wire, "prompt-1", request_handler=handle_request)
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot([
    {
        "method": "event",
        "type": "TurnBegin",
        "payload": {"user_input": "run external tool", "todos": None},
    },
    {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
    {
        "method": "event",
        "type": "ContentPart",
        "payload": {"type": "text", "text": "calling external tool"},
    },
    {
        "method": "event",
        "type": "ToolCall",
        "payload": {
            "type": "function",
            "id": "tc-1",
            "function": {"name": "ext_tool", "arguments": '{"path": "README.md"}'},
            "extras": None,
        },
    },
    {
        "method": "event",
        "type": "StatusUpdate",
        "payload": {
            "context_usage": None,
            "context_tokens": None,
            "max_context_tokens": None,
            "token_usage": None,
            "message_id": None,
            "plan_mode": False,
            "thinking_suppressed": False,
            "yolo": None,
            "mcp_status": None,
            "waiting_on_background": None,
        },
    },
    {
        "method": "request",
        "type": "ToolCallRequest",
        "payload": {
            "id": "tc-1",
            "name": "ext_tool",
            "arguments": '{"path": "README.md"}',
        },
    },
    {
        "method": "event",
        "type": "ToolResult",
        "payload": {
            "tool_call_id": "tc-1",
            "return_value": {
                "is_error": False,
                "output": "Opened",
                "message": "Opened README.md",
                "display": [],
                "extras": None,
            },
        },
    },
    {"method": "event", "type": "StepBegin", "payload": {"n": 2}},
    {
        "method": "event",
        "type": "ContentPart",
        "payload": {"type": "text", "text": "done"},
    },
    {
        "method": "event",
        "type": "StatusUpdate",
        "payload": {
            "context_usage": None,
            "context_tokens": None,
            "max_context_tokens": None,
            "token_usage": None,
            "message_id": None,
            "plan_mode": False,
            "thinking_suppressed": False,
            "yolo": None,
            "mcp_status": None,
            "waiting_on_background": None,
        },
    },
    {"method": "event", "type": "TurnEnd", "payload": {"interrupt_reason": None}},
])
    finally:
        wire.close()


def test_prompt_without_initialize(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello without init"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot([
    {
        "method": "event",
        "type": "TurnBegin",
        "payload": {"user_input": "hi", "todos": None},
    },
    {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
    {
        "method": "event",
        "type": "ContentPart",
        "payload": {"type": "text", "text": "hello without init"},
    },
    {
        "method": "event",
        "type": "StatusUpdate",
        "payload": {
            "context_usage": None,
            "context_tokens": None,
            "max_context_tokens": None,
            "token_usage": None,
            "message_id": None,
            "plan_mode": False,
            "thinking_suppressed": False,
            "yolo": None,
            "mcp_status": None,
            "waiting_on_background": None,
        },
    },
    {"method": "event", "type": "TurnEnd", "payload": {"interrupt_reason": None}},
])
    finally:
        wire.close()
