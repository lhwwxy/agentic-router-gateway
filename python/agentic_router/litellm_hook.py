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

import os
import threading
import time

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


def _destination_budget_exhausted(user_api_key_dict, model):
    """键级按模型预算（model_max_budget / model_spend）由 proxy 对**请求模型**
    在本 hook 之前执行。改写不能落在预算已耗尽的目标模型上——
    直接请求它会被 proxy 拒绝。缺失/非数值的花费数据不阻塞（只阻塞已知超支的分桶）。"""
    if not user_api_key_dict or not model:
        return False
    max_budget = getattr(user_api_key_dict, "model_max_budget", None)
    if not isinstance(max_budget, (int, float)) or max_budget <= 0:
        return False  # 该 key 没配按模型预算
    spend = getattr(user_api_key_dict, "model_spend", None)
    if isinstance(spend, dict) and isinstance(spend.get(model), (int, float)):
        return spend[model] >= max_budget
    return False


def _key_allowed_models(user_api_key_dict):
    """收集 proxy 附在 key 上的模型白名单（key / team / 别名表）。

    空集合 = 不受限（或在 hook 层无从判断）。active 模式的改写必须落在白名单内，
    否则保留请求原模型——学习型路由不能越权换模型。"""
    allowed = set()
    for attr in ("models", "allowed_team_models"):
        v = getattr(user_api_key_dict, attr, None)
        if isinstance(v, (list, tuple)):
            allowed.update(m for m in v if isinstance(m, str) and m)
    aliases = getattr(user_api_key_dict, "team_model_aliases", None)
    if isinstance(aliases, dict):
        allowed.update(m for m in aliases.values() if isinstance(m, str) and m)
    return allowed


_ID_KEYS = ("litellm_call_id", "request_id")
_NESTED_SOURCES = ("data", "request_data", "request_obj", "original_request",
                   "kwargs", "body", "litellm_params")


