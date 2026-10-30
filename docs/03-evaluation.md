# 测试与实际结果

## 自动化测试

在 Python 3.12.7 下执行 `uv run --locked python -m pytest -q`，结果为 **30 passed**。完整输出见 [测试记录](../examples/test-results.txt)。测试覆盖正常排期、信息缺失、规则风险、区间冲突、重复提交、保存失败、确认前排期变化、请求参数不一致、模型无工具调用和执行上限。

[审查回归测试](../tests/test_review_regressions.py)还覆盖：已有排期快照更新及失败回滚、无效输入不替换快照、旧草稿冲突、同一批建议不重叠、缺失信息与风险同时出现、混合批次未完成，以及中断 Trace。

## 真实 GLM 运行

使用 Claude Agent SDK 0.2.175、其内置 Claude Code 运行时 2.1.314，以及智谱 Anthropic 兼容接口。表中运行均由 SDK 返回 `glm-5.3-flash` 模型标识。素材、规则、已有排期和存储均为本地合成数据。

| 场景 | 实际结果 | 输入与输出 |
| --- | --- | --- |
| 正常排期 | 审核通过，保存原定时段草稿 | [输入](../examples/normal.input.json)、[真实输出与 Trace](../examples/normal.glm.output.json) |
| 时段冲突 | 查询候选时段并保存待确认草稿 | [输入](../examples/conflict.input.json)、[真实输出与 Trace](../examples/conflict.glm.output.json) |
| 资料不足 | 提示缺失信息，没有保存草稿 | [输入](../examples/missing.input.json)、[真实输出与 Trace](../examples/missing.glm.output.json) |
| 模拟保存失败 | 标记保存失败和未完成，数据库没有草稿 | [输入](../examples/save-failure.input.json)、[真实输出与 Trace](../examples/save-failure.glm.output.json) |
| Agent 轮次上限 | 返回未完成，Trace 标记中断及原因 | [输入](../examples/normal.input.json)、[真实输出与 Trace](../examples/limit.glm.output.json) |

正常和冲突场景使用相同输入与相互独立的数据库，差别是提供的已有排期快照。两个 Trace 展示了模型根据查询结果采取不同后续动作。保存失败采用本地故障注入，程序以实际存储结果报告未完成。

真实上限场景传入 `--max-turns 1`，SDK 返回 `max_turns`，同时报告 `turns: 2`，Trace 中实际执行了两个工具。SDK 的轮次不是逐个工具调用数；因此这里以中断状态和实际 Trace 为准，不将 `turns` 解释为严格的工具次数上限。相关机制参见 [SDK Agent 循环文档](https://code.claude.com/docs/en/agent-sdk/agent-loop)。

本地确认操作保留了[确认成功](../examples/confirm.output.json)和[确认前时段被占用](../examples/confirm-conflict.output.json)的实际结果；后者使用[更新后的排期快照](../examples/confirm-occupied-schedule.json)。

## 离线结果

离线结果由脚本驱动，并非真实模型输出：

- [正常](../examples/normal.mock.output.json)、[信息缺失](../examples/missing.mock.output.json)、[规则风险](../examples/risk.mock.output.json)
- [冲突](../examples/conflict.mock.output.json)、[重复提交](../examples/duplicate.mock.output.json)、[模拟保存失败](../examples/save-failure.mock.output.json)
- [快照变化导致旧草稿冲突](../examples/schedule-update.mock.output.json)，使用[更新的排期](../examples/updated-schedule.json)；[Mock 调用次数上限](../examples/limit.mock.output.json)

每个输出文件包含实际工具调用 Trace。重复提交的输出与第一次正常运行使用同一数据库，草稿 ID 一致。

## 限制与用时

真实调用时，本机原有代理路由无法连接智谱接口；验证使用了只对本次进程生效的临时本地转发，没有修改系统网络配置。接口运行成功不证明自建业务 Agent 在 Coding Plan 中的套餐适用范围；没有核验套餐账单。SDK 对模型名称输出过识别提示，但上述模型调用和工具返回实际完成。

这次验证不覆盖真实内容审核、真实排期系统、线上部署或长期稳定性。已有排期仅以调用方最后提供的快照为准，不会自动同步外部系统；更新后发生冲突的草稿会保留，本项目没有自动改期或撤销草稿的命令。

项目初次开发与本地验证为 2026-10-30 21:27 至 21:52（北京时间），约 25 分钟；不包含后续 GitHub 仓库发布。审查问题的修复与本地验证为 22:11 至 22:22，约 11 分钟；不包含后续推送及远端复核。
