from __future__ import annotations

import os
from typing import Any, Iterable

from .schemas import CouncilConfig


SUPPORTED_GOOGLE_GEMINI_BACKENDS = {"gemini_api", "vertex_ai"}


def normalize_google_gemini_backend(value: str | None) -> str:
    backend = (value or "gemini_api").strip().lower()
    if backend not in SUPPORTED_GOOGLE_GEMINI_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_GOOGLE_GEMINI_BACKENDS))
        raise ValueError(
            f"GOOGLE_GEMINI_BACKEND must be one of: {supported}. Got: {value!r}."
        )
    return backend


def is_google_gemini_model(model_id: str) -> bool:
    provider, model_name = _split_model_id(model_id)
    return provider in {"gemini", "vertex_ai"} or (
        not provider and model_name.lower().startswith("gemini")
    )


def resolve_google_gemini_model(model_id: str, backend: str) -> str:
    normalized_backend = normalize_google_gemini_backend(backend)
    provider, model_name = _split_model_id(model_id)
    if provider not in {"gemini", "vertex_ai"} and not (
        not provider and model_name.lower().startswith("gemini")
    ):
        return model_id.strip()
    prefix = "vertex_ai" if normalized_backend == "vertex_ai" else "gemini"
    return f"{prefix}/{model_name}"


def validate_google_gemini_config(settings: Any, config: CouncilConfig) -> None:
    model_ids = [expert.model for expert in config.experts if expert.enabled]
    model_ids.append(config.synthesis_model)
    validate_google_gemini_models(settings, model_ids)


def validate_google_gemini_models(settings: Any, model_ids: Iterable[str]) -> None:
    uses_google_gemini = any(is_google_gemini_model(model_id) for model_id in model_ids)
    if not uses_google_gemini:
        return
    if settings.google_gemini_backend == "vertex_ai":
        missing: list[str] = []
        if not settings.vertexai_project:
            missing.append("VERTEXAI_PROJECT")
        if not settings.vertexai_location:
            missing.append("VERTEXAI_LOCATION")
        if missing:
            raise ValueError(
                "Google Gemini is configured to use Vertex AI, but required environment "
                f"variables are missing: {', '.join(missing)}."
            )
        _validate_vertex_ai_adc()
        return
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise ValueError(
            "Google Gemini is configured to use the Gemini API, but neither GEMINI_API_KEY "
            "nor GOOGLE_API_KEY is set."
        )


def _split_model_id(model_id: str) -> tuple[str, str]:
    normalized = model_id.strip()
    if "/" not in normalized:
        return "", normalized
    provider, model_name = normalized.split("/", 1)
    return provider.strip().lower(), model_name.strip()


def _validate_vertex_ai_adc() -> None:
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError as exc:  # pragma: no cover - dependency/runtime guard
        raise ValueError(
            "Vertex AI mode requires the 'google-auth' package in the backend environment. "
            "Reinstall backend dependencies with './.venv/bin/pip install -r requirements.txt', "
            "then configure ADC."
        ) from exc

    try:
        google.auth.default()
    except DefaultCredentialsError as exc:
        raise ValueError(
            "Vertex AI mode requires Application Default Credentials. Run "
            "'gcloud auth application-default login' and ensure the authenticated account "
            "has access to the configured Vertex AI project."
        ) from exc
