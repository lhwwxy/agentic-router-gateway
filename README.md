# agentic-router-gateway

学习型智能模型路由（Agentic Router）——可挂载到 **LiteLLM** 与 **Higress** 的网关插件。

把 [dsh-agentic-router](https://github.com/lhwwxy/dsh-agentic-router)（DeepSeek Harness 宿主内插件）的学习核心抽出来，做成跨网关形态：**规则表 + UCB + LinUCB + k-NN 四个专家并行推荐，EXP3 元选择器决定听谁的，每次路由的质量奖励（延迟/成本/错误）回传飞轮，让路由随使用越来越准**。

## 它和别的路由有什么不同

| 能力 | 常见网关路由 | agentic-router-gateway |
|---|---|---|
| 决策依据 | 静态规则 / 价格表 / 近期延迟 | 任务分类 + 复杂度特征 + 历史经验 |
| 学习闭环 | 无（或仅 Thompson 采样单算法） | 4 专家 + EXP3 元选择器，奖励反馈全链路落盘 |
| 冷启动 | — | 规则表兜底，专家逐个"毕业"接管（LinUCB 50 样本 / k-NN 30 经验） |
| 上线安全 | — | shadow 模式先影子观察，数据够了再 active 接管 |
| 可解释性 | — | 每次决策带理由落盘（JSONL 审计日志） |

## 仓库结构

```
├── spec/ROUTING_SPEC.md   # 跨语言行为规范（移植对照的唯一事实源）
├── js/                    # 参考实现（Node ESM，零依赖）+ golden 测试
├── python/                # LiteLLM 适配（agentic_router 包 + CustomLogger hook）
├── go/                    # Higress 适配（Wasm 插件，TinyGo 构建）
├── gateway/               # new-api 前置网关（零依赖 Node，OpenAI 兼容反代）
└── tools/                 # 仿真回放器（无真实流量时验证飞轮收敛）
```

三个实现通过 **golden 数值对拍**锁定行为一致性：`js/test/core.test.js`、`python/tests/test_core.py`、`go/core/core_test.go` 中的数值用例一一同源，改公式必须三处同步。

## 快速开始

### LiteLLM（Python）

```bash
pip install -e "python/[litellm]"
```

SDK 内联使用：

```python
import litellm
from agentic_router.litellm_hook import AgenticRouterHook

hook = AgenticRouterHook(
    mode="shadow",                 # 先影子观察；飞轮数据够了改 "active"
    tier_models={
        "fast": "gpt-4o-mini",
        "mid": "gpt-4o",
        "strong": "o3",
    },
)
litellm.callbacks = [hook]

resp = litellm.completion(model="gpt-4o", messages=[...])  # 路由器自动接管选档
print(hook.stats())
```

Proxy（config.yaml）方式与运维细节见 [python/README.md](python/README.md)。

### Higress（Go Wasm）

```yaml
# 在路由/McpServer 的插件配置中：
shadow: true                # 观察模式
tierModels:
  fast: "qwen-flash"
  mid: "qwen-plus"
  strong: "qwen-max"
```

构建与安装（TinyGo → plugin.wasm → Higress 插件市场/独立路由）见 [go/README.md](go/README.md)。

### new-api（Node 前置网关）

new-api **零改动**：网关插在客户端与 new-api 之间，自动发现模型清单归三档，active 模式改写 `model` 字段，流式透传 + usage 成本结算。

```bash
NEWAPI_BASE_URL=http://127.0.0.1:3000 \
NEWAPI_API_KEY=sk-xxx \
MODE=shadow \
node gateway/server.mjs          # 客户端 base_url 改指 http://127.0.0.1:8787
```

环境变量、launchd 自启、信号边界说明见 [gateway/README.md](gateway/README.md)。

### 本地开发

```bash
cd js && npm test                      # node --test
cd gateway && npm test                 # e2e（mock new-api）
cd python && python3 -m unittest discover -s tests
cd go && go test ./core/...            # 需要本机 Go 工具链
```

### 无真实流量时的验证：仿真回放

没有生产流量也能验证学习行为——`tools/sim.mjs` 构造一个"模拟世界"（每类任务×档位预设 成功率/延迟/成本），打合成流量看飞轮是否收敛到各类型的最优档位：

```bash
node tools/sim.mjs --turns=20000      # SEED=7 可复现特定随机序列
```

```
各类型收敛情况（后1/4流量的档位分布 vs 模拟世界最优档）:
  类型        fast  mid strong  最优档  收敛?
  code         0%   1%  99%   strong  ✓
  write        1%  93%   6%  mid    ✓
  qa          98%   1%   1%   fast   ✓
  ...
```

收敛行为由 `tools/test/sim.test.mjs` 在 CI 里锁定（多种子断言）。**这个仿真器在开发中实打实抓出过三个算法缺陷**（EXP3 权重无 γ 混合导致的永久锁死、归一化下溢、贪婪专家主导后探索停滞），修法已同步进三语言实现与 spec。

## 工作原理

```
请求 → 分类(<1ms, 规则) → 特征向量[1,复杂度,长度,类型×6]
     → 专家推荐: rule / UCB / LinUCB / k-NN
     → EXP3 元选择器采样(按累计权重)
     → 档位解析(fast/mid/strong → 具体模型, 支持跨 provider)
     → [active] 改写请求模型 → 转发
响应 → 延迟 + 用量成本 + 状态 → 质量奖励(1 - 各项罚分)
     → 奖励回传四专家 + EXP3 权重更新(飞轮)
```

奖励函数（详见 [spec/ROUTING_SPEC.md](spec/ROUTING_SPEC.md)）：

```
reward = 1 − 工具失败罚(0.2/次, 上限0.6) − 重试罚(0.2/次, 上限0.4)
         − 成本罚(20×元, 上限0.5) − 延迟罚(>30s:0.1, >90s:0.3)   [钳到 0..1]
```

## 已知限制（网关形态的信号盲区）

- **人类 👍/👎 反馈**：网关层拿不到（dsh 宿主内版本有 `messageFeedback` 通道）；网关版提供 `correct()` API，供宿主侧有反馈通道时调用
- **流式响应成本**：SSE 场景聚合 usage 拿不到时成本记 0（延迟/状态信号仍有效）
- **Higress shared data**：多 worker last-writer-wins，重实例场景建议接 Redis（见 go/README.md）

## 路线图

- [x] 三语言核心 + golden 对拍
- [x] LiteLLM CustomLogger 集成（pre_call 改写模型 / post_call 奖励）
- [x] Higress Wasm 插件（body 模型改写 + 响应结算）
- [x] new-api 前置网关（模型目录自动发现 + 流式透传 + usage 结算）
- [ ] LiteLLM `CustomRoutingStrategyBase` deployment 级选择（实验）
- [ ] Higress Redis 持久化
- [ ] 上游合并（[UPSTREAM.md](UPSTREAM.md)）

## License

MIT © lhl
