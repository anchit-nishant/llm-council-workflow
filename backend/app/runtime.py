from __future__ import annotations

import asyncio
import operator
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

from .events import EventBroker
from .google_gemini import resolve_google_gemini_model
from .model_gateway import CompletionSettings, LiteLLMModelGateway
from .schemas import (
    AggregateReview,
    AnonymizedResponse,
    CouncilConfig,
    ExpertOutput,
    ExpertSpec,
    NodeSnapshot,
    ReviewOutput,
    RunEvent,
    RunSnapshot,
    StageSnapshot,
    utcnow,
)
from .settings import AppSettings
from .storage import SQLiteStorage


class GraphState(TypedDict):
    run_id: str
    query: str
    config: CouncilConfig
    expert_outputs: Annotated[list[ExpertOutput], operator.add]
    anonymized_responses: list[AnonymizedResponse]
    review_outputs: Annotated[list[ReviewOutput], operator.add]
    aggregate_review: AggregateReview | None
    final_answer: str | None
    errors: Annotated[list[str], operator.add]


class ExpertTaskState(TypedDict):
    run_id: str
    query: str
    config: CouncilConfig
    expert: ExpertSpec


class ReviewTaskState(TypedDict):
    run_id: str
    query: str
    config: CouncilConfig
    expert: ExpertSpec
    anonymized_responses: list[AnonymizedResponse]


