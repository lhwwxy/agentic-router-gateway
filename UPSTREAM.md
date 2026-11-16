# 上游合并策略（LiteLLM / Higress / new-api）

目标：把学习型路由能力合并进三个上游项目。分两步走——**先独立仓库跑通 + 开 issue 对齐维护者预期，再提 PR**。直接冷提 PR 大概率被搁置。

## 通用原则

1. **先 issue 后 PR**：在上游开 issue 说明动机（现有路由策略均无学习闭环/质量奖励反馈），附本仓库 benchmark 与设计文档链接，问维护者想要的落地形态（内置 vs 外部插件）
2. **每个上游单独分支**，只移植该上游需要的最小代码（不要带 monorepo 结构）
3. **遵守上游代码风格 / 测试框架 / PR 模板**，CI 必须绿
4. **功能开关默认关闭**：上游合并不改变任何现有用户默认行为

## LiteLLM

- 仓库：https://github.com/BerriAI/litellm（Python）
- 目标位置：`litellm/integrations/agentic_router.py`（CustomLogger hook）
- 形态：`litellm_settings.agentic_router: true` 开启；`tier_models` 配置三档模型
- 前置工作：
  - [ ] fork + 分支 `feat/agentic-router`
  - [ ] 把 `python/agentic_router/{classify,strategies,meta,router}.py` 合成单文件放进 `litellm/integrations/`（上游习惯相关逻辑内聚在一个模块）
  - [ ] 对齐代码风格：black + isort + pydantic 配置类（上游大量使用 pydantic BaseModel 做配置）
  - [ ] 测试放 `tests/`（pytest，参照 `test_litellm_router.py` 的组织方式），golden 数值直接搬
  - [ ] 文档：`docs/my-website/docs/` 下新增 routing 文档段落
- 替代路径（更稳）：先作为独立包收录进 LiteLLM 文档的 "Custom Integrations / Callbacks" 列表——零合并风险，先拿用户
- 实验路径：`CustomRoutingStrategyBase` 做 deployment 级选择（同档多 deployment 间按学习权重挑），LiteLLM 的 routing 文档有自定义策略接入说明；API 随版本变动，需对照当时的 `litellm/router.py`

## Higress

- 仓库：https://github.com/higress-group/higress（Go，插件在 `plugins/wasm-go/extensions/`）
- 目标位置：`plugins/wasm-go/extensions/agentic-router/`（main.go + README.md，中英双语配置文档）
- 前置工作：
  - [ ] fork + 分支 `feat/agentic-router-plugin`
  - [ ] 代码 = `go/main.go` + `go/core/`（合并进插件目录），import path 改为上游相对路径
  - [ ] 遵守 wasm-go 插件规范：`wrapper.SetCtx` 生命周期、gjson 解析配置、`tinygo build -o plugin.wasm -target=wasi` 可构建
  - [ ] README 按上游插件模板：用途 / 配置 / 构建说明 / 路由级示例（zh + en 两份，上游插件 README 均双语）
  - [ ] 验证：本地 higress-consumer 或 e2e 用例覆盖 shadow 与 active 两模式
- 需要在 issue 里和维护者确认的点：
  - 与现有 `ai-proxy` modelMapping、`model-router` 插件的职责边界（我们做档位学习决策，它们做转发执行）
  - shared data 跨 worker 状态共享是否可接受，或是否要求接 Redis
  - 是否要求把飞轮状态外置（wasm 沙箱无本地磁盘）

## new-api（QuantumNous）

- 仓库：https://github.com/QuantumNous/new-api（Go + Gin，AGPLv3，one-api 增强 fork）
- **前置网关已可用**（`gateway/`，零改动对接）——先跑 shadow 攒数据
- 合并目标位置：`controller/relay` 的渠道分发环节（解析 `model` 后、选渠道前调用 `Decide()` 改写；relay 结束处结算）
- **信号优势是三个宿主里最厚的**：new-api 自己就采集渠道重试次数、预扣费/流式结算的真实 quota（可直接换算 costYuan）、耗时、错误状态——`rewardOf` 的罚分项几乎全部可精确喂入
- 技术可行性：new-api 是 Go 项目，`go/core`（纯标准库）可直接复用，移植成本三个宿主里最低
- 协议：AGPLv3 与 MIT 核心兼容（MIT 可并入 AGPL 项目）
- issue 要点：现有渠道优先级/权重是静态配置，无学习闭环；学习型选模 + 质量奖励飞轮 + shadow 灰度，附本仓库 golden 测试与前置网关运行数据

## 建议的 issue 措辞要点

- 痛点：现有 6 种 routing strategy 全是"请求前一次性决策"，无结果反馈闭环；Autorouter v2 有 Thompson 采样但不校验结果质量
- 方案：4 专家 + EXP3 + 质量奖励飞轮（附 spec/ROUTING_SPEC.md）
- 差异化：shadow 模式灰度、决策可解释落盘、correct API 供宿主反馈通道
- 证据：dsh-agentic-router 生产运行数据 + 本仓库 golden 对拍测试

## 时间线建议

1. 本仓库发布 + README 完备（本周）
2. 两个上游各开 issue 沟通（+1 周）
3. 按维护者反馈决定：独立收录 / 最小 PR（先 shadow-only 模式）/ 完整合并
