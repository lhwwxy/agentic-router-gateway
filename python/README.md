# agentic-router（Python / LiteLLM 适配）

LiteLLM 的学习型路由 hook + 纯核心库。行为规范见 [spec/ROUTING_SPEC.md](../spec/ROUTING_SPEC.md)。

## 安装

```bash
pip install -e ".[litellm]"   # 本地开发安装
# 或直接把 agentic_router/ 目录拷进你的项目
```

## SDK 用法

```python
import litellm
from agentic_router.litellm_hook import AgenticRouterHook

hook = AgenticRouterHook(
    mode="shadow",   # "shadow" 只记录不改写；"active" 真实换模型
    tier_models={    # 三档 → LiteLLM model_name（deployment 里的名字）
        "fast": "gpt-4o-mini",
        "mid": "gpt-4o",
        "strong": "o3",
    },
)
litellm.callbacks = [hook]
```

运行时切换模式 / 查看统计：

```python
hook.activate()          # 影子观察够了，接管
hook.shadow()            # 回到只记录
hook.stats()             # {"mode", "decisions", "ucbArms", "linSamples", "knnPool", "exp3Weights", ...}
```

## Proxy（config.yaml）用法

```yaml
model_list:
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
  - model_name: o3
    litellm_params:
      model: openai/o3
      api_key: os.environ/OPENAI_API_KEY

litellm_settings:
  callbacks: custom_callbacks.agentic_router_handler   # 指向返回 AgenticRouterHook 实例的工厂
```

`custom_callbacks.py`：

```python
from agentic_router.litellm_hook import AgenticRouterHook

handler = AgenticRouterHook(
    mode="shadow",
    tier_models={"fast": "gpt-4o-mini", "mid": "gpt-4o", "strong": "o3"},
)
```

> 个别 LiteLLM 版本需要在 `litellm_settings` 里开 `modify_params: true` 才允许 hook 原地改写请求模型。

## 数据与运维

- 飞轮状态与审计日志：`${AGENTIC_ROUTER_HOME:-~/.agentic-router}/`（`state.json` / `decisions.jsonl` / `rewards.jsonl`，全部本地明文可审计）
- 删除目录即重置学习状态
- `cost_multiplier`：把 LiteLLM 的 `response_cost`（USD）换算成奖励成本单位（默认 1.0 原样使用）

## 纯核心（不经 LiteLLM）

```python
from agentic_router import Router

r = Router(home="~/.agentic-router", mode="shadow",
           model_tiers={"providers": [{"provider": "p1"}],
                        "tiers": {"fast": ["p1/lite"], "mid": ["p1/base"], "strong": ["p1/pro"]}})
d = r.decide("帮我总结这篇文章的要点", "p1")
r.settle(d["id"], success=True, dt_ms=5200, cost_yuan=0.003)
r.correct(d["id"], 0)   # 宿主侧拿到 👍/👎 时修正奖励
```

## 测试

```bash
python3 -m unittest discover -s tests
```

golden 数值与 `js/test/core.test.js` 同源，改公式三处同步。
