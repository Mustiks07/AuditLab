"""External test adapter for policy_host: uses only the public wire format, imports nothing from auditlab."""


class TwoLooksThenEmpty:
    def __init__(self, seed):
        self.calls = 0
        self.episode_id = None

    def act(self, observation):
        if "manifest" in observation:
            self.episode_id = observation["manifest"]["episode_id"]
        self.calls += 1
        if self.calls == 1:
            return {"action": "list_records", "args": {"table": "payments", "cursor": None, "limit": 50}}
        if self.calls == 2:
            return {"action": "calculator", "args": {"expression": "50000-30000-20000"}}
        return {"action": "submit", "args": {"episode_id": self.episode_id, "findings": []}}


def make(seed):
    return TwoLooksThenEmpty(seed)
