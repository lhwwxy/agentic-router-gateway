"""四专家策略 + 奖励计算 + 飞轮更新（纯函数，state 由调用方传入）。

与 js/src/strategies.js 逐行对拍；数值行为以 golden 测试锁定。
"""

import math
import re

FAST_RE = re.compile(r"flash|lite|mini|small|fast|air|nano|tiny|light")
STRONG_RE = re.compile(r"pro|max|ultra|plus|large|premium|reason|thinking")

DEFAULT_PRICES = {
    "*": {
        "fast": {"in": 1, "out": 4},
        "mid": {"in": 2, "out": 8},
        "strong": {"in": 3, "out": 16},
    },
}

CONSERVATIVE = {"in": 3, "out": 16}


def _round2(x):
    return math.floor(x * 100 + 0.5) / 100


def tier_of(model):
    if not model:
        return "unknown"
    if FAST_RE.search(model):
        return "fast"
    if STRONG_RE.search(model):
        return "strong"
    return "mid"


def tier_target(tier, provider, model_tiers):
    tiers = (model_tiers or {}).get("tiers") or {}
    lst = tiers.get(tier) or []
    prefix = (provider or "") + "/"
    for s in lst:
        if s.startswith(prefix):
            return s[len(prefix):]
    return None


def tier_has_any(tier, model_tiers):
    tiers = (model_tiers or {}).get("tiers") or {}
    lst = tiers.get(tier)
    return bool(lst)


def _provider_id(p):
    if isinstance(p, dict):
        return p.get("provider") or p.get("id") or p.get("name")
    return p


def resolve_tier(tier, provider, model_tiers, opts=None):
    """档位 → 模型 id 解析（支持跨 provider 与 mid 回退），返回 dict。

    与 js resolveTier 一致：{target, provider, used, note}
    """
    opts = opts or {}
    if not model_tiers:
        return {"target": None, "provider": None, "used": tier, "note": "tiers-not-ready"}
    cross = opts.get("crossProvider", True)

    def same(t):
        m = tier_target(t, provider, model_tiers)
        return {"provider": provider, "model": m} if m else None

    def cross_pick(t):
        if not cross:
            return None
        for p in model_tiers.get("providers") or []:
            pid = _provider_id(p)
            if not pid or pid == provider:
                continue
            m = tier_target(t, pid, model_tiers)
            if m:
                return {"provider": pid, "model": m}
        return None

    def is_cross(r):
        return bool(r and provider and r["provider"] != provider)

    direct = same(tier) or cross_pick(tier)
    if direct:
        return {"target": direct["model"], "provider": direct["provider"], "used": tier,
                "note": "cross-provider" if is_cross(direct) else ""}
    if tier == "mid":
        strong = same("strong") or cross_pick("strong")
        if strong:
            note = "mid->strong fallback" + (" [cross-provider]" if is_cross(strong) else "")
            return {"target": strong["model"], "provider": strong["provider"], "used": "strong", "note": note}
        fast = same("fast") or cross_pick("fast")
        if fast:
            note = "mid->fast fallback" + (" [cross-provider]" if is_cross(fast) else "")
            return {"target": fast["model"], "provider": fast["provider"], "used": "fast", "note": note}
    return {"target": None, "provider": None, "used": tier, "note": "no-candidate"}


def recommend_rule(cls):
    want = "strong" if cls["complexity"] >= 0.7 else ("fast" if cls["complexity"] <= 0.3 else "mid")
    return {"tier": want, "reason": "rule threshold complexity=" + str(cls["complexity"]), "conf": 0.5}


def recommend_ucb(cls, ucb_state, available_tiers=None):
    """UCB：mean + 1.2·√(lnN/n)；未探索臂上界∞，多个∞平局按总拉动数轮转。"""
    tiers = available_tiers or ["fast", "mid", "strong"]
    arms = ucb_state["arms"]
    scores = []
    for tier in tiers:
        arm = arms.get(cls["type"] + ":" + tier) or {"n": 0, "mean": 0}
        if arm["n"] > 0:
            ucb = arm["mean"] + 1.2 * math.sqrt(math.log(ucb_state["total"] + 1) / arm["n"])
        else:
            ucb = math.inf
        scores.append({"tier": tier, "ucb": ucb, "arm": arm})
    best = scores[0]
    for s in scores:
        if s["ucb"] > best["ucb"]:
            best = s
    infs = [s for s in scores if s["ucb"] == math.inf]
    if len(infs) > 1:
        best = infs[ucb_state["total"] % len(infs)]
    bound = "inf" if math.isinf(best["ucb"]) else _round2(best["ucb"])
    return {
        "tier": best["tier"],
        "reason": "ucb " + cls["type"] + " n=" + str(best["arm"]["n"]) +
                  " mean=" + str(_round2(best["arm"]["mean"])) + " bound=" + str(bound),
        "conf": 0.6 if math.isfinite(best["ucb"]) else 0.2,
    }


