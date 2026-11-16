"""分类器：任务类型 + 复杂度（确定性规则，<1ms）。

与 js/src/classify.js 逐行对拍（见 spec/ROUTING_SPEC.md 与 tests/test_core.py）。
"""

import re

QA_IMPERATIVE = re.compile(r"(请|麻烦|帮我).{0,6}(回答|告诉|说)")
CODE_RE = re.compile(r"def |function |class |import |require\(|```|\.py|\.ts|\.js|\.go|\.rs\b|bug|error|exception|traceback|报错|代码|函数|接口|脚本")
WRITE_RE = re.compile(r"(写|创作|起草|润色|翻译|translate|write|blog|article|文案|邮件|报告)")
SUMMARIZE_RE = re.compile(r"(总结|概括|summar|摘要|要点)")
RESEARCH_RE = re.compile(r"(查|搜索|检索|research|调研|对比|最新|资料|文档)")
AGENTIC_RE = re.compile(r"(帮我|请|做|搭建|构建|部署|实现|写一个|生成|build|setup|deploy|implement|create)")
QA_RE = re.compile(r"[?？]|什么|为什么|怎么|如何|how|what|why|能否|是否")
COMPLEX_RE = re.compile(r"(重构|优化|设计|分析|架构|debug|调试|排查|review|方案|复杂|多步|逐步|implement|design|architecture)")
SIMPLE_RE = re.compile(r"(一句|简单|快速|quickly|short|hello|你好|谢谢)")

# 任务类型基础复杂度先验（实测调参）：代码与 agentic 任务天然更重
PRIOR = {"code": 0.3, "agentic": 0.3, "research": 0.2, "write": 0.15, "summarize": 0.05, "qa": 0, "other": 0}

FEATURE_TYPES = ["code", "write", "summarize", "research", "agentic", "qa"]


def classify(text):
    """返回 {"type": str, "complexity": float(0~1, 两位小数)}。"""
    t = text or ""
    lower = t.lower()
    type_ = "other"
    if QA_IMPERATIVE.search(lower):
        type_ = "qa"
    elif CODE_RE.search(lower):
        type_ = "code"
    elif WRITE_RE.search(lower):
        type_ = "write"
    elif SUMMARIZE_RE.search(lower):
        type_ = "summarize"
    elif RESEARCH_RE.search(lower):
        type_ = "research"
    elif AGENTIC_RE.search(lower):
        type_ = "agentic"
    elif QA_RE.search(lower):
        type_ = "qa"

    complex_kw = bool(COMPLEX_RE.search(lower))
    simple_kw = bool(SIMPLE_RE.search(lower))
    score = PRIOR.get(type_, 0)
    score += min(len(t) / 2000, 1) * 0.2
    score += 0.25 if "```" in t else 0
    if complex_kw:
        score += 0.25
    # 同时出现"简单"与"复杂"关键词时不做简单惩罚（实测踩过的坑）
    if simple_kw and not complex_kw:
        score -= 0.25
    score = max(0, min(1, score))
    # 复刻 JS 的 Math.round(x*100)/100（四舍五入向 +∞，而非 Python 的银行家舍入）
    return {"type": type_, "complexity": math_round2(score)}


def math_round2(x):
    import math
    return math.floor(x * 100 + 0.5) / 100


def features_of(cls, text):
    """LinUCB / k-NN 共享的特征向量：[1, 复杂度, 长度归一, 类型 one-hot ×6]。"""
    t = text or ""
    f = [1.0, cls["complexity"], min(len(t), 4000) / 4000]
    for ty in FEATURE_TYPES:
        f.append(1.0 if cls["type"] == ty else 0.0)
    return f
