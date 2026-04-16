from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from litellm import completion

from .schemas import AnonymizedResponse, ExpertOutput, ExpertSpec, ReviewOutput


TokenCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class CompletionSettings:
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: int = 120


class LiteLLMModelGateway:
    async def generate_expert_output(
        self,
        *,
        expert: ExpertSpec,
        query: str,
        on_token: TokenCallback,
    ) -> ExpertOutput:
        settings = CompletionSettings(
            temperature=expert.temperature,
            max_tokens=expert.max_tokens,
            timeout_seconds=expert.timeout_seconds,
        )
        prompt = self._expert_prompt(expert, query)
        text = await self._stream_text(
            model_id=expert.model,
            messages=[
                {"role": "system", "content": expert.system_prompt},
                {"role": "user", "content": prompt},
            ],
            settings=settings,
            on_token=on_token,
        )
        parsed = self._parse_expert_output(text)
        return ExpertOutput(
            expert_id=expert.id,
            expert_label=expert.label,
            model=expert.model,
            persona=expert.persona,
            answer=parsed["answer"],
            claims=parsed["claims"],
            uncertainties=parsed["uncertainties"],
            citations=parsed["citations"],
            confidence=parsed["confidence"],
        )

    async def generate_review_output(
        self,
        *,
        expert: ExpertSpec,
        query: str,
        responses: Sequence[AnonymizedResponse],
        review_prompt_template: str,
        on_token: TokenCallback,
    ) -> ReviewOutput:
        settings = CompletionSettings(
            temperature=expert.temperature,
            max_tokens=expert.max_tokens,
            timeout_seconds=expert.timeout_seconds,
        )
        prompt = self._review_prompt(
            expert=expert,
            query=query,
            responses=responses,
            review_prompt_template=review_prompt_template,
        )
        text = await self._stream_text(
            model_id=expert.model,
            messages=[
                {"role": "system", "content": expert.system_prompt},
                {"role": "user", "content": prompt},
            ],
            settings=settings,
            on_token=on_token,
        )
        parsed = self._parse_review_output(text, responses)
        return ReviewOutput(
            reviewer_id=expert.id,
            reviewer_label=expert.label,
            model=expert.model,
            persona=expert.persona,
            ranking_labels=parsed["ranking_labels"],
            ranking_expert_ids=[
                response.expert_id
                for label in parsed["ranking_labels"]
                for response in responses
                if response.label == label
            ],
            best_overall_expert_id=self._response_label_to_expert_id(
                parsed["best_overall_label"], responses
            ),
            best_for_architecture_expert_id=self._response_label_to_expert_id(
                parsed["best_for_architecture_label"], responses
            ),
            best_for_execution_expert_id=self._response_label_to_expert_id(
                parsed["best_for_execution_label"], responses
            ),
            best_for_clarity_expert_id=self._response_label_to_expert_id(
                parsed["best_for_clarity_label"], responses
            ),
            summary=parsed["summary"],
            merge_recommendations=parsed["merge_recommendations"],
            critical_disagreements=parsed["critical_disagreements"],
            per_response_feedback=parsed["feedback"],
        )

    async def generate_synthesis(
        self,
        *,
        model_id: str,
        query: str,
        expert_outputs: Sequence[ExpertOutput],
        review_outputs: Sequence[ReviewOutput],
        aggregate_ranking: Sequence[str],
        review_summary: str,
        synthesis_prompt_template: str,
        settings: CompletionSettings,
        on_token: TokenCallback,
    ) -> str:
        prompt = self._synthesis_prompt(
            query=query,
            expert_outputs=expert_outputs,
            review_outputs=review_outputs,
            aggregate_ranking=aggregate_ranking,
            review_summary=review_summary,
            synthesis_prompt_template=synthesis_prompt_template,
        )
        return await self._stream_text(
            model_id=model_id,
            messages=[
                {
                    "role": "system",
                    "content": "You are the final workflow synthesizer. Produce the best final answer for the user.",
                },
                {"role": "user", "content": prompt},
            ],
            settings=settings,
            on_token=on_token,
        )

    async def _stream_text(
        self,
        *,
        model_id: str,
        messages: list[dict[str, str]],
        settings: CompletionSettings,
        on_token: TokenCallback,
    ) -> str:
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(
            self._stream_text_sync,
            loop,
            model_id,
            messages,
            settings,
            on_token,
        )

    def _stream_text_sync(
        self,
        loop: asyncio.AbstractEventLoop,
        model_id: str,
        messages: list[dict[str, str]],
        settings: CompletionSettings,
        on_token: TokenCallback,
    ) -> str:
        kwargs = self._build_completion_kwargs(
            model_id=model_id,
            messages=messages,
            settings=settings,
            stream=not self._disable_streaming(model_id),
        )
        if not kwargs["stream"]:
            response = completion(**kwargs)
            text = self._extract_message_text(response).strip()
            for chunk in self._chunk_text(text):
                future = asyncio.run_coroutine_threadsafe(on_token(chunk), loop)
                future.result()
            return text

        response = completion(**kwargs)
        parts: list[str] = []
        for chunk in response:
            token = self._extract_delta_text(chunk)
            if not token:
                continue
            parts.append(token)
            future = asyncio.run_coroutine_threadsafe(on_token(token), loop)
            future.result()
        return "".join(parts).strip()

    def _build_completion_kwargs(
        self,
        *,
        model_id: str,
        messages: list[dict[str, str]],
        settings: CompletionSettings,
        stream: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": stream,
            "timeout": settings.timeout_seconds,
        }
        if settings.temperature is not None:
            kwargs["temperature"] = settings.temperature
        if settings.max_tokens is not None:
            kwargs["max_tokens"] = settings.max_tokens
        return kwargs

    def _disable_streaming(self, model_id: str) -> bool:
        lowered = model_id.lower()
        # Gemini/Vertex responses through LiteLLM are more reliable here when handled as
        # non-streaming completions, so we fall back to chunking the final text ourselves.
        return lowered.startswith("gemini/") or lowered.startswith("vertex_ai/")

    def _extract_message_text(self, response: Any) -> str:
        if response is None:
            return ""
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")
        if not choices:
            return ""
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None and isinstance(choice, dict):
            message = choice.get("message")
        text = self._extract_text_value(message)
        if text:
            return text
        delta = getattr(choice, "delta", None)
        if delta is None and isinstance(choice, dict):
            delta = choice.get("delta")
        return self._extract_text_value(delta)

    def _extract_delta_text(self, chunk: Any) -> str:
        if chunk is None:
            return ""
        choices = getattr(chunk, "choices", None)
        if choices is None and isinstance(chunk, dict):
            choices = chunk.get("choices")
        if not choices:
            return ""
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None and isinstance(choice, dict):
            delta = choice.get("delta")
        if delta is None:
            message = getattr(choice, "message", None)
            if message is None and isinstance(choice, dict):
                message = choice.get("message")
            return self._extract_text_value(message)
        return self._extract_text_value(delta)

    def _extract_text_value(self, payload: Any) -> str:
        if payload is None:
            return ""
        value = getattr(payload, "content", None)
        if value is None and isinstance(payload, dict):
            value = payload.get("content")
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text_value = item.get("text") or item.get("content")
                    if isinstance(text_value, str):
                        parts.append(text_value)
            return "".join(parts)
        return str(value)

    def _expert_prompt(self, expert: ExpertSpec, query: str) -> str:
        return (
            f"User query:\n{query}\n\n"
            f"Persona:\n{expert.persona}\n\n"
            "Respond using exactly these sections:\n"
            "ANSWER:\n"
            "<your complete answer>\n\n"
            "CLAIMS:\n"
            "- <claim 1>\n"
            "- <claim 2>\n"
            "- <claim 3>\n\n"
            "UNCERTAINTIES:\n"
            "- <uncertainty 1>\n"
            "- <uncertainty 2>\n\n"
            "CITATIONS:\n"
            "- <citation or 'None'>\n\n"
            "CONFIDENCE:\n"
            "<number between 0 and 1>\n"
        )

    def _review_prompt(
        self,
        *,
        expert: ExpertSpec,
        query: str,
        responses: Sequence[AnonymizedResponse],
        review_prompt_template: str,
    ) -> str:
        response_blocks = []
        for response in responses:
            response_blocks.append(
                (
                    f"{response.label}\n"
                    f"Answer: {response.answer}\n"
                    f"Claims: {json.dumps(response.claims)}\n"
                    f"Uncertainties: {json.dumps(response.uncertainties)}"
                )
            )
        joined = "\n\n".join(response_blocks)
        return (
            f"User query:\n{query}\n\n"
            f"Reviewer persona:\n{expert.persona}\n\n"
            f"Review instruction:\n{review_prompt_template}\n\n"
            f"Responses:\n{joined}\n\n"
            "Return exactly this format:\n"
            "BEST_OVERALL:\n"
            "Response A\n\n"
            "BEST_FOR_ARCHITECTURE:\n"
            "Response B\n\n"
            "BEST_FOR_EXECUTION:\n"
            "Response C\n\n"
            "BEST_FOR_OPERATOR_CLARITY:\n"
            "Response A\n\n"
            "MERGE_RECOMMENDATIONS:\n"
            "- <which ideas should be merged>\n"
            "- <what should be preserved in synthesis>\n\n"
            "CRITICAL_DISAGREEMENTS:\n"
            "- <important disagreement or tradeoff>\n\n"
            "RANKING:\n"
            "Response B > Response A > Response C\n"
            "(Use ranking only as a synthesis aid, not as a claim that only one answer is valid.)\n\n"
            "SUMMARY:\n"
            "<one concise paragraph>\n\n"
            "FEEDBACK:\n"
            "- Response A: <feedback>\n"
            "- Response B: <feedback>\n"
            "- Response C: <feedback>\n"
        )

    def _synthesis_prompt(
        self,
        *,
        query: str,
        expert_outputs: Sequence[ExpertOutput],
        review_outputs: Sequence[ReviewOutput],
        aggregate_ranking: Sequence[str],
        review_summary: str,
        synthesis_prompt_template: str,
    ) -> str:
        expert_section = "\n\n".join(
            f"{output.expert_label} ({output.model})\nAnswer: {output.answer}\nClaims: {json.dumps(output.claims)}\nUncertainties: {json.dumps(output.uncertainties)}"
            for output in expert_outputs
        )
        review_section = "\n\n".join(
            (
                f"{review.reviewer_label}\n"
                f"Ranking: {' > '.join(review.ranking_labels)}\n"
                f"Best overall expert id: {review.best_overall_expert_id}\n"
                f"Best architecture expert id: {review.best_for_architecture_expert_id}\n"
                f"Best execution expert id: {review.best_for_execution_expert_id}\n"
                f"Best clarity expert id: {review.best_for_clarity_expert_id}\n"
                f"Merge recommendations: {json.dumps(review.merge_recommendations)}\n"
                f"Critical disagreements: {json.dumps(review.critical_disagreements)}\n"
                f"Summary: {review.summary}\n"
                f"Feedback: {json.dumps(review.per_response_feedback)}"
            )
            for review in review_outputs
        )
        return (
            f"User query:\n{query}\n\n"
            f"Synthesis instruction:\n{synthesis_prompt_template}\n\n"
            f"Expert outputs:\n{expert_section}\n\n"
            f"Peer reviews:\n{review_section}\n\n"
            f"Aggregate ranking by expert id:\n{json.dumps(list(aggregate_ranking))}\n\n"
            f"Aggregate review summary:\n{review_summary}\n\n"
            "Treat peer review as multi-dimensional guidance, not a single-winner election. "
            "If multiple answers are complementary, merge them deliberately.\n\n"
            "Write the best final answer directly for the user. Resolve disagreement explicitly and keep the answer concrete."
        )

    def _parse_expert_output(self, text: str) -> dict[str, Any]:
        answer = self._extract_section(text, "ANSWER", ["CLAIMS", "UNCERTAINTIES", "CITATIONS", "CONFIDENCE"])
        claims = self._extract_bullets(
            self._extract_section(text, "CLAIMS", ["UNCERTAINTIES", "CITATIONS", "CONFIDENCE"])
        )
        uncertainties = self._extract_bullets(
            self._extract_section(text, "UNCERTAINTIES", ["CITATIONS", "CONFIDENCE"])
        )
        citations = self._extract_bullets(
            self._extract_section(text, "CITATIONS", ["CONFIDENCE"])
        )
        confidence_section = self._extract_section(text, "CONFIDENCE", [])
        confidence_match = re.search(r"([01](?:\.\d+)?)", confidence_section)
        confidence = float(confidence_match.group(1)) if confidence_match else 0.5
        answer = answer or text.strip()
        if not claims:
            claims = self._sentences(answer, limit=3)
        if not uncertainties:
            uncertainties = self._sentences(answer, limit=2)[-2:]
        if citations == ["None"]:
            citations = []
        return {
            "answer": answer,
            "claims": claims[:3],
            "uncertainties": uncertainties[:2],
            "citations": citations[:4],
            "confidence": max(0.0, min(confidence, 1.0)),
        }

    def _parse_review_output(
        self, text: str, responses: Sequence[AnonymizedResponse]
    ) -> dict[str, Any]:
        ranking_section = self._extract_section(text, "RANKING", ["SUMMARY", "FEEDBACK"])
        ranking_labels = self._extract_response_labels(ranking_section, responses)
        if not ranking_labels:
            ranking_labels = [response.label for response in responses]
        seen: set[str] = set()
        deduped_ranking = []
        for label in ranking_labels:
            if label not in seen:
                deduped_ranking.append(label)
                seen.add(label)
        for response in responses:
            if response.label not in seen:
                deduped_ranking.append(response.label)
        summary = self._extract_section(text, "SUMMARY", ["FEEDBACK"]) or text.strip()
        best_overall_label = self._extract_response_label(
            self._extract_section(
                text,
                "BEST_OVERALL",
                [
                    "BEST_FOR_ARCHITECTURE",
                    "BEST_FOR_EXECUTION",
                    "BEST_FOR_OPERATOR_CLARITY",
                    "MERGE_RECOMMENDATIONS",
                    "CRITICAL_DISAGREEMENTS",
                    "RANKING",
                    "SUMMARY",
                    "FEEDBACK",
                ],
            ),
            responses,
        )
        best_for_architecture_label = self._extract_response_label(
            self._extract_section(
                text,
                "BEST_FOR_ARCHITECTURE",
                [
                    "BEST_FOR_EXECUTION",
                    "BEST_FOR_OPERATOR_CLARITY",
                    "MERGE_RECOMMENDATIONS",
                    "CRITICAL_DISAGREEMENTS",
                    "RANKING",
                    "SUMMARY",
                    "FEEDBACK",
                ],
            ),
            responses,
        )
        best_for_execution_label = self._extract_response_label(
            self._extract_section(
                text,
                "BEST_FOR_EXECUTION",
                [
                    "BEST_FOR_OPERATOR_CLARITY",
                    "MERGE_RECOMMENDATIONS",
                    "CRITICAL_DISAGREEMENTS",
                    "RANKING",
                    "SUMMARY",
                    "FEEDBACK",
                ],
            ),
            responses,
        )
        best_for_clarity_label = self._extract_response_label(
            self._extract_section(
                text,
                "BEST_FOR_OPERATOR_CLARITY",
                ["MERGE_RECOMMENDATIONS", "CRITICAL_DISAGREEMENTS", "RANKING", "SUMMARY", "FEEDBACK"],
            ),
            responses,
        )
        merge_recommendations = self._extract_bullets(
            self._extract_section(
                text,
                "MERGE_RECOMMENDATIONS",
                ["CRITICAL_DISAGREEMENTS", "RANKING", "SUMMARY", "FEEDBACK"],
            )
        )
        critical_disagreements = self._extract_bullets(
            self._extract_section(text, "CRITICAL_DISAGREEMENTS", ["RANKING", "SUMMARY", "FEEDBACK"])
        )
        feedback_section = self._extract_section(text, "FEEDBACK", [])
        feedback: dict[str, str] = {}
        for match in re.finditer(
            r"(Response [A-Z])\s*:\s*(.+?)(?=\n(?:-?\s*Response [A-Z]\s*:)|\Z)",
            feedback_section,
            re.IGNORECASE | re.DOTALL,
        ):
            normalized_label = self._extract_response_label(match.group(1), responses)
            if normalized_label:
                feedback[normalized_label] = match.group(2).strip()
        for response in responses:
            feedback.setdefault(
                response.label,
                "This answer is useful, but it should be more concrete about decisions and tradeoffs.",
            )
        return {
            "best_overall_label": best_overall_label,
            "best_for_architecture_label": best_for_architecture_label,
            "best_for_execution_label": best_for_execution_label,
            "best_for_clarity_label": best_for_clarity_label,
            "ranking_labels": deduped_ranking,
            "summary": summary,
            "merge_recommendations": merge_recommendations[:4],
            "critical_disagreements": critical_disagreements[:4],
            "feedback": feedback,
        }

    def _extract_response_labels(
        self, text: str, responses: Sequence[AnonymizedResponse]
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_label in re.findall(r"Response [A-Z]", text, re.IGNORECASE):
            label = self._extract_response_label(raw_label, responses)
            if label and label not in seen:
                normalized.append(label)
                seen.add(label)
        return normalized

    def _extract_response_label(
        self, text: str, responses: Sequence[AnonymizedResponse]
    ) -> str | None:
        label_map = {response.label.lower(): response.label for response in responses}
        match = re.search(r"Response [A-Z]", text, re.IGNORECASE)
        if not match:
            return None
        return label_map.get(match.group(0).lower())

    def _response_label_to_expert_id(
        self, label: str | None, responses: Sequence[AnonymizedResponse]
    ) -> str | None:
        if not label:
            return None
        for response in responses:
            if response.label == label:
                return response.expert_id
        return None

    def _extract_section(
        self, text: str, header: str, next_headers: list[str]
    ) -> str:
        next_pattern = "|".join(re.escape(next_header) for next_header in next_headers)
        if next_pattern:
            pattern = rf"{re.escape(header)}:\s*(.*?)(?=\n(?:{next_pattern}):|\Z)"
        else:
            pattern = rf"{re.escape(header)}:\s*(.*)\Z"
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else ""

    def _extract_bullets(self, text: str) -> list[str]:
        bullets = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("-"):
                bullets.append(stripped[1:].strip())
        return [bullet for bullet in bullets if bullet]

    def _sentences(self, text: str, limit: int) -> list[str]:
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
        return parts[:limit]

    def _chunk_text(self, text: str, chunk_size: int = 120) -> list[str]:
        if not text:
            return []
        return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]
