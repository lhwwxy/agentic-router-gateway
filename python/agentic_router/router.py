"""参考实现的 Python 移植：决策 → 落盘 → 奖励结算编排（框架无关）。

行为与 js/src/router.js 一致（golden 对拍见 tests/test_core.py）。
LiteLLM / 其他网关适配层把"请求进入 / 响应结束 / 失败"翻译成 decide/settle 调用。
"""

import time

from .classify import classify, features_of, FEATURE_TYPES
from .meta import DEFAULT_WEIGHTS, meta_pick
from .strategies import (
    apply_reward, correct_reward, recommend_knn, recommend_lin, recommend_rule,
    recommend_ucb, resolve_tier, reward_of,
)
from .store import Store

LIN_GRADUATION = 50    # LinUCB 毕业样本线
KNN_GRADUATION = 30    # k-NN 毕业经验线
DECISION_WINDOW = 200  # 内存中保留的最近决策数（用于 settle 匹配）


def empty_tiers():
    return {"providers": [], "tiers": {"fast": [], "mid": [], "strong": []}}


def fresh_state():
    return {
        "mode": "shadow",  # shadow: 只记录不干预；active: 真实改写模型
        "seq": 0,
        "decisions": [],
        "ucb": {"total": 0, "arms": {}},
        "lin": {
            "total": 0,
            "tierN": {"fast": 0, "mid": 0, "strong": 0},
            "tierW": {"fast": [0.0] * 9, "mid": [0.0] * 9, "strong": [0.0] * 9},
        },
        "knn": {"pool": []},
        "exp3": {"total": 0, "linGraduated": False, "knnGraduated": False,
                 "weights": dict(DEFAULT_WEIGHTS)},
    }


def rebuild_features(decision):
    f = [1.0, decision["complexity"], min(decision.get("len") or 0, 4000) / 4000]
    for ty in FEATURE_TYPES:
        f.append(1.0 if decision["type"] == ty else 0.0)
    return f


class Router:
    def __init__(self, store=None, home=None, mode=None, model_tiers=None,
                 cross_provider=True, prices=None, rng=None):
        self.store = store or Store(home)
        self.cross_provider = cross_provider
        self.model_tiers = model_tiers or empty_tiers()
        self.prices = prices
        self._rng = rng
        self.state = self.store.load_state() or fresh_state()
        if mode in ("active", "shadow"):
            self.state["mode"] = mode

    @property
    def mode(self):
        return self.state["mode"]

    def set_mode(self, mode):
        if mode not in ("active", "shadow"):
            raise ValueError('mode must be "active" or "shadow"')
        self.state["mode"] = mode
        self._persist()

    def decide(self, text, provider=None, available_tiers=None):
        """输入本轮用户文本，返回路由决策 dict（含目标模型）。"""
        state = self.state
        cls = classify(text)
        f = features_of(cls, text)
        tiers = list(available_tiers) if available_tiers else ["fast", "mid", "strong"]

        recs = {
            "rule": recommend_rule(cls),
            "ucb": recommend_ucb(cls, state["ucb"], tiers),
            "lin": recommend_lin(cls, f, state["lin"], tiers),
            "knn": recommend_knn(cls, f, state["knn"], tiers),
        }

        state["exp3"]["linGraduated"] = state["lin"]["total"] >= LIN_GRADUATION
        state["exp3"]["knnGraduated"] = len(state["knn"]["pool"]) >= KNN_GRADUATION
        meta = meta_pick(recs, state["exp3"], self._rng)
        chosen_rec = meta["byName"].get(meta["chosen"]) or recs["rule"]
        # k-NN 冷启动可能给出 None 档位：回退规则表
        tier = chosen_rec["tier"] or recs["rule"]["tier"]
        resolved = resolve_tier(tier, provider, self.model_tiers,
                                {"crossProvider": self.cross_provider})

        state["seq"] += 1
        decision = {
            "id": state["seq"],
            "ts": int(time.time() * 1000),
            "type": cls["type"],
            "complexity": cls["complexity"],
            "len": len(text or ""),
            "mode": state["mode"],
            "chosen": meta["chosen"],
            "pChosen": meta["ps"].get(meta["chosen"], 0),
            "pool": meta["pool"],
            "tier": tier,
            "used": resolved["used"],
            "provider": resolved["provider"],
            "model": resolved["target"],
            "note": resolved["note"],
            "reason": (chosen_rec or {}).get("reason", ""),
            "settled": False,
        }
        state["decisions"].append(decision)
        if len(state["decisions"]) > DECISION_WINDOW:
            state["decisions"].pop(0)
        self._persist()
        self.store.append_jsonl("decisions.jsonl", decision)
        return decision

    def settle(self, decision_id, success=True, dt_ms=None, cost_yuan=0.0,
               tool_errors=0, retries=0):
        """结果回传：计算奖励并喂飞轮。返回 {"reward", "breakdown", "tier"} 或 None。"""
        state = self.state
        d = next((x for x in state["decisions"] if x["id"] == decision_id and not x["settled"]), None)
        if not d:
            return None
        d["settled"] = True

        tag = "clean-close" if success is not False else "failed"
        reward, breakdown = reward_of(
            {"toolErrors": tool_errors, "retries": retries, "costYuan": cost_yuan},
            tag, dt_ms)

        rec = {
            "clsType": d["type"],
            "ucbTier": d["used"],
            "linTier": d["used"],
            "f": rebuild_features(d),
            "reward": reward,
            "chosen": d["chosen"],
            "pChosen": d["pChosen"],
            "pool": d["pool"],
        }
        apply_reward(state, rec)
        d["reward"] = reward
        d["linLr"] = rec["linLr"]
        self._persist()
        self.store.append_jsonl("rewards.jsonl", {
            "decisionId": d["id"], "ts": int(time.time() * 1000),
            "reward": reward, "breakdown": breakdown,
        })
        return {"reward": reward, "breakdown": breakdown, "tier": d["used"]}

    def correct(self, decision_id, new_reward):
        """人类反馈修正（👍/👎）。"""
        state = self.state
        d = next((x for x in state["decisions"] if x["id"] == decision_id and x["settled"]), None)
        if not d:
            return None
        rec = {
            "clsType": d["type"],
            "ucbTier": d["used"],
            "linTier": d["used"],
            "f": rebuild_features(d),
            "reward": d.get("reward", 0),
            "chosen": d["chosen"],
            "pChosen": d["pChosen"],
            "pool": d["pool"],
            "linLr": d.get("linLr"),
        }
        result = correct_reward(state, rec, new_reward)
        d["reward"] = new_reward
        self._persist()
        return result

    def _persist(self):
        self.store.save_state(self.state)
