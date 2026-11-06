"""Public configuration names with compatibility for existing installations."""

import os


def env(name: str, legacy: str | None = None, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]
    return os.environ.get(legacy, default) if legacy else default


def schedule_enabled() -> bool:
    return env("LONGFOR_SCHEDULE_ENABLED", "AUX_SCHEDULE_ENABLED", "0").strip().lower() in (
        "1", "true", "on", "yes",
    )