def recommend_lin(cls, f, lin_state, available_tiers=None):
    """近似 LinUCB：每档线性权重 w·f + 探索项（tn=0 时 0.3，否则 0.8·√(ln total/tn)）。"""
    tiers = available_tiers or ["fast", "mid", "strong"]
    tn = lin_state["tierN"]
    tier_w = lin_state["tierW"]
    best = None
    for tier in tiers:
        w = tier_w.get(tier) or []
        dot = 0.0
        for i in range(len(f)):
            dot += (w[i] if i < len(w) else 0) * f[i]
        bonus = 0.8 * math.sqrt(math.log(lin_state["total"] + 1) / tn[tier]) if tn[tier] > 0 else 0.3
        score = dot + bonus
        if best is None or score > best["score"]:
            best = {"tier": tier, "score": score, "dot": dot}
    return {
        "tier": best["tier"],
        "reason": "linucb score=" + str(_round2(best["score"])) + " (dot=" + str(_round2(best["dot"])) + ")",
        "conf": 0.7 if lin_state["total"] >= 50 else 0.1,
    }


def recommend_knn(cls, f, knn_state, available_tiers=None):
    """k-NN：欧氏距离取 k=5 邻居，按邻居平均奖励选档；经验 <5 时返回 tier=None。"""
    tiers = available_tiers or ["fast", "mid", "strong"]
    pool = knn_state["pool"]
    if len(pool) < 5:
        return {"tier": None, "reason": "knn cold (" + str(len(pool)) + "/5 seed)", "conf": 0}
    scored = []
    for e in pool:
        d = 0.0
        ef = e["f"]
        for i in range(len(f)):
            d += (ef[i] - f[i]) * (ef[i] - f[i])
        scored.append({"d": d, "e": e})
    scored.sort(key=lambda x: x["d"])
    k = min(5, len(scored))
    agg = {"fast": {"n": 0, "s": 0.0}, "mid": {"n": 0, "s": 0.0}, "strong": {"n": 0, "s": 0.0}}
    for i in range(k):
        t = scored[i]["e"]["tier"]
        if t in agg:
            agg[t]["n"] += 1
            agg[t]["s"] += scored[i]["e"]["r"]
    best = None
    for tier in tiers:
        a = agg[tier]
        if best is None or (a["n"] > 0 and a["s"] / a["n"] > best["score"]):
            best = {"tier": tier, "score": a["s"] / a["n"] if a["n"] > 0 else -1}
    return {
        "tier": best["tier"],
        "reason": "knn k=" + str(k) + " neighbors score=" + str(_round2(best["score"])),
        "conf": min(0.8, len(pool) / 100),
    }


def merge_prices(base, over):
    out = {}
    for k, v in (base or {}).items():
        out[k] = {}
        for k2, v2 in (v or {}).items():
            out[k][k2] = dict(v2)
    for k, v in (over or {}).items():
        out.setdefault(k, {})
        for k2, v2 in (v or {}).items():
            if isinstance(v2, dict) and isinstance(v2.get("in"), (int, float)) and isinstance(v2.get("out"), (int, float)):
                out[k][k2] = {"in": v2["in"], "out": v2["out"]}
    return out


def resolve_price(provider, model, tier, prices=None):
    table = prices or DEFAULT_PRICES
    by_provider = table.get(provider)
    if by_provider and by_provider.get(model):
        return {"price": by_provider[model], "source": "provider-model"}
    if by_provider and by_provider.get(tier):
        return {"price": by_provider[tier], "source": "provider-tier"}
    generic = table.get("*")
    if generic and generic.get(tier):
        return {"price": generic[tier], "source": "generic-tier"}
    return {"price": CONSERVATIVE, "source": "unknown-conservative"}


def turn_cost_yuan(usage, provider, model, tier, prices=None):
    if not usage:
        return 0
    price = resolve_price(provider, model, tier, prices)["price"]
    input_tokens = usage.get("inputTokens", 0) + usage.get("cacheReadTokens", 0)
    output_tokens = usage.get("outputTokens", 0) + usage.get("reasoningTokens", 0)
    return (price["in"] * input_tokens + price["out"] * output_tokens) / 1e6


