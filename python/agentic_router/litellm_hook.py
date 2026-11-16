"""LiteLLM 集成：AgenticRouterHook（CustomLogger）。

挂到 LiteLLM（SDK 或 proxy）上实现学习型路由：
  - 请求前（async_pre_call_hook）：任务分类 + 路由决策；active 模式下原地改写
    data["model"]（LiteLLM 会在同 model_name 的 deployment 集里完成实际转发）
  - 成功后（async_post_call_success_hook）：延迟 / 用量成本 → 质量奖励回传飞轮
  - 失败后（async_post_call_failure_hook）：reward = 0 结算

SDK 用法::

    import litellm
    from agentic_router.litellm_hook import AgenticRouterHook

    hook = AgenticRouterHook(
        mode="shadow",                      # 先影子观察，数据够了切 "active"
        tier_models={
            "fast": "gpt-4o-mini",
            "mid": "gpt-4o",
            "strong": "o3",
        },
    )
    litellm.callbacks = [hook]

Proxy（config.yaml）用法见 python/README.md。
"""

import threading

from .router import Router

try:  # litellm 是可选依赖：纯核心（测试/对拍）不需要它
    from litellm.integrations.custom_logger import CustomLogger
    HAS_LITELLM = True
except ImportError:  # pragma: no cover
    CustomLogger = object
    HAS_LITELLM = False


def _normalize_tiers(tier_models):
    """把 {"fast": "model", ...} / LiteLLM 模型名列表规整成核心的 model_tiers 结构。"""
    providers = [{"provider": "default"}]
    tiers = {}
    for tier, models in (tier_models or {}).items():
        if isinstance(models, str):
            models = [models]
        tiers[tier] = [m if "/" in m else "default/" + m for m in (models or [])]
    return {"providers": providers, "tiers": tiers}


def _last_user_text(messages):
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, list):
            parts = [p.get("text", "") for p in c
                     if isinstance(p, dict) and p.get("type") == "text"]
            joined = "\n".join(x for x in parts if x)
            if joined.strip():
                return joined
    return ""


def _extract_usage_cost(kwargs, response_obj):
    """优先 LiteLLM 的 response_cost（USD 计），否则从 usage 估算。返回 (cost, source)。"""
    cost = kwargs.get("response_cost")
    if isinstance(cost, (int, float)):
        return float(cost), "litellm-response-cost"
    usage = None
    try:
        usage = getattr(response_obj, "usage", None) or (kwargs.get("usage") or None)
    except Exception:
        usage = None
    if usage is None:
        return 0.0, "no-usage"
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    # 通用档位兜底价（元/百万 token）粗估：fast 1/4, mid 2/8, strong 3/16
    return (2 * prompt + 8 * completion) / 1e6, "usage-generic-mid"


class AgenticRouterHook(CustomLogger):
    """学习型路由 hook。所有路由决策与奖励落盘在 AGENTIC_ROUTER_HOME（默认 ~/.agentic-router）。"""

    def __init__(self, router=None, mode="shadow", tier_models=None,
                 cross_provider=True, home=None, cost_multiplier=1.0,
                 dt_penalty=True, **router_kwargs):
        if router is not None:
            self.router = router
        else:
            model_tiers = _normalize_tiers(tier_models)
            self.router = Router(home=home, mode=mode, model_tiers=model_tiers,
                                 cross_provider=cross_provider, **router_kwargs)
        self.cost_multiplier = cost_multiplier
        self._lock = threading.RLock()
        self._pending = {}  # litellm_call_id / request_id → decision_id

    # ---- 请求前：分类 + 决策（+ active 模式改写模型）----
    async def async_pre_call_hook(self, user_api_key_dict=None, cache=None,
                                  data=None, call_type=None, **kwargs):
        if not isinstance(data, dict):
            return data
        messages = data.get("messages") or []
        text = _last_user_text(messages)
        if not text:
            return data
        request_id = data.get("litellm_call_id") or data.get("request_id") or id(data)
        original_model = data.get("model")
        with self._lock:
            decision = self.router.decide(text, provider="default")
            self._pending[request_id] = (decision["id"], original_model)
        if self.router.mode == "active" and decision.get("model"):
            data["model"] = decision["model"]
            data.setdefault("metadata", {})["agentic_router"] = {
                "decision_id": decision["id"],
                "tier": decision["tier"],
                "expert": decision["chosen"],
                "reason": decision["reason"],
            }
        return data

    # ---- 成功：延迟/成本 → 奖励 ----
    async def async_post_call_success_hook(self, user_api_key_dict=None, kwargs=None,
                                           response_obj=None, start_time=None,
                                           end_time=None, **kw):
        kwargs = kwargs or {}
        request_id = kwargs.get("litellm_call_id") or kwargs.get("request_id")
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        decision_id, _original = pending
        dt_ms = None
        if start_time is not None and end_time is not None:
            dt_ms = (end_time - start_time).total_seconds() * 1000
        cost, source = _extract_usage_cost(kwargs, response_obj)
        with self._lock:
            self.router.settle(
                decision_id, success=True, dt_ms=dt_ms,
                cost_yuan=(cost or 0.0) * self.cost_multiplier,
            )

    # ---- 失败：reward=0 ----
    async def async_post_call_failure_hook(self, user_api_key_dict=None,
                                           request_obj=None, original_exception=None,
                                           user_model=None, start_time=None,
                                           end_time=None, **kwargs):
        kwargs = kwargs or {}
        request_id = kwargs.get("litellm_call_id") or kwargs.get("request_id")
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        decision_id, _original = pending
        with self._lock:
            self.router.settle(decision_id, success=False)

    # ---- 运维辅助 ----
    def stats(self):
        with self._lock:
            s = self.router.state
            return {
                "mode": s["mode"],
                "decisions": s["seq"],
                "ucbArms": len(s["ucb"]["arms"]),
                "linSamples": s["lin"]["total"],
                "knnPool": len(s["knn"]["pool"]),
                "exp3Weights": dict(s["exp3"]["weights"]),
                "pending": len(self._pending),
            }

    def activate(self):
        with self._lock:
            self.router.set_mode("active")

    def shadow(self):
        with self._lock:
            self.router.set_mode("shadow")
