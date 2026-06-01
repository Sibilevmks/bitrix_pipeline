#!/usr/bin/env python3

import inspect
from typing import Any, Callable, Dict, Optional


def make_debug_log(log: Any) -> Callable[[str, str, Optional[Dict[str, Any]]], None]:
    debug = getattr(log, "debug", None)
    if not callable(debug):
        return lambda scope, message, ctx=None: None

    has_varargs = True
    try:
        signature = inspect.signature(debug)
        has_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL
            for p in signature.parameters.values()
        )
    except (TypeError, ValueError):
        pass

    def debug_log(scope: str, message: str, ctx: Optional[Dict[str, Any]] = None) -> None:
        if has_varargs:
            if ctx:
                flat_ctx = " ".join("%s=%r" % (k, v) for k, v in ctx.items())
                debug("%s %s %s", scope, message, flat_ctx)
            else:
                debug("%s %s", scope, message)
            return

        debug(scope, message, ctx or {})

    return debug_log