def reward_of(td, tag, dt_ms=None):
    """质量代理奖励：干净收尾 1 分起，扣工具失败/重试/延迟/成本。返回 (reward, breakdown)。"""
    if tag != "clean-close":
        return 0, {
            "base": 0, "toolErrors": td.get("toolErrors", 0), "retries": td.get("retries", 0),
            "dtMs": dt_ms, "costYuan": td.get("costYuan", 0), "costPenalty": 0,
            "pricingSource": td.get("pricingSource", "n/a"),
        }
    reward = 1.0
    tool_penalty = min(0.6, 0.2 * td.get("toolErrors", 0))
    retry_penalty = min(0.4, 0.2 * td.get("retries", 0))
    cost_cap = td.get("costCap", 0.5)
    cost_weight = td.get("costWeight", 20)
    cost_yuan = td.get("costYuan", 0)
    cost_penalty = min(cost_cap, cost_weight * cost_yuan)
    reward -= tool_penalty
    reward -= retry_penalty
    reward -= cost_penalty
    if dt_ms is not None:
        if dt_ms > 90000:
            reward -= 0.3
        elif dt_ms > 30000:
            reward -= 0.1
    reward = max(0, _round2(reward))
    breakdown = {
        "base": 1, "toolErrors": td.get("toolErrors", 0), "toolPenalty": tool_penalty,
        "retries": td.get("retries", 0), "retryPenalty": retry_penalty,
        "costYuan": cost_yuan, "costPenalty": cost_penalty, "dtMs": dt_ms,
        "pricingSource": td.get("pricingSource", "n/a"),
    }
    return reward, breakdown


def apply_reward(state, rec):
    """把一条奖励记录应用到飞轮（UCB/LinUCB/k-NN/EXP3），replay 与实时更新共用。"""
    tier_n = state["lin"]["tierN"]
    rec["linLr"] = 1 / (1 + tier_n.get(rec["linTier"], 0))
    arm_key = rec["clsType"] + ":" + rec["ucbTier"]
    arm = state["ucb"]["arms"].setdefault(arm_key, {"n": 0, "mean": 0})
    arm["n"] += 1
    arm["mean"] = arm["mean"] + (rec["reward"] - arm["mean"]) / arm["n"]
    state["ucb"]["total"] += 1

    lw = state["lin"]["tierW"].setdefault(rec["linTier"], [0.0] * 9)
    lr = 1 / (1 + tier_n.get(rec["linTier"], 0))
    dot = 0.0
    for i in range(len(rec["f"])):
        dot += (lw[i] if i < len(lw) else 0) * rec["f"][i]
    for i in range(len(rec["f"])):
        lw[i] = (lw[i] if i < len(lw) else 0) + lr * (rec["reward"] - dot) * rec["f"][i]
    tier_n[rec["linTier"]] = tier_n.get(rec["linTier"], 0) + 1
    state["lin"]["total"] += 1

    state["knn"]["pool"].append({"f": list(rec["f"]), "tier": rec["ucbTier"], "r": rec["reward"]})
    if len(state["knn"]["pool"]) > 400:
        state["knn"]["pool"].pop(0)

    if rec.get("pChosen", 0) > 0:
        weights = state["exp3"]["weights"]
        pool_len = len(rec.get("pool") or [1])
        weights[rec["chosen"]] = weights.get(rec["chosen"], 0) * math.exp(
            0.3 * (rec["reward"] / rec["pChosen"]) / pool_len)
    state["exp3"]["total"] += 1

    # 权重防爆保险：整体除以最大值（γ 混合已保证采样下限；此处防 float 溢出）。
    # 关键是"地板"：反复归一化会让小权重下溢到精确 0（0×任何数=0，永久不可恢复）。
    # 地板把最大权重比钳在 1e6 内——比例过宽（如 1e12）时，被冷落专家靠 γ 地板
    # 采样翻身需要上千轮，主导权振荡周期过长；1e6 让恢复以百轮计。
    ws = state["exp3"]["weights"]
    if ws:
        m = max(ws.values())
        if m > 1e50:
            for k in ws:
                ws[k] = max(ws[k] / m, 1e-6)


def correct_reward(state, rec, new_reward):
    """反馈结算修正：把已应用奖励改为 new_reward（👍/👎 之后）。"""
    delta = new_reward - rec["reward"]
    if not delta:
        return {"delta": 0}

    arm = state["ucb"]["arms"].get(rec["clsType"] + ":" + rec["ucbTier"])
    if arm and arm["n"] > 0:
        arm["mean"] = arm["mean"] + delta / arm["n"]

    lw = state["lin"]["tierW"].get(rec["linTier"])
    if lw and isinstance(rec["f"], (list, tuple)):
        lr = rec.get("linLr") if isinstance(rec.get("linLr"), float) else 1 / (1 + state["lin"]["tierN"].get(rec["linTier"], 0))
        for i in range(len(rec["f"])):
            lw[i] = (lw[i] if i < len(lw) else 0) + lr * delta * rec["f"][i]

    if state.get("knn") and isinstance(rec["f"], (list, tuple)):
        state["knn"]["pool"].append({"f": list(rec["f"]), "tier": rec["ucbTier"], "r": new_reward})

    if rec.get("pChosen", 0) > 0 and rec.get("pool"):
        weights = state["exp3"]["weights"]
        weights[rec["chosen"]] = weights.get(rec["chosen"], 0) * math.exp(
            0.3 * delta / (rec["pChosen"] * len(rec["pool"])))

    rec["reward"] = new_reward
    return {"delta": delta}
