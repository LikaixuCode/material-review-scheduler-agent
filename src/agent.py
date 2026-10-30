import json
import os

from .business import BusinessTools


def run_mock(service: BusinessTools) -> dict:
    """离线脚本只演示流程，不代表模型决策能力。"""
    for item in service.request.items:
        material_id = item.material_id
        service.call("read_material", {"material_id": material_id, "reason": "读取请求中的素材"})
        review = service.call("check_rules", {"material_id": material_id, "reason": "检查素材资料与审核规则"})
        if review["status"] != "passed":
            continue
        slot = service.call("query_slots", {"material_id": material_id, "mode": "requested", "reason": "检查用户要求的时段"})
        proposed_at = item.requested_at.isoformat()
        if not slot["available"]:
            alternatives = service.call("query_slots", {"material_id": material_id, "mode": "alternatives", "reason": "原定时段冲突，查询可选时段"})
            if not alternatives["candidates"]:
                continue
            proposed_at = alternatives["candidates"][0]
        service.call("save_draft", {"material_id": material_id, "proposed_at": proposed_at, "reason": "保存草稿，变更时段等待确认"})
    return service.output("mock", {"status": "completed", "source": "scripted_mock"})


async def run_glm(service: BusinessTools, max_turns: int = 12) -> dict:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ClaudeSDKError,
        ResultError,
        ResultMessage,
        ToolUseBlock,
        create_sdk_mcp_server,
        tool,
    )

    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        return service.output("glm", {"status": "incomplete", "reason": "missing_credential"})

    def response(name: str, args: dict) -> dict:
        result = service.call(name, args)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "is_error": not result["ok"]}

    @tool("read_material", "读取指定素材的元数据。", {"material_id": str, "reason": str})
    async def read_material(args):
        return response("read_material", args)

    @tool("check_rules", "检查素材所需资料和审核风险。", {"material_id": str, "reason": str})
    async def check_rules(args):
        return response("check_rules", args)

    @tool("query_slots", "查询用户要求的时段，或在冲突时查询替代时段。", {
        "type": "object",
        "properties": {
            "material_id": {"type": "string"},
            "mode": {"type": "string", "enum": ["requested", "alternatives"]},
            "reason": {"type": "string"},
        },
        "required": ["material_id", "mode", "reason"],
        "additionalProperties": False,
    })
    async def query_slots(args):
        return response("query_slots", args)

    @tool("save_draft", "保存本地排期草稿；与原要求不同的时段会被标记为待确认。", {"material_id": str, "proposed_at": str, "reason": str})
    async def save_draft(args):
        return response("save_draft", args)

    server = create_sdk_mcp_server(name="review", tools=[read_material, check_rules, query_slots, save_draft])
    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=[f"mcp__review__{name}" for name in ("read_material", "check_rules", "query_slots", "save_draft")],
        mcp_servers={"review": server},
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=max_turns,
        model=os.environ.get("GLM_MODEL", "glm-5.3-flash"),
        env={
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "API_TIMEOUT_MS": "60000",
        },
        system_prompt=(
            "你是素材审核与排期助手。使用提供的工具处理每项素材，不发布内容。"
            "先读取素材并检查规则；资料不足或有风险时停止处理该素材。"
            "对通过审核的素材查询 requested 时段：如果可用，保存该时段草稿；"
            "如果冲突，查询 alternatives，选择一个可用时段保存为待确认草稿。"
            "不得把建议时间称为已确认或已发布。每次调用工具的 reason 用一句话说明公开的决策依据，"
            "不要输出内部思维过程。根据工具真实结果完成任务。"
        ),
    )
    terminal = {"status": "incomplete", "reason": "no_result"}
    model_calls = []
    reported_models = set()
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(json.dumps(service.request.model_dump(mode="json"), ensure_ascii=False))
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    if message.model:
                        reported_models.add(message.model)
                    model_calls.extend(block.name for block in message.content if isinstance(block, ToolUseBlock))
                elif isinstance(message, ResultMessage):
                    terminal = {
                        "status": "completed" if not message.is_error and message.subtype == "success" else "incomplete",
                        "reason": message.terminal_reason or message.subtype,
                        "turns": message.num_turns,
                        "model": options.model,
                    }
                    if terminal["status"] == "incomplete":
                        terminal["api_error_status"] = message.api_error_status
                        terminal["detail"] = (message.result or "").replace(token, "[REDACTED]")[:500]
    except ResultError as exc:
        terminal = {"status": "incomplete", "reason": exc.terminal_reason or exc.subtype or "sdk_result_error", "model": options.model}
    except ClaudeSDKError as exc:
        terminal = {"status": "incomplete", "reason": type(exc).__name__, "model": options.model}
    except Exception as exc:
        terminal = {"status": "incomplete", "reason": type(exc).__name__, "model": options.model}
    if not model_calls and terminal["status"] == "completed":
        terminal = {**terminal, "status": "incomplete", "reason": "no_model_tool_calls"}
    terminal["reported_models"] = sorted(reported_models)
    return service.output("glm", terminal)
