"""Mark when select is running under the async hang-point.

``get_available_deployment`` is both the public sync entry and the callee of
``async_get_available_deployment``. Vision compose must fail-closed on the
public sync path without aborting production async select.
"""

from __future__ import annotations

import contextvars

_in_async_select: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sq_vision_async_select",
    default=False,
)


def mark_async_select() -> contextvars.Token[bool]:
    return _in_async_select.set(True)


def reset_async_select(token: contextvars.Token[bool]) -> None:
    _in_async_select.reset(token)


def is_async_select() -> bool:
    return bool(_in_async_select.get())
