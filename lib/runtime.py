"""Runtime helpers shared across project scripts."""

import os
import sys
from typing import Iterable, Mapping, Optional


def is_truthy_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "on", "yes", "y"}


def is_dry_run(
    args: Optional[Iterable[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    args = list(sys.argv[1:] if args is None else args)
    env = os.environ if env is None else env
    raw_values = []

    for arg in args:
        if arg == "--dry-run":
            return True
        if arg.startswith("dry_run=") or arg.startswith("dry-run="):
            raw_values.append(arg.split("=", 1)[1])

    for key in ("dry_run", "dry-run", "DRY_RUN"):
        value = env.get(key)
        if value is not None:
            raw_values.append(value)

    return any(is_truthy_value(str(value)) for value in raw_values)
