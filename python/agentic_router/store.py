"""飞轮持久化（框架无关版）：state.json 原子写，决策/奖励追加 JSONL。

JSONL 日志（decisions/rewards）按大小轮转：超过 MAX_LOG_MB（默认 10，
AGENTIC_ROUTER_MAX_LOG_MB 可配）后当前文件挪到 <name>.1（保留一代），
持续流量不会耗尽磁盘。DECISION_WINDOW 只约束内存态快照，不约束这些文件。
"""

import json
import os
from pathlib import Path

MAX_LOG_MB = 10


def default_home():
    env = os.environ.get("AGENTIC_ROUTER_HOME")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".agentic-router")


class Store:
    def __init__(self, home=None):
        self.home = Path(home or default_home())
        try:
            mb = float(os.environ.get("AGENTIC_ROUTER_MAX_LOG_MB", "") or MAX_LOG_MB)
        except ValueError:
            mb = MAX_LOG_MB
        self.max_log_bytes = max(mb, 1.0) * 1024 * 1024
        self.home.mkdir(parents=True, exist_ok=True)

    def state_path(self):
        return self.home / "state.json"

    def policy_path(self):
        return self.home / "policy.json"

    def _atomic_write(self, path, content):
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(path))

    def load_state(self):
        try:
            if not self.state_path().exists():
                return None
            s = json.loads(self.state_path().read_text(encoding="utf-8"))
            if not isinstance(s, dict) or "ucb" not in s:
                return None
            return s
        except (OSError, ValueError):
            return None

    def save_state(self, state):
        self._atomic_write(self.state_path(), json.dumps(state, ensure_ascii=False))

    def load_policy(self):
        try:
            if not self.policy_path().exists():
                return {}
            return json.loads(self.policy_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_policy(self, policy):
        self._atomic_write(self.policy_path(), json.dumps(policy, ensure_ascii=False, indent=2))

    def append_jsonl(self, name, obj):
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        p = self.home / name
        try:
            if p.exists() and p.stat().st_size + len(line) > self.max_log_bytes:
                # 尺寸轮转：当前文件挪到 .1（覆盖更旧的），新行写进新文件。
                # 轮转失败不影响请求路径（只影响磁盘占用）。
                old = self.home / (name + ".1")
                if old.exists():
                    old.unlink()
                os.replace(str(p), str(old))
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)

    def load_jsonl(self, name):
        try:
            p = self.home / name
            if not p.exists():
                return []
            return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            return []