@dataclass
class RunTracker:
    storage: SQLiteStorage
    broker: EventBroker
    snapshot: RunSnapshot
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def _persist(self) -> None:
        self.snapshot.updated_at = utcnow()
        await asyncio.to_thread(self.storage.save_run_snapshot, self.snapshot)

    async def _record_event(
        self,
        *,
        event_type: str,
        stage: str | None = None,
        node_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        event = RunEvent(
            run_id=self.snapshot.run_id,
            type=event_type,
            stage=stage,  # type: ignore[arg-type]
            node_id=node_id,
            payload=payload or {},
        )
        event = await asyncio.to_thread(self.storage.append_event, event)
        await self.broker.publish(event)
        return event

    async def mark_run_started(self) -> None:
        async with self.lock:
            self.snapshot.status = "running"
            await self._persist()
            await self._record_event(
                event_type="run_started",
                payload={
                    "query": self.snapshot.query,
                    "config_id": self.snapshot.config_snapshot.id,
                    "config_name": self.snapshot.config_snapshot.name,
                },
            )

    async def mark_stage_started(self, stage: str) -> None:
        async with self.lock:
            stage_snapshot = self.snapshot.stage_snapshots[stage]
            stage_snapshot.status = "running"
            stage_snapshot.started_at = utcnow()
            stage_snapshot.error = None
            await self._persist()
            await self._record_event(
                event_type="stage_started",
                stage=stage,
                payload={
                    "label": stage_snapshot.label,
                    "total_nodes": stage_snapshot.total_nodes,
                },
            )

    async def mark_stage_completed(self, stage: str) -> None:
        async with self.lock:
            stage_snapshot = self.snapshot.stage_snapshots[stage]
            stage_snapshot.status = "completed"
            stage_snapshot.completed_at = utcnow()
            await self._persist()
            await self._record_event(
                event_type="stage_completed",
                stage=stage,
                payload={
                    "completed_nodes": stage_snapshot.completed_nodes,
                    "failed_nodes": stage_snapshot.failed_nodes,
                },
            )

    async def mark_stage_failed(self, stage: str, error: str) -> None:
        async with self.lock:
            stage_snapshot = self.snapshot.stage_snapshots[stage]
            stage_snapshot.status = "failed"
            stage_snapshot.completed_at = utcnow()
            stage_snapshot.error = error
            self.snapshot.error = error
            self.snapshot.status = "failed"
            await self._persist()
            await self._record_event(
                event_type="stage_failed",
                stage=stage,
                payload={"error": error},
            )

    async def set_stage_total_nodes(self, stage: str, total_nodes: int) -> None:
        async with self.lock:
            stage_snapshot = self.snapshot.stage_snapshots[stage]
            stage_snapshot.total_nodes = total_nodes
            await self._persist()

    async def mark_node_started(self, node_id: str) -> None:
        async with self.lock:
            node = self.snapshot.node_snapshots[node_id]
            node.status = "running"
            node.started_at = utcnow()
            node.error = None
            await self._persist()
            await self._record_event(
                event_type="node_started",
                stage=node.stage,
                node_id=node_id,
                payload={
                    "label": node.label,
                    "model": node.model,
                    "persona": node.persona,
                    "node_type": node.node_type,
                },
            )

    async def append_node_token(self, node_id: str, token: str) -> None:
        async with self.lock:
            node = self.snapshot.node_snapshots[node_id]
            node.output_preview += token
            await self._persist()
            await self._record_event(
                event_type="node_token",
                stage=node.stage,
                node_id=node_id,
                payload={"token": token},
            )

    async def mark_node_completed(self, node_id: str, output: Any) -> None:
        async with self.lock:
            node = self.snapshot.node_snapshots[node_id]
            node.status = "completed"
            node.completed_at = utcnow()
            node.output = output
            if isinstance(output, dict):
                if "answer" in output:
                    node.output_preview = str(output["answer"])
                elif "summary" in output:
                    node.output_preview = str(output["summary"])
                elif "text" in output:
                    node.output_preview = str(output["text"])
            elif isinstance(output, str):
                node.output_preview = output
            stage_snapshot = self.snapshot.stage_snapshots[node.stage]
            stage_snapshot.completed_nodes += 1
            await self._persist()
            await self._record_event(
                event_type="node_completed",
                stage=node.stage,
                node_id=node_id,
                payload={"output": output},
            )

    async def mark_node_failed(self, node_id: str, error: str) -> None:
        async with self.lock:
            node = self.snapshot.node_snapshots[node_id]
            node.status = "failed"
            node.completed_at = utcnow()
            node.error = error
            stage_snapshot = self.snapshot.stage_snapshots[node.stage]
            stage_snapshot.failed_nodes += 1
            await self._persist()
            await self._record_event(
                event_type="node_failed",
                stage=node.stage,
                node_id=node_id,
                payload={"error": error},
            )

    async def mark_node_skipped(self, node_id: str, reason: str) -> None:
        async with self.lock:
            node = self.snapshot.node_snapshots[node_id]
            node.status = "skipped"
            node.completed_at = utcnow()
            node.error = reason
            await self._persist()
            await self._record_event(
                event_type="node_skipped",
                stage=node.stage,
                node_id=node_id,
                payload={"reason": reason},
            )

    async def add_expert_output(self, output: ExpertOutput) -> None:
        async with self.lock:
            self.snapshot.expert_outputs.append(output)
            await self._persist()

    async def set_anonymized_responses(
        self, responses: list[AnonymizedResponse]
    ) -> None:
        async with self.lock:
            self.snapshot.anonymized_responses = responses
            await self._persist()

    async def add_review_output(self, output: ReviewOutput) -> None:
        async with self.lock:
            self.snapshot.review_outputs.append(output)
            await self._persist()

    async def set_aggregate_review(self, aggregate: AggregateReview) -> None:
        async with self.lock:
            self.snapshot.aggregate_review = aggregate
            await self._persist()

    async def set_final_answer(self, final_answer: str) -> None:
        async with self.lock:
            self.snapshot.final_answer = final_answer
            await self._persist()

    async def mark_run_completed(self) -> None:
        async with self.lock:
            self.snapshot.status = "completed"
            await self._persist()
            await self._record_event(
                event_type="run_completed",
                payload={"final_answer": self.snapshot.final_answer or ""},
            )

    async def mark_run_failed(self, error: str) -> None:
        async with self.lock:
            self.snapshot.status = "failed"
            self.snapshot.error = error
            await self._persist()
            await self._record_event(
                event_type="run_failed",
                payload={"error": error},
            )

    async def mark_run_canceled(self) -> None:
        async with self.lock:
            self.snapshot.status = "canceled"
            self.snapshot.error = "Run canceled by user."
            await self._persist()
            await self._record_event(
                event_type="run_canceled",
                payload={"message": "Run canceled by user."},
            )


class CouncilRuntime:
    def __init__(
        self, storage: SQLiteStorage, broker: EventBroker, settings: AppSettings
    ) -> None:
        self.storage = storage
        self.broker = broker
        self.settings = settings
        self.models = LiteLLMModelGateway(settings)
        self.trackers: dict[str, RunTracker] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(GraphState)
        graph.add_node("prepare_run", self.prepare_run)
        graph.add_node("expert_worker", self.expert_worker)
        graph.add_node("prepare_reviews", self.prepare_reviews)
        graph.add_node("review_worker", self.review_worker)
        graph.add_node("prepare_synthesis", self.prepare_synthesis)
        graph.add_node("synthesize_final", self.synthesize_final)
        graph.add_edge(START, "prepare_run")
        graph.add_conditional_edges("prepare_run", self.dispatch_experts)
        graph.add_edge("expert_worker", "prepare_reviews")
        graph.add_conditional_edges("prepare_reviews", self.dispatch_reviews)
        graph.add_edge("review_worker", "prepare_synthesis")
        graph.add_edge("prepare_synthesis", "synthesize_final")
        graph.add_edge("synthesize_final", END)
        return graph.compile()

    async def start_run(self, query: str, config: CouncilConfig) -> RunSnapshot:
        run_id = uuid.uuid4().hex
        snapshot = self._build_initial_snapshot(run_id, query, config)
        await asyncio.to_thread(self.storage.create_run, snapshot)
        tracker = RunTracker(storage=self.storage, broker=self.broker, snapshot=snapshot)
        self.trackers[run_id] = tracker
        self.cancel_events[run_id] = asyncio.Event()
        task = asyncio.create_task(self._execute_run(run_id, query, config))
        self.tasks[run_id] = task
        task.add_done_callback(lambda _: self._cleanup_run(run_id))
        return snapshot

    async def cancel_run(self, run_id: str) -> RunSnapshot | None:
        snapshot = await asyncio.to_thread(self.storage.get_run, run_id)
        if not snapshot:
            return None
        await asyncio.to_thread(self.storage.request_cancel, run_id)
        cancel_event = self.cancel_events.get(run_id)
        if cancel_event:
            cancel_event.set()
        return await asyncio.to_thread(self.storage.get_run, run_id)

    async def _execute_run(self, run_id: str, query: str, config: CouncilConfig) -> None:
        tracker = self.trackers[run_id]
        try:
            result = await self.graph.ainvoke(
                {
                    "run_id": run_id,
                    "query": query,
                    "config": config,
                    "expert_outputs": [],
                    "anonymized_responses": [],
                    "review_outputs": [],
                    "aggregate_review": None,
                    "final_answer": None,
                    "errors": [],
                }
            )
            if self.cancel_events[run_id].is_set():
                await tracker.mark_run_canceled()
                return
            if result.get("final_answer"):
                await tracker.set_final_answer(result["final_answer"])
            await tracker.mark_run_completed()
        except asyncio.CancelledError:
            await tracker.mark_run_canceled()
        except Exception as exc:  # noqa: BLE001
            await tracker.mark_run_failed(str(exc))

    def _cleanup_run(self, run_id: str) -> None:
        self.tasks.pop(run_id, None)
        self.cancel_events.pop(run_id, None)
        self.trackers.pop(run_id, None)

    def _build_initial_snapshot(
        self, run_id: str, query: str, config: CouncilConfig
    ) -> RunSnapshot:
        enabled_experts = [expert for expert in config.experts if expert.enabled]
        stage_snapshots = {
            "experts": StageSnapshot(
                stage="experts",
                label="Expert Responses",
                total_nodes=len(enabled_experts),
            ),
            "peer_review": StageSnapshot(
                stage="peer_review",
                label="Peer Review",
                total_nodes=0,
            ),
            "synthesis": StageSnapshot(
                stage="synthesis",
                label="Final Synthesis",
                total_nodes=1,
            ),
        }
        node_snapshots: dict[str, NodeSnapshot] = {}
        for index, expert in enumerate(enabled_experts):
            resolved_model = resolve_google_gemini_model(
                expert.model, self.settings.google_gemini_backend
            )
            node_snapshots[f"expert:{expert.id}"] = NodeSnapshot(
                node_id=f"expert:{expert.id}",
                stage="experts",
                node_type="expert",
                label=expert.label,
                model=resolved_model,
                persona=expert.persona,
                display_order=index,
            )
            node_snapshots[f"review:{expert.id}"] = NodeSnapshot(
                node_id=f"review:{expert.id}",
                stage="peer_review",
                node_type="review",
                label=f"{expert.label} Review",
                model=resolved_model,
                persona=expert.persona,
                display_order=index,
            )
        resolved_synthesis_model = resolve_google_gemini_model(
            config.synthesis_model, self.settings.google_gemini_backend
        )
        node_snapshots["synthesis:final"] = NodeSnapshot(
            node_id="synthesis:final",
            stage="synthesis",
            node_type="synthesis",
            label="Final Synthesis",
            model=resolved_synthesis_model,
            persona="Final council synthesizer",
            display_order=0,
        )
        return RunSnapshot(
            run_id=run_id,
            query=query,
            status="pending",
            config_snapshot=config,
            stage_snapshots=stage_snapshots,
            node_snapshots=node_snapshots,
        )

    async def _ensure_not_canceled(self, run_id: str) -> None:
        cancel_event = self.cancel_events.get(run_id)
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        canceled = await asyncio.to_thread(self.storage.is_cancel_requested, run_id)
        if canceled:
            if cancel_event:
                cancel_event.set()
            raise asyncio.CancelledError

    async def prepare_run(self, state: GraphState) -> dict[str, Any]:
        tracker = self.trackers[state["run_id"]]
        await tracker.mark_run_started()
        await tracker.mark_stage_started("experts")
        return {}

    def dispatch_experts(self, state: GraphState) -> list[Send]:
        sends: list[Send] = []
        for expert in state["config"].experts:
            if expert.enabled:
                sends.append(
                    Send(
                        "expert_worker",
                        {
                            "run_id": state["run_id"],
                            "query": state["query"],
                            "config": state["config"],
                            "expert": expert,
                        },
                    )
                )
        return sends

    async def expert_worker(self, state: ExpertTaskState) -> dict[str, Any]:
        run_id = state["run_id"]
        expert = state["expert"]
        tracker = self.trackers[run_id]
        node_id = f"expert:{expert.id}"
        await self._ensure_not_canceled(run_id)
        await tracker.mark_node_started(node_id)
        try:
            output = await self.models.generate_expert_output(
                expert=expert,
                query=state["query"],
                on_token=lambda token: tracker.append_node_token(node_id, token),
            )
            await tracker.add_expert_output(output)
            await tracker.mark_node_completed(node_id, output.model_dump(mode="json"))
            return {"expert_outputs": [output]}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await tracker.mark_node_failed(node_id, str(exc))
            return {"errors": [f"{expert.id}: {exc}"]}

    async def prepare_reviews(self, state: GraphState) -> dict[str, Any]:
        tracker = self.trackers[state["run_id"]]
        expert_outputs = self._sort_expert_outputs(state["config"], state["expert_outputs"])
        if not expert_outputs:
            error = "All expert nodes failed; no responses available for review."
            await tracker.mark_stage_failed("experts", error)
            raise RuntimeError(error)
        await tracker.mark_stage_completed("experts")
        responses = [
            AnonymizedResponse(
                label=f"Response {chr(65 + index)}",
                expert_id=output.expert_id,
                expert_label=output.expert_label,
                answer=output.answer,
                claims=output.claims,
                uncertainties=output.uncertainties,
            )
            for index, output in enumerate(expert_outputs)
        ]
        successful_expert_ids = {output.expert_id for output in expert_outputs}
        await tracker.set_anonymized_responses(responses)
        await tracker.set_stage_total_nodes("peer_review", len(successful_expert_ids))
        for expert in state["config"].experts:
            if expert.enabled and expert.id not in successful_expert_ids:
                await tracker.mark_node_skipped(
                    f"review:{expert.id}",
                    "Reviewer skipped because the expert response failed.",
                )
        await tracker.mark_stage_started("peer_review")
        return {"anonymized_responses": responses}

    def dispatch_reviews(self, state: GraphState) -> list[Send]:
        successful_expert_ids = {output.expert_id for output in state["expert_outputs"]}
        return [
            Send(
                "review_worker",
                {
                    "run_id": state["run_id"],
                    "query": state["query"],
                    "config": state["config"],
                    "expert": expert,
                    "anonymized_responses": state["anonymized_responses"],
                },
            )
            for expert in state["config"].experts
            if expert.enabled and expert.id in successful_expert_ids
        ]

    async def review_worker(self, state: ReviewTaskState) -> dict[str, Any]:
        run_id = state["run_id"]
        expert = state["expert"]
        tracker = self.trackers[run_id]
        node_id = f"review:{expert.id}"
        await self._ensure_not_canceled(run_id)
        await tracker.mark_node_started(node_id)
        try:
            review = await self.models.generate_review_output(
                expert=expert,
                query=state["query"],
                responses=state["anonymized_responses"],
                review_prompt_template=state["config"].review_prompt_template,
                on_token=lambda token: tracker.append_node_token(node_id, token),
            )
            await tracker.add_review_output(review)
            await tracker.mark_node_completed(node_id, review.model_dump(mode="json"))
            return {"review_outputs": [review]}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await tracker.mark_node_failed(node_id, str(exc))
            return {"errors": [f"review:{expert.id}: {exc}"]}

    async def prepare_synthesis(self, state: GraphState) -> dict[str, Any]:
        tracker = self.trackers[state["run_id"]]
        await tracker.mark_stage_completed("peer_review")
        aggregate = self._aggregate_reviews(state["config"], state["review_outputs"])
        await tracker.set_aggregate_review(aggregate)
        await tracker.mark_stage_started("synthesis")
        return {"aggregate_review": aggregate}

    async def synthesize_final(self, state: GraphState) -> dict[str, Any]:
        run_id = state["run_id"]
        tracker = self.trackers[run_id]
        node_id = "synthesis:final"
        await self._ensure_not_canceled(run_id)
        await tracker.mark_node_started(node_id)
        try:
            expert_outputs = self._sort_expert_outputs(state["config"], state["expert_outputs"])
            aggregate = state.get("aggregate_review") or AggregateReview()
            final_answer = await self.models.generate_synthesis(
                model_id=state["config"].synthesis_model,
                query=state["query"],
                expert_outputs=expert_outputs,
                review_outputs=state["review_outputs"],
                aggregate_ranking=aggregate.ranking_expert_ids,
                review_summary=aggregate.summary,
                synthesis_prompt_template=state["config"].synthesis_prompt_template,
                settings=CompletionSettings(
                    temperature=state["config"].synthesis_temperature,
                    max_tokens=state["config"].synthesis_max_tokens,
                    timeout_seconds=state["config"].synthesis_timeout_seconds,
                ),
                on_token=lambda token: tracker.append_node_token(node_id, token),
            )
            await tracker.set_final_answer(final_answer)
            await tracker.mark_node_completed(node_id, {"text": final_answer})
            await tracker.mark_stage_completed("synthesis")
            return {"final_answer": final_answer}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await tracker.mark_node_failed(node_id, str(exc))
            await tracker.mark_stage_failed("synthesis", str(exc))
            raise

    def _sort_expert_outputs(
        self, config: CouncilConfig, outputs: list[ExpertOutput]
    ) -> list[ExpertOutput]:
        order = {expert.id: index for index, expert in enumerate(config.experts)}
        return sorted(outputs, key=lambda output: order.get(output.expert_id, 10_000))

    def _aggregate_reviews(
        self, config: CouncilConfig, reviews: list[ReviewOutput]
    ) -> AggregateReview:
        if not reviews:
            return AggregateReview(
                ranking_expert_ids=[],
                scores={},
                summary="No peer-review results were available, so synthesis falls back to the expert outputs alone.",
            )
        scores: dict[str, int] = defaultdict(int)
        dimension_votes: dict[str, dict[str, int]] = {
            "best_overall_expert_id": defaultdict(int),
            "best_for_architecture_expert_id": defaultdict(int),
            "best_for_execution_expert_id": defaultdict(int),
            "best_for_clarity_expert_id": defaultdict(int),
        }
        merge_recommendations: list[str] = []
        critical_disagreements: list[str] = []
        for review in reviews:
            total = len(review.ranking_expert_ids)
            for index, expert_id in enumerate(review.ranking_expert_ids):
                scores[expert_id] += total - index
            for field_name in dimension_votes:
                expert_id = getattr(review, field_name)
                if expert_id:
                    dimension_votes[field_name][expert_id] += 1
            for recommendation in review.merge_recommendations:
                if recommendation not in merge_recommendations:
                    merge_recommendations.append(recommendation)
            for disagreement in review.critical_disagreements:
                if disagreement not in critical_disagreements:
                    critical_disagreements.append(disagreement)
        order = {expert.id: idx for idx, expert in enumerate(config.experts)}
        ranking = sorted(
            scores,
            key=lambda expert_id: (-scores[expert_id], order.get(expert_id, 10_000)),
        )
        best_overall_expert_id = self._choose_dimension_winner(
            dimension_votes["best_overall_expert_id"], order
        )
        best_for_architecture_expert_id = self._choose_dimension_winner(
            dimension_votes["best_for_architecture_expert_id"], order
        )
        best_for_execution_expert_id = self._choose_dimension_winner(
            dimension_votes["best_for_execution_expert_id"], order
        )
        best_for_clarity_expert_id = self._choose_dimension_winner(
            dimension_votes["best_for_clarity_expert_id"], order
        )
        summary_parts = []
        if best_overall_expert_id:
            summary_parts.append(f"Peer review saw {best_overall_expert_id} as the strongest overall answer.")
        if best_for_architecture_expert_id:
            summary_parts.append(f"{best_for_architecture_expert_id} led on architecture rigor.")
        if best_for_execution_expert_id:
            summary_parts.append(f"{best_for_execution_expert_id} led on execution practicality.")
        if best_for_clarity_expert_id:
            summary_parts.append(f"{best_for_clarity_expert_id} led on operator clarity.")
        if merge_recommendations:
            summary_parts.append(
                "Reviewers consistently recommended merging complementary strengths instead of blindly taking one winner."
            )
        if critical_disagreements:
            summary_parts.append(f"Main disagreement: {critical_disagreements[0]}")
        summary = " ".join(summary_parts) or (
            "Peer review completed, but did not produce a stable dimension-based consensus."
        )
        return AggregateReview(
            ranking_expert_ids=ranking,
            scores=dict(scores),
            best_overall_expert_id=best_overall_expert_id,
            best_for_architecture_expert_id=best_for_architecture_expert_id,
            best_for_execution_expert_id=best_for_execution_expert_id,
            best_for_clarity_expert_id=best_for_clarity_expert_id,
            merge_recommendations=merge_recommendations[:6],
            critical_disagreements=critical_disagreements[:6],
            summary=summary,
        )

    def _choose_dimension_winner(
        self, votes: dict[str, int], order: dict[str, int]
    ) -> str | None:
        if not votes:
            return None
        return sorted(votes, key=lambda expert_id: (-votes[expert_id], order.get(expert_id, 10_000)))[0]
