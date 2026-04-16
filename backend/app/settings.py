from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_DEFAULT_COUNCIL_MODELS: tuple[str, ...] = ()
BUILTIN_DEFAULT_SYNTHESIS_MODEL = ""


def load_env_file(env_path: Path | None = None) -> Path:
    path = env_path or PROJECT_ROOT / ".env"
    if not path.exists():
        return path

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _default_models_from_env() -> tuple[str, ...]:
    if os.getenv("DEFAULT_COUNCIL_MODELS"):
        return _split_csv(os.getenv("DEFAULT_COUNCIL_MODELS"))
    return BUILTIN_DEFAULT_COUNCIL_MODELS


@dataclass(frozen=True)
class AppSettings:
    project_root: Path
    env_file: Path
    default_council_models: tuple[str, ...]
    default_synthesis_model: str


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    env_file = load_env_file()
    default_models = _default_models_from_env()
    default_synthesis = os.getenv("DEFAULT_SYNTHESIS_MODEL") or BUILTIN_DEFAULT_SYNTHESIS_MODEL
    return AppSettings(
        project_root=PROJECT_ROOT,
        env_file=env_file,
        default_council_models=default_models,
        default_synthesis_model=default_synthesis,
    )