def _find_call_id(*sources):
    """在 hook 的各种参数形态里找 call id：顶层 kwargs，或嵌套一层的
    data / request_data / request_obj 等 dict 里。找不到返回 None。"""
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in _ID_KEYS:
            if src.get(k):
                return src[k]
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in _NESTED_SOURCES:
            nested = src.get(k)
            if isinstance(nested, dict):
                for kk in _ID_KEYS:
                    if nested.get(kk):
                        return nested[kk]
    return None


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
    """学习型路由 hook。所有路由决策与奖励落盘在 AGENTIC_ROUTER_HOME（默认 ~/.agentic-router）。

    shadow 模式只记录决策与结果观测，不喂奖励飞轮（请求由原模型服务，
    其结果不能归因于路由档位）；active 模式改写模型时会先校验 key/team
    的模型白名单，不通过则保留原模型。"""

    MAX_PENDING = 500    # 结算匹配不上的请求也不能无限堆积
    PENDING_TTL = 900.0  # 秒；插入时顺带淘汰过期条目

    def __init__(self, router=None, mode="shadow", tier_models=None,
                 cross_provider=True, home=None, cost_multiplier=1.0,
                 dt_penalty=True, guarded_models=None, **router_kwargs):
        if router is not None:
            self.router = router
        else:
            model_tiers = _normalize_tiers(tier_models)
            self.router = Router(home=home, mode=mode, model_tiers=model_tiers,
                                 cross_provider=cross_provider, **router_kwargs)
        self.cost_multiplier = cost_multiplier
        # 模型级 guardrail（PII/拦截）在 proxy 选定模型后才挂接：把请求改写到
        # 受保护模型会跳过它的输入检查。把这些模型列出来（构造参数或
        # AGENTIC_ROUTER_GUARDED_MODELS 环境变量，逗号分隔），active 模式拒绝
        # 路由进入它们——即使 key 的白名单允许。
        self.guarded_models = {m for m in (guarded_models or []) if isinstance(m, str) and m}
        self.guarded_models.update(
            m.strip() for m in os.environ.get("AGENTIC_ROUTER_GUARDED_MODELS", "").split(",")
            if m.strip())
        self._lock = threading.RLock()
        self._pending = {}  # call id → {id, ts, mode, train}

    def _track(self, request_id, decision_id, mode, train):
        with self._lock:
            now = time.time()
            if len(self._pending) > self.MAX_PENDING // 2:
                for k in [k for k, v in self._pending.items()
                          if now - v["ts"] > self.PENDING_TTL]:
                    del self._pending[k]
            self._pending[request_id] = {"id": decision_id, "ts": now,
                                         "mode": mode, "train": train}
            while len(self._pending) > self.MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))  # 丢最旧

    def _take(self, *sources):
        request_id = _find_call_id(*sources)
        if not request_id:
            return None
        with self._lock:
            pending = self._pending.pop(request_id, None)
        return pending

    # ---- 请求前：分类 + 决策（+ active 模式改写模型）----
    async def async_pre_call_hook(self, user_api_key_dict=None, cache=None,
                                  data=None, call_type=None, **kwargs):
        if not isinstance(data, dict):
            return data
        # 敏感数据路由（PROXY_SensitiveDataRoutingHandler）可能已把会话钉到
        # 私有模型。该钉住是合规边界，必须保持权威：完全跳过 agentic 路由
        # （不决策、不训练），否则本 hook 的档位改写会把携带敏感历史的
        # 后续消息送到公网档位模型。
        if (data.get("metadata") or {}).get("sensitive_data_routing_applied"):
            return data
        messages = data.get("messages") or []
        text = _last_user_text(messages)
        if not text:
            return data
        request_id = _find_call_id(data, kwargs) or id(data)
        with self._lock:
            decision = self.router.decide(text, provider="default")
        train = self.router.mode == "active"
        if self.router.mode == "active" and decision.get("model"):
            allowed = _key_allowed_models(user_api_key_dict)
            blocked = None
            if allowed and decision["model"] not in allowed:
                blocked = "key-model-restriction"
            elif decision["model"] in self.guarded_models:
                blocked = "guardrail-model-restriction"
            elif _destination_budget_exhausted(user_api_key_dict, decision["model"]):
                blocked = "budget-model-restriction"
            if blocked is None:
                data["model"] = decision["model"]
                data.setdefault("metadata", {})["agentic_router"] = {
                    "decision_id": decision["id"],
                    "tier": decision["tier"],
                    "expert": decision["chosen"],
                    "reason": decision["reason"],
                }
            else:
                # key/team 受限，或目标模型受 guardrail 保护：保留原模型并记录。
                # 请求由原模型服务，其结果不能归因被拒档位——标记不训练。
                train = False
                data.setdefault("metadata", {})["agentic_router"] = {
                    "decision_id": decision["id"],
                    "tier": decision["tier"],
                    "model": decision["model"],
                    "blocked": blocked,
                    "reason": decision["reason"],
                }
        self._track(request_id, decision["id"], self.router.mode, train)
        return data

    # ---- 成功：延迟/成本 → 奖励（active）----
    async def async_post_call_success_hook(self, user_api_key_dict=None, kwargs=None,
                                           response_obj=None, start_time=None,
                                           end_time=None, **kw):
        pending = self._take(kwargs, kw)
        if pending is None:
            return
        # train（含决策时模式快照）为假：shadow 或被拒改写——
        # 结果属于原模型，不训练飞轮
        if not pending["train"]:
            return
        decision_id = pending["id"]
        dt_ms = None
        if start_time is not None and end_time is not None:
            dt_ms = (end_time - start_time).total_seconds() * 1000
        cost, source = _extract_usage_cost(kwargs, response_obj)
        with self._lock:
            self.router.settle(
                decision_id, success=True, dt_ms=dt_ms,
                cost_yuan=(cost or 0.0) * self.cost_multiplier,
            )

    # ---- 失败：reward=0（active）----
    async def async_post_call_failure_hook(self, user_api_key_dict=None,
                                           request_obj=None, original_exception=None,
                                           user_model=None, start_time=None,
                                           end_time=None, **kwargs):
        pending = self._take(request_obj, kwargs)
        if pending is None:
            return
        if not pending["train"]:
            return  # shadow 或被拒改写：结果属于原模型，不训练
        decision_id = pending["id"]
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
