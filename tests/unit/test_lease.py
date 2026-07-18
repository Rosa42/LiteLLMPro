from shared_quota_router.lease import LeaseManager, lease_ttl_seconds


class FakeRedisLua:
    """Minimal redis-like with EVAL for acquire/release scripts."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str):
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        if nx and name in self.data:
            return False
        self.data[name] = str(value)
        return True

    def delete(self, *names: str):
        for n in names:
            self.data.pop(n, None)

    def incr(self, name: str):
        v = int(self.data.get(name, "0")) + 1
        self.data[name] = str(v)
        return v

    def decr(self, name: str):
        v = int(self.data.get(name, "0")) - 1
        self.data[name] = str(v)
        return v

    def expire(self, name: str, time: int):
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args):
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        # Detect acquire vs release by key count
        if numkeys == 3:
            status_key, inflight_key, lease_key = keys
            ttl, max_inflight, request_id = int(args[0]), int(args[1]), args[2]
            raw = self.data.get(status_key)
            if raw and any(s in raw for s in ('"EXHAUSTED"', '"DISABLED"', '"PROBING"')):
                return [0, "quota_unavailable"]
            inflight = int(self.data.get(inflight_key, "0"))
            if max_inflight > 0 and inflight >= max_inflight:
                return [0, "max_inflight"]
            inflight = self.incr(inflight_key)
            self.set(lease_key, request_id, ex=ttl)
            return [1, str(inflight)]
        if numkeys == 2:
            inflight_key, lease_key = keys
            self.delete(lease_key)
            inflight = int(self.data.get(inflight_key, "0"))
            if inflight > 0:
                inflight = self.decr(inflight_key)
            if inflight < 0:
                self.data[inflight_key] = "0"
                inflight = 0
            return inflight
        raise AssertionError("unexpected eval")


def test_lease_ttl_formula() -> None:
    assert lease_ttl_seconds(300) == 330


def test_acquire_release_inflight() -> None:
    lm = LeaseManager(FakeRedisLua())
    assert lm.acquire(quota_group_id="a", request_id="r1", request_timeout_seconds=60)
    assert lm.get_inflight("a") == 1
    assert lm.release(quota_group_id="a", request_id="r1") == 0
    assert lm.get_inflight("a") == 0


def test_acquire_blocked_when_exhausted() -> None:
    r = FakeRedisLua()
    r.data["sq:quota:a"] = '{"status": "EXHAUSTED"}'
    lm = LeaseManager(r)
    assert lm.acquire(quota_group_id="a", request_id="r1") is False
