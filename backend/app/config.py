from __future__ import annotations

from .schemas import CouncilConfig, ExpertSpec
from .settings import AppSettings


def _assign_model(models: tuple[str, ...], index: int) -> str:
    if not models:
        raise ValueError(
            "DEFAULT_COUNCIL_MODELS is empty. Set default models in .env before starting the app."
        )
    return models[index % len(models)]


def build_default_council_config(settings: AppSettings) -> CouncilConfig:
    if not settings.default_synthesis_model:
        raise ValueError(
            "DEFAULT_SYNTHESIS_MODEL is empty. Set a synthesis model in .env before starting the app."
        )
    expert_templates = [
        (
            "systems-strategist",
            "Systems Strategist",
            "Think in systems, tradeoffs, leverage, and long-term constraints.",
            "You are the systems strategist. Clarify architecture, sequencing, dependencies, and second-order effects.",
        ),
        (
            "product-skeptic",
            "Product Skeptic",
            "Pressure-test assumptions, identify risk, and call out what is vague.",
            "You are the product skeptic. Find vague thinking, hidden complexity, risky assumptions, and missing success criteria.",
        ),
        (
            "implementation-lead",
            "Implementation Lead",
            "Turn ideas into practical implementation steps and delivery plans.",
            "You are the implementation lead. Focus on concrete implementation, sequencing, interfaces, and operational tradeoffs.",
        ),
        (
            "user-advocate",
            "User Advocate",
            "Represent user comprehension, usability, and trust.",
            "You are the user advocate. Emphasize clarity, UX, operator trust, and what a real user will struggle to understand.",
        ),
    ]
    experts = [
        ExpertSpec(
            id=expert_id,
            label=label,
            model=_assign_model(settings.default_council_models, index),
            persona=persona,
            system_prompt=system_prompt,
            timeout_seconds=150,
        )
        for index, (expert_id, label, persona, system_prompt) in enumerate(expert_templates)
    ]
    return CouncilConfig(
        id="default-council",
        name="Startup Review Workflow",
        description=(
            "A grounded workflow for stress-testing an early startup idea from strategy, product, execution, and user-trust angles."
        ),
        experts=experts,
        review_prompt_template=(
            "Review the anonymized expert answers for the same user query. Evaluate them on overall "
            "quality, architecture rigor, execution practicality, and operator clarity. If more than one "
            "answer is good, say what should be merged instead of pretending there is only one valid winner."
        ),
        synthesis_model=settings.default_synthesis_model,
        synthesis_prompt_template=(
            "Synthesize the best final answer from the expert outputs and the peer reviews. "
            "Merge complementary strengths, resolve disagreement directly, and surface uncertainty when it matters."
        ),
        synthesis_timeout_seconds=180,
    )
