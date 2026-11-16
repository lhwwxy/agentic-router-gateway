"""飞轮持久化（框架无关版）：state.json 原子写，决策/奖励追加 JSONL。"""

import json
import os
from pathlib import Path


def default_home():
    env = os.environ.get("AGENTIC_ROUTER_HOME")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".agentic-router")


class Store:
    def __init__(self, home=None):
        self.home = Path(home or default_home())
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
        with open(self.home / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def load_jsonl(self, name):
        try:
            p = self.home / name
            if not p.exists():
                return []
            return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            return []
