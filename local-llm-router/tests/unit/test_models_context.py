from shared_quota_router.models import RequestRoutingContext


def test_request_routing_context_fields_and_limits() -> None:
    ctx = RequestRoutingContext(request_id="req-1")
    assert ctx.max_quota_groups == 3
    assert ctx.first_byte_sent is False
    assert ctx.can_try_quota_group("a") is True

    ctx.mark_tried("a")
    assert ctx.can_try_quota_group("a") is False
    assert ctx.can_try_quota_group("b") is True

    ctx.mark_tried("b")
    ctx.mark_tried("c")
    assert len(ctx.tried_quota_groups) == 3
    assert ctx.can_try_quota_group("d") is False


def test_first_byte_blocks_all_retries() -> None:
    ctx = RequestRoutingContext(request_id="req-2")
    ctx.mark_first_byte_sent()
    assert ctx.first_byte_sent is True
    assert ctx.can_try_quota_group("a") is False
