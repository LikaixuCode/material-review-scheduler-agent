# 素材审核与发布排期 Agent

这是一个单 Agent 命令行项目。Claude Agent SDK 驱动 GLM 根据素材、规则和时段查询结果选择工具；业务代码校验审核、冲突、确认和草稿保存。项目只处理本地合成数据，不对接真实发布平台。

## 环境与安装

- Python 3.12，使用 `uv` 安装依赖；锁定的 Claude Agent SDK 为 0.2.175，安装包包含 Claude Code 运行时。
- 离线模式无需密钥；真实模型模式使用智谱 GLM Coding Plan 的 Anthropic 兼容接口。

```bash
uv sync --python 3.12
uv run pytest -q
```

## 离线运行

使用独立数据库运行正常与冲突场景：

```bash
uv run python -m src.main run --mode mock --input examples/normal.input.json --db .runtime/normal.sqlite3
uv run python -m src.main run --mode mock --input examples/conflict.input.json --schedule data/schedules/busy.json --db .runtime/conflict.sqlite3
```

冲突场景可通过以下命令明确确认当前草稿建议的时间：

```bash
uv run python -m src.main confirm --request-id case-m01 --material-id M01 --time 2026-11-05T11:15:00+08:00 --db .runtime/conflict.sqlite3
```

`--schedule` 只在数据库首次创建时导入已有排期。比较不同排期场景时，请使用不同的数据库文件。相同请求对同一个数据库重复提交会返回同一份草稿。

模拟保存失败：

```bash
uv run python -m src.main run --mode mock --input examples/save-failure.input.json --simulate-save-failure --db .runtime/failure.sqlite3
```

Mock 是本地脚本驱动的流程验证，不是模型决策的证明。

## 真实模型运行

复制 `.env.example` 为 `.env`，在本地填写 `ANTHROPIC_AUTH_TOKEN`，然后将环境变量载入当前终端。不要提交 `.env`。

```bash
cp .env.example .env
# 在本地编辑 .env，填写智谱密钥
set -a
. ./.env
set +a
uv run python scripts/sdk_smoke.py
uv run python -m src.main run --mode glm --input examples/normal.input.json --db .runtime/glm-normal.sqlite3
```

默认地址是 `https://open.bigmodel.cn/api/anthropic`，默认模型是 `glm-5.3-flash`。SDK 只加载本项目注册的四个业务工具，最多运行 12 个 Agent 轮次。模型调用失败时返回 `incomplete`，不会自动切换为 Mock。

智谱官方支持 [Claude Code 接入 Coding Plan](https://docs.bigmodel.cn/cn/coding-plan/tool/claude)，其 [FAQ](https://docs.bigmodel.cn/cn/coding-plan/faq) 对自建应用另有使用标准 API 的说明。SDK 与本项目场景的套餐适用范围以智谱实际规则为准；接口成功不等同于套餐适用范围已经确认。

## 输入和结果

输入中的每项素材包含 `material_id`、`channel`、带时区的 `requested_at`、`duration_minutes` 和 `hard_time`。时长未指定时默认为 60 分钟，支持 1 至 1440 分钟。演示规则和素材位于 `data/`。

输出包含请求状态、逐项审核状态、风险原因、原定和建议时间、确认状态、草稿保存状态、草稿 ID、SDK 结束状态及 Trace。Trace 记录工具参数、返回摘要、后续动作和简短依据，不记录密钥和模型内部思维内容。

正常草稿在本地模拟排期中占位；替代时段草稿先保持待确认且不占位，确认前会重新检查。所有结果都只是草稿。

## 评审入口

- [需求与验收](docs/01-requirements.md)、[设计与取舍](docs/02-solution.md)、[测试与实际结果](docs/03-evaluation.md)
- [AI 使用](docs/04-ai-usage.md)、[复盘](docs/05-retrospective.md)
- `examples/*.glm.output.json`：真实 GLM 运行结果；`examples/*.mock.output.json`：离线运行结果；每个输出内含 Trace。

业务示例参考 [题目交付目录示例](https://github.com/lwz1357/agent-campus-submission-example)。SDK 工具注册参考 [Claude Agent SDK 官方文档](https://code.claude.com/docs/en/agent-sdk/custom-tools)；排期工具的职责边界参考 [Postmill](https://github.com/postmill-ai/postmill-app/blob/main/libraries/nestjs-libraries/src/chat/tools/integration.schedule.post.ts)，没有复制其代码。
