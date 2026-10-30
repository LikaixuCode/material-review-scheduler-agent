"""Check that the SDK can call a local tool using the configured model."""

import asyncio
import json
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)


@tool("check_slot", "查询给定时间是否可用", {"slot": str})
async def check_slot(args):
    return {"content": [{"type": "text", "text": json.dumps({"slot": args["slot"], "available": False})}]}


async def main():
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        print(json.dumps({"status": "missing_credential"}))
        return 1

    server = create_sdk_mcp_server(name="smoke", tools=[check_slot])
    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=["mcp__smoke__check_slot"],
        mcp_servers={"smoke": server},
        strict_mcp_config=True,
        setting_sources=[],
        max_turns=4,
        model=os.environ.get("GLM_MODEL", "glm-5.3-flash"),
        env={
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "API_TIMEOUT_MS": "60000",
        },
        system_prompt="先调用 check_slot 查询用户给出的时段，再根据工具结果简短回答。",
    )
    events = []
    terminal = None
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query("请查询示例时段的可用状态。")
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            events.append({"tool": block.name, "input": block.input})
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            events.append({"tool_result": str(block.content)[:500]})
                elif isinstance(message, ResultMessage):
                    terminal = {
                        "subtype": message.subtype,
                        "is_error": message.is_error,
                        "num_turns": message.num_turns,
                        "terminal_reason": message.terminal_reason,
                        "api_error_status": message.api_error_status,
                        "result": str(message.result or "").replace(token, "[REDACTED]")[:600],
                        "errors": [str(error).replace(token, "[REDACTED]")[:600] for error in message.errors or []],
                    }
    except Exception as exc:
        print(json.dumps({"status": "error", "exception": type(exc).__name__, "message": str(exc).replace(token, "[REDACTED]")[:500]}, ensure_ascii=False))
        return 1

    success = bool(terminal and not terminal["is_error"] and any("tool" in event for event in events))
    print(json.dumps({"status": "ok" if success else "failed", "events": events, "terminal": terminal}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
