# agentic-router-gateway（new-api 前置网关）

零依赖（Node ≥18）的 OpenAI 兼容前置网关，为 [new-api](https://github.com/QuantumNous/new-api) 加上学习型模型路由。**new-api 不需要任何改动**。

```
客户端 ──► 本网关(:8787) ──改写 model──► new-api ──► 各渠道
                │
                └── 状态/延迟/usage成本 → 质量奖励 → 飞轮（~/.agentic-router/）
```

## 特性

- **模型目录自动发现**：启动时拉取 new-api `/v1/models`，按 `tierOf` 自动归三档（fast/mid/strong），TTL 10 分钟自动刷新（渠道增删模型自愈）；也可用 `TIERS` 静态配置
- **流式透传**：逐分片转发不整包缓冲（SSE 首字延迟不受影响），跨分片按行扫描末帧 usage 计入成本
- **令牌透传**：客户端自带的 `Authorization` 原样转发（保留 new-api 的令牌/额度管理），未带时用 `NEWAPI_API_KEY`
- **shadow / active 双模式**：默认 shadow 只记录不改写，`/healthz` 看数据，切 active 接管
- **通用透传**：`/v1/models`、`/v1/embeddings` 等非 chat 路径原样代理

## 启动

```bash
NEWAPI_BASE_URL=http://127.0.0.1:3000 \
NEWAPI_API_KEY=sk-xxx \
MODE=shadow \
node gateway/server.mjs
```

客户端把 `base_url` 从 new-api 换成 `http://127.0.0.1:8787` 即可。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEWAPI_BASE_URL` | 必填 | new-api 地址（如 `http://127.0.0.1:3000`） |
| `NEWAPI_API_KEY` | — | 目录探测用；客户端不带令牌时的兜底令牌 |
| `MODE` | `shadow` | `active` 后真实改写模型 |
| `PORT` / `HOST` | `8787` / `127.0.0.1` | 监听 |
| `EXPLORATION` | `0.08` | 档位级 ε 探索率（学习必需；`0` 关闭，代价是弱样本档位学不到） |
| `TIERS` | 自动发现 | 静态档位覆盖：`'{"fast":["m"],"mid":["m"],"strong":["m"]}'` |
| `PRICES` | 内置通用价 | 元/百万 token：`'{"fast":{"in":1,"out":4}}'` |
| `TIERS_TTL_MS` | `600000` | 目录探测周期 |
| `AGENTIC_ROUTER_HOME` | `~/.agentic-router` | 飞轮状态/审计日志目录 |

## 运维

```bash
curl http://127.0.0.1:8787/healthz
# {"mode":"shadow","decisions":128,"ucbArms":18,"linSamples":128,
#  "knnPool":128,"exp3Weights":{...},"catalog":{"fast":3,"mid":5,"strong":2}}
```

- 切 active：重启时 `MODE=active`（或改 `~/.agentic-router/state.json` 的 `mode` 字段）
- 路由审计：`~/.agentic-router/decisions.jsonl`（每次决策带理由）/ `rewards.jsonl`（每次结算的奖励分解）
- 重置学习状态：删除 `~/.agentic-router/` 目录

## 与 new-api 的信号边界

网关层能采到：状态、延迟、usage 成本（流式末帧 + 非流式）。**拿不到** new-api 内部的渠道重试次数与渠道级错误（`retries` 记 0）。要吃到完整信号需把核心移植进 new-api 本体（Go，见 [../UPSTREAM.md](../UPSTREAM.md) 思路）。

## 开机自启（macOS launchd 示例）

```xml
<!-- ~/Library/LaunchAgents/ai.agentic-router-gateway.plist -->
<dict>
  <key>KeepAlive</key><true/>
  <key>ProgramArguments</key>
  <array>
    <string>node</string><string>/path/to/agentic-router-gateway/gateway/server.mjs</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NEWAPI_BASE_URL</key><string>http://127.0.0.1:3000</string>
    <key>NEWAPI_API_KEY</key><string>sk-xxx</string>
  </dict>
</dict>
```

## 测试

```bash
cd gateway && npm test   # 7 个用例：目录构建 / SSE 解析 / e2e(mock new-api) 双模式
```
