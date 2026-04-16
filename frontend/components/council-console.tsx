"use client";

import "@xyflow/react/dist/style.css";

import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import ELK from "elkjs/lib/elk.bundled.js";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { MarkdownBlock, markdownPreview } from "@/components/markdown-block";
import {
  cancelRun,
  clearRuns,
  createRun,
  deleteRun as removeRun,
  eventStreamUrl,
  fetchConfigs,
  fetchRun,
  fetchRuns,
} from "@/lib/api";
import type {
  CouncilConfig,
  ExpertOutput,
  NodeSnapshot,
  NodeType,
  ReviewOutput,
  RunEvent,
  RunSnapshot,
  RunSummary,
  StageName,
  StageSnapshot,
} from "@/lib/types";

const WORKFLOW_STAGES: StageName[] = ["experts", "peer_review", "synthesis"];
const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "canceled"]);

type GraphMode = "council" | "run";

interface PreviewNode {
  node_id: string;
  stage: StageName;
  node_type: NodeType;
  label: string;
  model: string;
  persona: string;
  summary: string;
  prompt: string;
  display_order: number;
}

interface GraphCardNode {
  id: string;
  label: string;
  model: string;
  summary: string;
  status: string;
  stage: StageName;
  node_type: NodeType;
}

interface GraphScene {
  mode: GraphMode;
  configName: string;
  queryText: string;
  experts: GraphCardNode[];
  reviews: GraphCardNode[];
  synthesis: GraphCardNode | null;
  stageBadges: Array<{
    stage: StageName;
    label: string;
    meta: string;
    status: string;
  }>;
  helperText: string;
}

type InspectorTarget =
  | { source: "run"; node: NodeSnapshot }
  | { source: "preview"; node: PreviewNode };

function stageOrder(stage: string): number {
  if (stage === "experts") return 0;
  if (stage === "peer_review") return 1;
  return 2;
}

function sortedNodes(snapshot: RunSnapshot | null): NodeSnapshot[] {
  if (!snapshot) return [];
  return Object.values(snapshot.node_snapshots).sort((a, b) => {
    const stageDelta = stageOrder(a.stage) - stageOrder(b.stage);
    if (stageDelta !== 0) return stageDelta;
    return a.display_order - b.display_order;
  });
}

function cloneRunSnapshot(snapshot: RunSnapshot): RunSnapshot {
  return JSON.parse(JSON.stringify(snapshot)) as RunSnapshot;
}

function applyEvent(snapshot: RunSnapshot, event: RunEvent): RunSnapshot {
  const next = cloneRunSnapshot(snapshot);
  const nodeId = event.node_id ?? undefined;
  const stageId = event.stage ?? undefined;

  if (event.type === "run_started") {
    next.status = "running";
  }

  if (stageId && next.stage_snapshots[stageId]) {
    const stage = next.stage_snapshots[stageId];
    if (event.type === "stage_started") {
      stage.status = "running";
      stage.started_at = event.timestamp;
      stage.total_nodes = Number(event.payload.total_nodes ?? stage.total_nodes);
    }
    if (event.type === "stage_completed") {
      stage.status = "completed";
      stage.completed_at = event.timestamp;
      stage.completed_nodes = Number(event.payload.completed_nodes ?? stage.completed_nodes);
      stage.failed_nodes = Number(event.payload.failed_nodes ?? stage.failed_nodes);
    }
    if (event.type === "stage_failed") {
      stage.status = "failed";
      stage.completed_at = event.timestamp;
      stage.error = String(event.payload.error ?? "Stage failed");
    }
  }

  if (nodeId && next.node_snapshots[nodeId]) {
    const node = next.node_snapshots[nodeId];
    if (event.type === "node_started") {
      node.status = "running";
      node.started_at = event.timestamp;
    }
    if (event.type === "node_token") {
      node.output_preview += String(event.payload.token ?? "");
    }
    if (event.type === "node_completed") {
      node.status = "completed";
      node.completed_at = event.timestamp;
      node.output = event.payload.output;
      if (typeof event.payload.output === "object" && event.payload.output) {
        const output = event.payload.output as Record<string, unknown>;
        if (typeof output.answer === "string") node.output_preview = output.answer;
        if (typeof output.summary === "string") node.output_preview = output.summary;
        if (typeof output.text === "string") node.output_preview = output.text;
      }
    }
    if (event.type === "node_failed") {
      node.status = "failed";
      node.completed_at = event.timestamp;
      node.error = String(event.payload.error ?? "Node failed");
    }
    if (event.type === "node_skipped") {
      node.status = "skipped";
      node.completed_at = event.timestamp;
      node.error = String(event.payload.reason ?? "Node skipped");
    }
  }

  if (event.type === "run_completed") {
    next.status = "completed";
    next.final_answer = String(event.payload.final_answer ?? next.final_answer ?? "");
  }

  if (event.type === "run_failed") {
    next.status = "failed";
    next.error = String(event.payload.error ?? "Run failed");
  }

  if (event.type === "run_canceled") {
    next.status = "canceled";
    next.error = String(event.payload.message ?? "Run canceled");
  }

  next.updated_at = event.timestamp;
  return next;
}

function statusClass(status: string): string {
  return status;
}

function formatDate(value?: string | null): string {
  if (!value) return "N/A";
  return new Date(value).toLocaleString();
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isExpertOutputValue(value: unknown): value is ExpertOutput {
  const record = asRecord(value);
  return Boolean(
    record &&
      typeof record.answer === "string" &&
      isStringArray(record.claims) &&
      isStringArray(record.uncertainties) &&
      isStringArray(record.citations)
  );
}

function isReviewOutputValue(value: unknown): value is ReviewOutput {
  const record = asRecord(value);
  return Boolean(
    record &&
      typeof record.summary === "string" &&
      isStringArray(record.ranking_expert_ids) &&
      isStringArray(record.ranking_labels) &&
      isStringArray(record.merge_recommendations) &&
      isStringArray(record.critical_disagreements)
  );
}

function isSynthesisOutputValue(value: unknown): value is { text: string } {
  const record = asRecord(value);
  return Boolean(record && typeof record.text === "string");
}

function nodePreview(node: NodeSnapshot): string {
  return markdownPreview(node.output_preview || node.persona || "Waiting for output...", 170);
}

function stageTitle(stage: StageName): string {
  if (stage === "experts") return "Expert Responses";
  if (stage === "peer_review") return "Peer Review";
  return "Final Synthesis";
}

function nodeRoleDescription(nodeType: NodeType): string {
  if (nodeType === "expert") {
    return "This is a first-pass expert answer. Read it for the agent's independent view before the workflow starts judging other answers.";
  }
  if (nodeType === "review") {
    return "This node is a peer review pass. The agent is judging anonymized expert answers, identifying strengths to merge and disagreements to resolve.";
  }
  return "This is the final workflow synthesizer. It takes the expert answers plus peer review signals and produces the answer you should actually read first.";
}

function nodeReadingHint(nodeType: NodeType): string {
  if (nodeType === "expert") {
    return "Use this to compare perspectives, find missing constraints, and inspect claims before consensus compresses them.";
  }
  if (nodeType === "review") {
    return "Use this to understand why one answer beat another on architecture, execution, or clarity, and what should be merged rather than discarded.";
  }
  return "Use this as the workflow's merged answer, but cross-check it against the expert and review nodes when you want to see tradeoffs or hidden disagreements.";
}

function expertLabel(snapshot: RunSnapshot, expertId: string): string {
  return (
    snapshot.config_snapshot.experts.find((expert) => expert.id === expertId)?.label ??
    snapshot.expert_outputs.find((expert) => expert.expert_id === expertId)?.expert_label ??
    expertId
  );
}

function responseAlias(snapshot: RunSnapshot, expertId: string): string | null {
  return snapshot.anonymized_responses.find((response) => response.expert_id === expertId)?.label ?? null;
}

function responseFeedbackTitle(snapshot: RunSnapshot, label: string): string {
  const response = snapshot.anonymized_responses.find((item) => item.label === label);
  if (!response) return label;
  return `${label} · ${response.expert_label}`;
}

function formatDimensionWinner(snapshot: RunSnapshot, expertId?: string | null): string {
  if (!expertId) return "No clear winner";
  const alias = responseAlias(snapshot, expertId);
  if (!alias) return expertLabel(snapshot, expertId);
  return `${expertLabel(snapshot, expertId)} (${alias})`;
}

function bezierPath(startX: number, startY: number, endX: number, endY: number): string {
  const delta = Math.max(60, (endX - startX) * 0.45);
  return `M ${startX} ${startY} C ${startX + delta} ${startY}, ${endX - delta} ${endY}, ${endX} ${endY}`;
}

function distributeYInRange(
  count: number,
  startY: number,
  endY: number,
  cardHeight: number,
  gap = 24
): number[] {
  if (!count) return [];
  const total = count * cardHeight + Math.max(0, count - 1) * gap;
  const available = Math.max(cardHeight, endY - startY);
  const start = total < available ? startY + (available - total) / 2 : startY;
  return Array.from({ length: count }, (_, index) => start + index * (cardHeight + gap));
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function buildPreviewNodes(config: CouncilConfig): Record<StageName, PreviewNode[]> {
  const enabledExperts = config.experts.filter((expert) => expert.enabled);
  return {
    experts: enabledExperts.map((expert, index) => ({
      node_id: `preview-expert-${expert.id}`,
      stage: "experts",
      node_type: "expert",
      label: expert.label,
      model: expert.model,
      persona: expert.persona,
      summary: expert.persona,
      prompt: expert.system_prompt,
      display_order: index,
    })),
    peer_review: enabledExperts.map((expert, index) => ({
      node_id: `preview-review-${expert.id}`,
      stage: "peer_review",
      node_type: "review",
      label: `${expert.label} Review`,
      model: expert.model,
      persona: expert.persona,
      summary: "This agent reviews anonymized expert answers and scores them on architecture, execution, and clarity.",
      prompt: config.review_prompt_template,
      display_order: index,
    })),
    synthesis: [
      {
        node_id: "preview-synthesis",
        stage: "synthesis",
        node_type: "synthesis",
        label: "Final Synthesis",
        model: config.synthesis_model,
        persona: "Final workflow synthesizer",
        summary: "This node merges expert outputs and review signals into one final answer.",
        prompt: config.synthesis_prompt_template,
        display_order: 0,
      },
    ],
  };
}

function previewNodeMap(nodes: Record<StageName, PreviewNode[]>): Record<string, PreviewNode> {
  return [...nodes.experts, ...nodes.peer_review, ...nodes.synthesis].reduce<Record<string, PreviewNode>>(
    (accumulator, node) => {
      accumulator[node.node_id] = node;
      return accumulator;
    },
    {}
  );
}

function buildPreviewScene(config: CouncilConfig, query: string): GraphScene {
  const nodes = buildPreviewNodes(config);
  return {
    mode: "council",
    configName: config.name,
    queryText: query,
    experts: nodes.experts.map((node) => ({
      id: node.node_id,
      label: node.label,
      model: node.model,
      summary: markdownPreview(node.summary, 120),
      status: "configured",
      stage: node.stage,
      node_type: node.node_type,
    })),
    reviews: nodes.peer_review.map((node) => ({
      id: node.node_id,
      label: node.label,
      model: node.model,
      summary: markdownPreview(node.summary, 120),
      status: "configured",
      stage: node.stage,
      node_type: node.node_type,
    })),
    synthesis: nodes.synthesis[0]
      ? {
          id: nodes.synthesis[0].node_id,
          label: nodes.synthesis[0].label,
          model: nodes.synthesis[0].model,
          summary: markdownPreview(nodes.synthesis[0].summary, 120),
          status: "configured",
          stage: "synthesis",
          node_type: "synthesis",
        }
      : null,
    stageBadges: [
      { stage: "experts", label: "Experts", meta: `${nodes.experts.length} configured`, status: "configured" },
      { stage: "peer_review", label: "Peer Review", meta: `${nodes.peer_review.length} reviewers`, status: "configured" },
      { stage: "synthesis", label: "Synthesis", meta: "1 final synthesizer", status: "configured" },
    ],
    helperText: "Preview the selected workflow before you run it. Click any node to inspect its role, prompt, and place in the execution flow.",
  };
}

function buildRunScene(snapshot: RunSnapshot): GraphScene {
  const sorted = sortedNodes(snapshot);
  const byStage = WORKFLOW_STAGES.reduce<Record<StageName, NodeSnapshot[]>>(
    (accumulator, stage) => {
      accumulator[stage] = sorted.filter((node) => node.stage === stage);
      return accumulator;
    },
    {
      experts: [],
      peer_review: [],
      synthesis: [],
    }
  );

  return {
    mode: "run",
    configName: snapshot.config_snapshot.name,
    queryText: snapshot.query,
    experts: byStage.experts.map((node) => ({
      id: node.node_id,
      label: node.label,
      model: node.model,
      summary: nodePreview(node),
      status: node.status,
      stage: node.stage,
      node_type: node.node_type,
    })),
    reviews: byStage.peer_review.map((node) => ({
      id: node.node_id,
      label: node.label,
      model: node.model,
      summary: nodePreview(node),
      status: node.status,
      stage: node.stage,
      node_type: node.node_type,
    })),
    synthesis: byStage.synthesis[0]
      ? {
          id: byStage.synthesis[0].node_id,
          label: byStage.synthesis[0].label,
          model: byStage.synthesis[0].model,
          summary: nodePreview(byStage.synthesis[0]),
          status: byStage.synthesis[0].status,
          stage: "synthesis",
          node_type: "synthesis",
        }
      : null,
    stageBadges: Object.values(snapshot.stage_snapshots)
      .sort((a, b) => stageOrder(a.stage) - stageOrder(b.stage))
      .map((stage) => ({
        stage: stage.stage,
        label: stage.label,
        meta: `${stage.completed_nodes}/${stage.total_nodes} complete`,
        status: stage.status,
      })),
    helperText: "Inspect the selected run. Click any node to open its output in the sticky inspector.",
  };
}

function currentSynthesisText(snapshot: RunSnapshot | null): string | null {
  if (!snapshot) return null;
  if (snapshot.final_answer) return snapshot.final_answer;
  const synthesisNode = Object.values(snapshot.node_snapshots).find((node) => node.stage === "synthesis");
  return synthesisNode?.output_preview || null;
}

type WorkflowRfNodeData =
  | {
      kind: "stage";
      title: string;
      subtitle: string;
      kicker: string;
      tone: "expert" | "review" | "synthesis";
    }
  | {
      kind: "card" | "hub";
      sourceId: string;
      selectable: boolean;
      kicker: string;
      title: string;
      model?: string;
      summary: string;
      status?: string;
      tone: "expert" | "review" | "synthesis" | "hub";
      hasTarget?: boolean;
      hasSource?: boolean;
    };

const elk = new ELK();
const ELK_CARD_WIDTH = 320;
const ELK_CARD_HEIGHT = 182;
const ELK_HUB_WIDTH = 250;
const ELK_HUB_HEIGHT = 190;
const ELK_SYNTHESIS_WIDTH = 360;
const ELK_SYNTHESIS_HEIGHT = 228;
const ELK_STAGE_HEADER = 96;

type StageNodeData = Extract<WorkflowRfNodeData, { kind: "stage" }>;
type FlowCardNodeData = Extract<WorkflowRfNodeData, { kind: "card" | "hub" }>;

function StageNode({ data }: NodeProps<Node<StageNodeData>>) {
  return (
    <div className={`workflow-stage-node workflow-stage-node-${data.tone}`}>
      <div className="workflow-stage-label">
        <span>{data.kicker}</span>
        <strong>{data.title}</strong>
        <small>{data.subtitle}</small>
      </div>
    </div>
  );
}

function CardNode({
  data,
}: NodeProps<Node<FlowCardNodeData>>) {
  const toneClass = `workflow-flow-card-${data.tone}`;
  const status = data.status ?? "configured";

  return (
    <div
      className={`workflow-flow-card ${toneClass} ${data.kind === "hub" ? "workflow-flow-hub" : ""}`}
    >
      {data.hasTarget ? <Handle type="target" position={Position.Left} className="workflow-flow-handle" /> : null}
      {data.hasSource ? <Handle type="source" position={Position.Right} className="workflow-flow-handle" /> : null}
      <div className="workflow-flow-topline">
        <span className="workflow-flow-kicker">{data.kicker}</span>
        {data.kind === "card" ? <span className={`status-pill ${statusClass(status)}`}>{status}</span> : null}
      </div>
      <h4>{data.title}</h4>
      {data.model ? <div className="workflow-flow-model">{data.model}</div> : null}
      <div className="workflow-flow-summary">{data.summary}</div>
    </div>
  );
}

const workflowNodeTypes = {
  stage: StageNode,
  card: CardNode,
};

async function buildWorkflowLayout(
  scene: GraphScene,
  selectedNodeId: string | null
): Promise<{ nodes: Node<WorkflowRfNodeData>[]; edges: Edge[] }> {
  const synthesisId = scene.synthesis?.id ?? "scene-synthesis";
  const baseNodes: Array<{
    id: string;
    width: number;
    height: number;
    stage: "input" | "experts" | "review_context" | "peer_review" | "synthesis";
    data: WorkflowRfNodeData;
    selected?: boolean;
  }> = [
    {
      id: "scene-input",
      width: ELK_HUB_WIDTH,
      height: ELK_HUB_HEIGHT,
      stage: "input",
      data: {
        kind: "hub",
        sourceId: "scene-input",
        selectable: false,
        kicker: scene.mode === "run" ? "Run Input" : "Workflow Preview",
        title: scene.mode === "run" ? "User Query" : "Input Brief",
        summary: markdownPreview(scene.queryText, 170),
        tone: "hub",
        hasSource: true,
      },
    },
    ...scene.experts.map((node) => ({
      id: node.id,
      width: ELK_CARD_WIDTH,
      height: ELK_CARD_HEIGHT,
      stage: "experts" as const,
      selected: node.id === selectedNodeId,
      data: {
        kind: "card" as const,
        sourceId: node.id,
        selectable: true,
        kicker: "Expert",
        title: node.label,
        model: node.model,
        summary: node.summary,
        status: node.status,
        tone: "expert" as const,
        hasTarget: true,
        hasSource: true,
      },
    })),
    {
      id: "scene-review-context",
      width: ELK_HUB_WIDTH,
      height: ELK_HUB_HEIGHT,
      stage: "review_context",
      data: {
        kind: "hub",
        sourceId: "scene-review-context",
        selectable: false,
        kicker: "Review Context",
        title: "Blind Review Set",
        summary:
          "Expert answers are relabeled before review so critique stays focused on content instead of author identity.",
        tone: "hub",
        hasTarget: true,
        hasSource: true,
      },
    },
    ...scene.reviews.map((node) => ({
      id: node.id,
      width: ELK_CARD_WIDTH,
      height: ELK_CARD_HEIGHT,
      stage: "peer_review" as const,
      selected: node.id === selectedNodeId,
      data: {
        kind: "card" as const,
        sourceId: node.id,
        selectable: true,
        kicker: "Review",
        title: node.label,
        model: node.model,
        summary: node.summary,
        status: node.status,
        tone: "review" as const,
        hasTarget: true,
        hasSource: true,
      },
    })),
  ];

  if (scene.synthesis) {
    baseNodes.push({
      id: synthesisId,
      width: ELK_SYNTHESIS_WIDTH,
      height: ELK_SYNTHESIS_HEIGHT,
      stage: "synthesis",
      selected: synthesisId === selectedNodeId,
      data: {
        kind: "card",
        sourceId: synthesisId,
        selectable: true,
        kicker: "Synthesis",
        title: scene.synthesis.label,
        model: scene.synthesis.model,
        summary: scene.synthesis.summary,
        status: scene.synthesis.status,
        tone: "synthesis",
        hasTarget: true,
      },
    });
  }

  const elkGraph = {
    id: "workflow-root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.padding": "[top=40,left=40,bottom=40,right=40]",
      "elk.spacing.nodeNode": "40",
      "org.eclipse.elk.layered.spacing.nodeNodeBetweenLayers": "110",
      "org.eclipse.elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "org.eclipse.elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
    },
    children: baseNodes.map((node) => ({
      id: node.id,
      width: node.width,
      height: node.height,
    })),
    edges: [
      ...scene.experts.map((node) => ({
        id: `edge-input-${node.id}`,
        sources: ["scene-input"],
        targets: [node.id],
      })),
      ...scene.experts.map((node) => ({
        id: `edge-${node.id}-pool`,
        sources: [node.id],
        targets: ["scene-review-context"],
      })),
      ...scene.reviews.map((node) => ({
        id: `edge-pool-${node.id}`,
        sources: ["scene-review-context"],
        targets: [node.id],
      })),
      ...(scene.synthesis
        ? scene.reviews.map((node) => ({
            id: `edge-${node.id}-${synthesisId}`,
            sources: [node.id],
            targets: [synthesisId],
          }))
        : []),
    ],
  };

  const layout = await elk.layout(elkGraph);
  const positioned = new Map(
    (layout.children ?? []).map((node) => [node.id, { x: node.x ?? 0, y: node.y ?? 0 }])
  );

  const nodePositions = new Map<string, { x: number; y: number }>();
  for (const node of baseNodes) {
    const position = positioned.get(node.id) ?? { x: 0, y: 0 };
    let y = position.y;
    if (node.stage === "experts" || node.stage === "peer_review" || node.stage === "synthesis") {
      y += ELK_STAGE_HEADER;
    } else {
      y += Math.round(ELK_STAGE_HEADER * 0.45);
    }
    nodePositions.set(node.id, { x: position.x, y });
  }

  const stageDefinitions = [
    {
      id: "stage-experts",
      title: "Expert Responses",
      subtitle: "Independent first-pass views",
      kicker: "Stage 1",
      tone: "expert" as const,
      members: scene.experts.map((node) => node.id),
      minWidth: ELK_CARD_WIDTH + 72,
    },
    {
      id: "stage-review",
      title: "Peer Review",
      subtitle: "Anonymous critique and ranking",
      kicker: "Stage 2",
      tone: "review" as const,
      members: scene.reviews.map((node) => node.id),
      minWidth: ELK_CARD_WIDTH + 72,
    },
    {
      id: "stage-synthesis",
      title: "Final Synthesis",
      subtitle: "One merged recommendation",
      kicker: "Stage 3",
      tone: "synthesis" as const,
      members: scene.synthesis ? [synthesisId] : [],
      minWidth: ELK_SYNTHESIS_WIDTH + 72,
    },
  ];

  const stageNodes: Node<WorkflowRfNodeData>[] = stageDefinitions
    .map((stage) => {
      const members = stage.members
        .map((id) => {
          const baseNode = baseNodes.find((node) => node.id === id);
          const position = nodePositions.get(id);
          if (!baseNode || !position) return null;
          return {
            left: position.x,
            top: position.y,
            right: position.x + baseNode.width,
            bottom: position.y + baseNode.height,
          };
        })
        .filter(Boolean) as Array<{ left: number; top: number; right: number; bottom: number }>;

      if (!members.length) return null;

      const minLeft = Math.min(...members.map((member) => member.left));
      const minTop = Math.min(...members.map((member) => member.top));
      const maxRight = Math.max(...members.map((member) => member.right));
      const maxBottom = Math.max(...members.map((member) => member.bottom));
      const width = Math.max(stage.minWidth, maxRight - minLeft + 56);
      const height = maxBottom - minTop + ELK_STAGE_HEADER + 40;

      return {
        id: stage.id,
        type: "stage",
        selectable: false,
        draggable: false,
        position: {
          x: minLeft - 28,
          y: minTop - ELK_STAGE_HEADER - 20,
        },
        width,
        height,
        data: {
          kind: "stage",
          title: stage.title,
          subtitle: stage.subtitle,
          kicker: stage.kicker,
          tone: stage.tone,
        },
        style: {
          width,
          height,
          zIndex: 0,
        },
      } satisfies Node<WorkflowRfNodeData>;
    })
    .filter(Boolean) as Node<WorkflowRfNodeData>[];

  const flowNodes: Node<WorkflowRfNodeData>[] = [
    ...stageNodes,
    ...baseNodes.map((node) => {
      const position = nodePositions.get(node.id) ?? { x: 0, y: 0 };
      return {
        id: node.id,
        type: node.data.kind === "hub" ? "card" : "card",
        draggable: false,
        selectable: false,
        position,
        data: node.data,
        className: node.selected ? "workflow-node-selected" : "",
        style: {
          width: node.width,
          height: node.height,
          zIndex: node.data.kind === "hub" ? 1 : 2,
        },
      } satisfies Node<WorkflowRfNodeData>;
    }),
  ];

  const flowEdges: Edge[] = (layout.edges ?? []).map((edge) => ({
    id: edge.id,
    source: edge.sources?.[0] ?? "",
    target: edge.targets?.[0] ?? "",
    type: "smoothstep",
    animated: false,
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    markerEnd: {
      type: MarkerType.ArrowClosed,
      width: 18,
      height: 18,
      color: "rgba(41, 122, 255, 0.68)",
    },
    style: {
      strokeWidth: 2.5,
      stroke: "rgba(41, 122, 255, 0.68)",
    },
    pathOptions: {
      offset: 28,
      borderRadius: 18,
    },
  }));

  return { nodes: flowNodes, edges: flowEdges };
}

function CouncilGraphCanvas({
  scene,
  selectedNodeId,
  onSelect,
}: {
  scene: GraphScene;
  selectedNodeId: string | null;
  onSelect: (nodeId: string) => void;
}) {
  const [nodes, setNodes] = useState<Node<WorkflowRfNodeData>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const { fitView } = useReactFlow();

  useEffect(() => {
    let cancelled = false;

    async function runLayout() {
      const graph = await buildWorkflowLayout(scene, selectedNodeId);
      if (cancelled) return;
      setNodes(graph.nodes);
      setEdges(graph.edges);
      requestAnimationFrame(() => {
        void fitView({
          padding: 0.08,
          minZoom: 0.55,
          maxZoom: 1.45,
          duration: 0,
        });
      });
    }

    void runLayout();
    return () => {
      cancelled = true;
    };
  }, [scene, selectedNodeId, fitView]);

  return (
    <div className="graph-flow-shell">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={workflowNodeTypes}
        fitView
        fitViewOptions={{ padding: 0.08, minZoom: 0.55, maxZoom: 1.45 }}
        panOnDrag
        zoomOnScroll
        zoomOnDoubleClick={false}
        zoomOnPinch
        minZoom={0.45}
        maxZoom={1.8}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => {
          const data = node.data as WorkflowRfNodeData;
          if (data.kind === "stage" || !data.selectable) return;
          onSelect(data.sourceId);
        }}
      >
        <Background gap={22} size={1} color="rgba(41, 122, 255, 0.08)" />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  );
}

function CouncilGraph({
  scene,
  selectedNodeId,
  onSelect,
}: {
  scene: GraphScene | null;
  selectedNodeId: string | null;
  onSelect: (nodeId: string) => void;
}) {
  if (!scene) {
    return <div className="graph-empty-state">No workflow is selected yet.</div>;
  }

  return (
    <div className="graph-visual">
      <div className="graph-stage-strip">
        {scene.stageBadges.map((badge) => (
          <div key={badge.stage} className={`graph-stage-chip ${statusClass(badge.status)}`}>
            <span>{badge.label}</span>
            <strong>{badge.meta}</strong>
          </div>
        ))}
      </div>

      <ReactFlowProvider>
        <CouncilGraphCanvas scene={scene} selectedNodeId={selectedNodeId} onSelect={onSelect} />
      </ReactFlowProvider>
    </div>
  );
}

function InspectorPanel({
  snapshot,
  selectedConfig,
  target,
}: {
  snapshot: RunSnapshot | null;
  selectedConfig: CouncilConfig | null;
  target: InspectorTarget | null;
}) {
  if (!target) {
    return (
      <div className="empty-state" style={{ marginTop: 14 }}>
        Select a node in the graph. The inspector stays pinned so you can compare nodes without losing context.
      </div>
    );
  }

  if (target.source === "preview") {
    const node = target.node;
    return (
      <div className="inspector-body" style={{ marginTop: 16 }}>
        <div className="toolbar-line">
          <span className="status-pill configured">configured</span>
          <span className="soft">{stageTitle(node.stage)}</span>
        </div>

        <div>
          <h3 className="inspector-title">{node.label}</h3>
          <div className="soft">{node.model}</div>
        </div>

        <div className="inspector-list">
          <div className="inspector-block">
            <p className="mini-title">Role In Workflow</p>
            <p className="inspector-copy">{nodeRoleDescription(node.node_type)}</p>
          </div>

          <div className="inspector-block">
            <p className="mini-title">Persona</p>
            <p className="inspector-copy">{node.persona}</p>
          </div>

          <div className="inspector-block">
            <p className="mini-title">Prompt</p>
            <MarkdownBlock content={node.prompt} />
          </div>
        </div>
      </div>
    );
  }

  const node = target.node;
  const output = node.output;

  return (
    <div className="inspector-body" style={{ marginTop: 16 }}>
      <div className="toolbar-line">
        <span className={`status-pill ${statusClass(node.status)}`}>{node.status}</span>
        <span className="soft">{stageTitle(node.stage)}</span>
      </div>

      <div>
        <h3 className="inspector-title">{node.label}</h3>
        <div className="soft">{node.model}</div>
      </div>

      <div className="inspector-list">
        <div className="inspector-block">
          <p className="mini-title">Role In Workflow</p>
          <p className="inspector-copy">{nodeRoleDescription(node.node_type)}</p>
        </div>

        {node.persona ? (
          <div className="inspector-block">
            <p className="mini-title">Persona</p>
            <p className="inspector-copy">{node.persona}</p>
          </div>
        ) : null}

        {isExpertOutputValue(output) ? (
          <>
            <div className="inspector-block">
              <p className="mini-title">Expert Answer</p>
              <MarkdownBlock content={output.answer} />
            </div>
            <div className="inspector-block">
              <p className="mini-title">Claims</p>
              <ul className="inspector-listing">
                {output.claims.map((claim, index) => (
                  <li key={index}>{claim}</li>
                ))}
              </ul>
            </div>
            <div className="inspector-block">
              <p className="mini-title">Uncertainties</p>
              <ul className="inspector-listing">
                {output.uncertainties.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
            <div className="inspector-block">
              <p className="mini-title">Evidence</p>
              {output.citations.length ? (
                <ul className="inspector-listing">
                  {output.citations.map((citation, index) => (
                    <li key={index}>{citation}</li>
                  ))}
                </ul>
              ) : (
                <p className="inspector-copy">No citations were surfaced for this node.</p>
              )}
              <div className="inspector-metric">Confidence: {Math.round(output.confidence * 100)}%</div>
            </div>
          </>
        ) : null}

        {isReviewOutputValue(output) && snapshot ? (
          <>
            <div className="inspector-block">
              <p className="mini-title">Review Summary</p>
              <MarkdownBlock content={output.summary} />
            </div>
            <div className="inspector-block">
              <p className="mini-title">Dimension Winners</p>
              <div className="inspector-metric-grid">
                <div>
                  <span className="soft">Overall</span>
                  <strong>{formatDimensionWinner(snapshot, output.best_overall_expert_id)}</strong>
                </div>
                <div>
                  <span className="soft">Architecture</span>
                  <strong>{formatDimensionWinner(snapshot, output.best_for_architecture_expert_id)}</strong>
                </div>
                <div>
                  <span className="soft">Execution</span>
                  <strong>{formatDimensionWinner(snapshot, output.best_for_execution_expert_id)}</strong>
                </div>
                <div>
                  <span className="soft">Clarity</span>
                  <strong>{formatDimensionWinner(snapshot, output.best_for_clarity_expert_id)}</strong>
                </div>
              </div>
            </div>
            <div className="inspector-block">
              <p className="mini-title">Ranked Ordering</p>
              <ol className="inspector-listing inspector-ordered">
                {output.ranking_expert_ids.map((expertId, index) => (
                  <li key={`${expertId}-${index}`}>
                    <strong>{expertLabel(snapshot, expertId)}</strong>
                    <span className="soft">
                      {output.ranking_labels[index] ? ` reviewed as ${output.ranking_labels[index]}` : ""}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
            <div className="inspector-block">
              <p className="mini-title">Merge Recommendations</p>
              {output.merge_recommendations.length ? (
                <ul className="inspector-listing">
                  {output.merge_recommendations.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              ) : (
                <p className="inspector-copy">This reviewer did not propose a specific merge.</p>
              )}
            </div>
            <div className="inspector-block">
              <p className="mini-title">Critical Disagreements</p>
              {output.critical_disagreements.length ? (
                <ul className="inspector-listing">
                  {output.critical_disagreements.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              ) : (
                <p className="inspector-copy">No major unresolved disagreement was called out here.</p>
              )}
            </div>
            <div className="inspector-block">
              <p className="mini-title">Per-Response Feedback</p>
              <div className="inspector-feedback-list">
                {Object.entries(output.per_response_feedback).map(([label, feedback]) => (
                  <div key={label} className="inspector-feedback-item">
                    <strong>{responseFeedbackTitle(snapshot, label)}</strong>
                    <MarkdownBlock content={feedback} />
                  </div>
                ))}
              </div>
            </div>
          </>
        ) : null}

        {isSynthesisOutputValue(output) ? (
          <div className="inspector-block">
            <p className="mini-title">Synthesized Answer</p>
            <MarkdownBlock content={output.text} />
          </div>
        ) : null}

        {!isExpertOutputValue(output) && !isReviewOutputValue(output) && !isSynthesisOutputValue(output) ? (
          <div className="inspector-block">
            <p className="mini-title">Captured Output</p>
            {typeof output === "string" ? (
              <MarkdownBlock content={output} />
            ) : node.output_preview ? (
              <MarkdownBlock content={node.output_preview} />
            ) : (
              <p className="inspector-copy">No output has been captured for this node yet.</p>
            )}
          </div>
        ) : null}

        {node.error ? (
          <div className="inspector-block inspector-block-danger">
            <p className="mini-title">Error / Reason</p>
            <p className="inspector-copy">{node.error}</p>
          </div>
        ) : null}

        <div className="inspector-block">
          <p className="mini-title">Timing</p>
          <div className="inspector-timing">
            <span>Started</span>
            <strong>{formatDate(node.started_at)}</strong>
            <span>Completed</span>
            <strong>{formatDate(node.completed_at)}</strong>
          </div>
        </div>
      </div>
    </div>
  );
}

function HistoryDrawer({
  open,
  runs,
  selectedRunId,
  clearingHistory,
  deletingRunId,
  onClose,
  onSelectRun,
  onDeleteRun,
  onClearHistory,
}: {
  open: boolean;
  runs: RunSummary[];
  selectedRunId: string | null;
  clearingHistory: boolean;
  deletingRunId: string | null;
  onClose: () => void;
  onSelectRun: (runId: string) => void;
  onDeleteRun: (run: RunSummary) => void;
  onClearHistory: () => void;
}) {
  return (
    <>
      <div className={`history-backdrop ${open ? "open" : ""}`} onClick={onClose} />
      <aside className={`history-drawer ${open ? "open" : ""}`}>
        <div className="history-drawer-head">
          <div>
            <p className="eyebrow">Run History</p>
            <h2 className="history-drawer-title">Past Runs</h2>
          </div>
          <div className="toolbar-line">
            <button type="button" className="button-tertiary" onClick={onClearHistory} disabled={clearingHistory || !runs.some((run) => TERMINAL_RUN_STATUSES.has(run.status))}>
              {clearingHistory ? "Clearing..." : "Clear Finished"}
            </button>
            <button type="button" className="button-tertiary" onClick={onClose}>
              Close
            </button>
          </div>
        </div>

        <div className="history-list">
          {runs.length === 0 ? (
            <div className="empty-state">No runs yet. Start a run to populate history.</div>
          ) : (
            runs.map((run) => (
              <div key={run.run_id} className={`history-item history-card ${selectedRunId === run.run_id ? "selected" : ""}`}>
                <button type="button" className="history-main" onClick={() => onSelectRun(run.run_id)}>
                  <div className="toolbar-line">
                    <span className={`status-pill ${statusClass(run.status)}`}>{run.status}</span>
                    <span className="soft">{run.config_name}</span>
                  </div>
                  <p className="history-query">{run.query}</p>
                  <div className="soft" style={{ fontSize: 13 }}>{formatDate(run.updated_at)}</div>
                </button>
                <button
                  type="button"
                  className="history-delete"
                  onClick={() => onDeleteRun(run)}
                  disabled={!TERMINAL_RUN_STATUSES.has(run.status) || deletingRunId === run.run_id}
                >
                  {deletingRunId === run.run_id ? "Deleting..." : "Delete"}
                </button>
              </div>
            ))
          )}
        </div>
      </aside>
    </>
  );
}

export function CouncilConsole() {
  const [configs, setConfigs] = useState<CouncilConfig[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedConfigId, setSelectedConfigId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [graphMode, setGraphMode] = useState<GraphMode>("council");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [selectedRunNodeId, setSelectedRunNodeId] = useState<string | null>(null);
  const [selectedPreviewNodeId, setSelectedPreviewNodeId] = useState<string | null>(null);
  const [query, setQuery] = useState(
    "A two-person startup wants to build an AI assistant for interior designers. What should the 30-day validation plan, MVP scope, pricing test, and biggest risks be?"
  );
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [clearingHistory, setClearingHistory] = useState(false);
  const [deletingRunId, setDeletingRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lastEventId = useRef(0);
  const snapshotRef = useRef<RunSnapshot | null>(null);
  const queryInputRef = useRef<HTMLTextAreaElement | null>(null);
  const inspectorRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    void initialize();
  }, []);

  useEffect(() => {
    snapshotRef.current = snapshot;
  }, [snapshot]);

  const selectedConfig = useMemo(
    () => configs.find((config) => config.id === selectedConfigId) ?? configs[0] ?? null,
    [configs, selectedConfigId]
  );

  const previewNodesByStage = useMemo(
    () => (selectedConfig ? buildPreviewNodes(selectedConfig) : { experts: [], peer_review: [], synthesis: [] }),
    [selectedConfig]
  );
  const previewNodes = useMemo(() => previewNodeMap(previewNodesByStage), [previewNodesByStage]);

  useEffect(() => {
    if (!selectedConfig) return;
    setSelectedPreviewNodeId((current) => {
      if (current && previewNodes[current]) return current;
      return previewNodesByStage.experts[0]?.node_id ?? previewNodesByStage.synthesis[0]?.node_id ?? null;
    });
  }, [selectedConfig, previewNodes, previewNodesByStage]);

  useEffect(() => {
    if (!snapshot) return;
    setSelectedRunNodeId((current) => current ?? sortedNodes(snapshot)[0]?.node_id ?? null);
  }, [snapshot]);

  useEffect(() => {
    if (!snapshot || !["pending", "running"].includes(snapshot.status)) return;
    const runId = snapshot.run_id;
    lastEventId.current = snapshot.latest_event_id ?? 0;
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let closed = false;

    const openStream = () => {
      source = new EventSource(eventStreamUrl(runId, lastEventId.current));
      source.onmessage = () => undefined;
      source.addEventListener("run_started", handleEvent as EventListener);
      source.addEventListener("stage_started", handleEvent as EventListener);
      source.addEventListener("node_started", handleEvent as EventListener);
      source.addEventListener("node_token", handleEvent as EventListener);
      source.addEventListener("node_completed", handleEvent as EventListener);
      source.addEventListener("node_failed", handleEvent as EventListener);
      source.addEventListener("node_skipped", handleEvent as EventListener);
      source.addEventListener("stage_completed", handleEvent as EventListener);
      source.addEventListener("stage_failed", handleEvent as EventListener);
      source.addEventListener("run_completed", handleEvent as EventListener);
      source.addEventListener("run_failed", handleEvent as EventListener);
      source.addEventListener("run_canceled", handleEvent as EventListener);
      source.onerror = () => {
        source?.close();
        if (closed) return;
        const current = snapshotRef.current;
        if (!current || current.run_id !== runId) return;
        if (!["pending", "running"].includes(current.status)) return;
        reconnectTimer = window.setTimeout(openStream, 1500);
      };
    };

    function handleEvent(message: MessageEvent<string>) {
      const event = JSON.parse(message.data) as RunEvent;
      const eventId = Number(event.id ?? 0);
      if (eventId && eventId <= lastEventId.current) {
        return;
      }
      lastEventId.current = Math.max(lastEventId.current, eventId);
      setSnapshot((current) => {
        if (!current || current.run_id !== event.run_id) return current;
        return applyEvent(current, event);
      });
      void refreshRuns();
    }

    openStream();

    return () => {
      closed = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      source?.close();
    };
  }, [snapshot?.run_id, snapshot?.status]);

  async function initialize() {
    setLoading(true);
    setError(null);
    try {
      const [configItems, runItems] = await Promise.all([fetchConfigs(), fetchRuns()]);
      setConfigs(configItems);
      if (configItems[0]) {
        setSelectedConfigId(configItems[0].id);
      }
      setRuns(runItems);
      if (runItems[0]) {
        const currentRun = await fetchRun(runItems[0].run_id);
        setSelectedRunId(currentRun.run_id);
        setSelectedConfigId(currentRun.config_snapshot.id);
        setSnapshot(currentRun);
        setQuery(currentRun.query);
        setGraphMode("run");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to initialize console.");
    } finally {
      setLoading(false);
    }
  }

  async function refreshRuns() {
    const runItems = await fetchRuns();
    setRuns(runItems);
  }

  async function handleCreateRun() {
    if (!query.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const run = await createRun({ query, config_id: selectedConfigId || undefined });
      setSnapshot(run);
      setSelectedRunId(run.run_id);
      setSelectedConfigId(run.config_snapshot.id);
      setSelectedRunNodeId(null);
      setGraphMode("run");
      await refreshRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create run.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSelectRun(runId: string) {
    setSelectedRunId(runId);
    setError(null);
    try {
      const run = await fetchRun(runId);
      setSnapshot(run);
      setSelectedConfigId(run.config_snapshot.id);
      setQuery(run.query);
      setSelectedRunNodeId(null);
      setGraphMode("run");
      setHistoryOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load run.");
    }
  }

  async function handleCancelRun() {
    if (!snapshot) return;
    setError(null);
    try {
      await cancelRun(snapshot.run_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to cancel run.");
    }
  }

  async function handleDeleteRun(run: RunSummary) {
    if (!TERMINAL_RUN_STATUSES.has(run.status)) return;
    if (!window.confirm("Delete this run from history? This cannot be undone.")) return;

    setDeletingRunId(run.run_id);
    setError(null);
    try {
      await removeRun(run.run_id);
      const nextRuns = await fetchRuns();
      setRuns(nextRuns);
      if (selectedRunId === run.run_id) {
        setSelectedRunId(null);
        setSnapshot(null);
        setSelectedRunNodeId(null);
        setGraphMode("council");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete run.");
    } finally {
      setDeletingRunId(null);
    }
  }

  async function handleClearHistory() {
    if (!runs.some((run) => TERMINAL_RUN_STATUSES.has(run.status))) return;
    if (!window.confirm("Clear finished runs from history? Active runs will be kept.")) return;

    setClearingHistory(true);
    setError(null);
    try {
      await clearRuns();
      const nextRuns = await fetchRuns();
      setRuns(nextRuns);
      if (selectedRunId && !nextRuns.some((run) => run.run_id === selectedRunId)) {
        setSelectedRunId(null);
        setSnapshot(null);
        setSelectedRunNodeId(null);
        setGraphMode("council");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to clear run history.");
    } finally {
      setClearingHistory(false);
    }
  }

  function focusComposer() {
    queryInputRef.current?.focus();
  }

  function scrollInspectorIntoViewOnSmallScreen() {
    if (window.innerWidth <= 1280) {
      inspectorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function handleSelectGraphNode(nodeId: string) {
    if (graphMode === "run") {
      setSelectedRunNodeId(nodeId);
    } else {
      setSelectedPreviewNodeId(nodeId);
    }
    scrollInspectorIntoViewOnSmallScreen();
  }

  const nodes = useMemo(() => sortedNodes(snapshot), [snapshot]);
  const totalNodes = snapshot ? Object.keys(snapshot.node_snapshots).length : 0;
  const completedNodes = nodes.filter((node) => node.status === "completed").length;
  const runningNodes = nodes.filter((node) => node.status === "running").length;
  const synthesisText = currentSynthesisText(snapshot);

  const previewScene = selectedConfig ? buildPreviewScene(selectedConfig, query) : null;
  const runScene = snapshot ? buildRunScene(snapshot) : null;
  const activeScene = graphMode === "run" && runScene ? runScene : previewScene;
  const expertCount =
    graphMode === "run" && runScene
      ? runScene.experts.length
      : selectedConfig?.experts.filter((expert) => expert.enabled).length ?? 0;

  const selectedInspectorTarget: InspectorTarget | null =
    graphMode === "run" && snapshot && selectedRunNodeId
      ? snapshot.node_snapshots[selectedRunNodeId]
        ? { source: "run", node: snapshot.node_snapshots[selectedRunNodeId] }
        : null
      : graphMode === "council" && selectedPreviewNodeId && previewNodes[selectedPreviewNodeId]
        ? { source: "preview", node: previewNodes[selectedPreviewNodeId] }
        : null;

  useEffect(() => {
    inspectorRef.current?.scrollTo({ top: 0 });
  }, [graphMode, selectedRunNodeId, selectedPreviewNodeId]);

  return (
    <main className="page-shell">
      <HistoryDrawer
        open={historyOpen}
        runs={runs}
        selectedRunId={selectedRunId}
        clearingHistory={clearingHistory}
        deletingRunId={deletingRunId}
        onClose={() => setHistoryOpen(false)}
        onSelectRun={handleSelectRun}
        onDeleteRun={handleDeleteRun}
        onClearHistory={handleClearHistory}
      />

      <div className="workspace-grid">
        <div className="workspace-main">
          <section className="panel topbar-panel">
            <div className="topbar-head">
              <div>
                <p className="eyebrow">Workflow Console</p>
                <h1 className="command-title">llm-council-workflow</h1>
              </div>
              <div className="toolbar-line">
                {snapshot ? <span className={`status-pill ${statusClass(snapshot.status)}`}>{snapshot.status}</span> : null}
                <button type="button" className="history-trigger" onClick={() => setHistoryOpen(true)}>
                  <span>History</span>
                  <strong>{runs.length}</strong>
                </button>
                <Link href="/builder" className="button-secondary link-button">
                  Open Builder
                </Link>
              </div>
            </div>

            <div className="command-grid">
              <div className="command-field">
                <label className="mini-title" htmlFor="council-select">Workflow For Next Run</label>
                <select
                  id="council-select"
                  className="toolbar-select"
                  value={selectedConfigId}
                  onChange={(event) => {
                    setSelectedConfigId(event.target.value);
                    setGraphMode("council");
                  }}
                >
                  {configs.map((config) => (
                    <option key={config.id} value={config.id}>
                      {config.name}
                    </option>
                  ))}
                </select>
                <p className="command-help">
                  This dropdown selects the workflow config the next run will use.
                </p>
              </div>

              <div className="command-query-field">
                <label className="mini-title" htmlFor="council-query">Query</label>
                <textarea
                  id="council-query"
                  ref={queryInputRef}
                  className="query-input"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Ask the workflow something that benefits from multiple perspectives."
                />
              </div>

              <div className="command-actions">
                <button
                  className="button-tertiary"
                  type="button"
                  onClick={handleCancelRun}
                  disabled={!snapshot || snapshot.status !== "running"}
                >
                  Cancel Run
                </button>
                <button
                  className="button-primary"
                  type="button"
                  onClick={handleCreateRun}
                  disabled={submitting || loading}
                >
                  {submitting ? "Launching..." : "Start New Run"}
                </button>
              </div>
            </div>

            <div className="command-footer">
              <div className="toolbar-line">
                <span className="soft">
                  {selectedConfig
                    ? `${selectedConfig.name} · ${selectedConfig.experts.filter((expert) => expert.enabled).length} experts · peer review · final synthesis`
                    : "No workflow selected"}
                </span>
                {error ? <span className="error-inline">{error}</span> : null}
              </div>
              <div className="toolbar-line">
                <span className="soft">Completed nodes: {completedNodes}/{totalNodes}</span>
                <span className="soft">Running now: {runningNodes}</span>
              </div>
            </div>
          </section>

          <section className="panel graph-panel graph-panel-reworked">
            <div className="graph-topbar">
              <div>
                <h2 className="section-title">Workflow Graph</h2>
                <p className="graph-caption">{activeScene?.helperText}</p>
              </div>
              <div className="graph-mode-toggle">
                <button
                  type="button"
                  className={`segmented-button ${graphMode === "council" ? "active" : ""}`}
                  onClick={() => setGraphMode("council")}
                  disabled={!selectedConfig}
                >
                  Selected Workflow
                </button>
                <button
                  type="button"
                  className={`segmented-button ${graphMode === "run" ? "active" : ""}`}
                  onClick={() => setGraphMode("run")}
                  disabled={!snapshot}
                >
                  Selected Run
                </button>
              </div>
            </div>

            <div className="graph-meta-line">
              <span>
                {graphMode === "run" && snapshot
                  ? `Run · ${snapshot.config_snapshot.name}`
                  : selectedConfig
                    ? `Workflow · ${selectedConfig.name}`
                    : "No workflow selected"}
              </span>
              <span>{expertCount} experts</span>
              <span>Click a node to inspect it on the right</span>
            </div>

            <CouncilGraph
              scene={activeScene}
              selectedNodeId={graphMode === "run" ? selectedRunNodeId : selectedPreviewNodeId}
              onSelect={handleSelectGraphNode}
            />
          </section>

          <section className="panel synthesis-panel">
            <div className="synthesis-head">
              <div>
                <p className="eyebrow">Primary Output</p>
                <h2 className="synthesis-title">Final Synthesis</h2>
              </div>
              <div className="toolbar-line">
                {snapshot?.status === "completed" ? <span className="status-pill completed">ready</span> : null}
                {graphMode === "council" ? <span className="status-pill configured">preview</span> : null}
              </div>
            </div>
            <div className="synthesis-body">
              {graphMode === "run" && synthesisText ? (
                <MarkdownBlock content={synthesisText} />
              ) : graphMode === "run" ? (
                <p className="answer-placeholder">Waiting for the synthesis node to produce output.</p>
              ) : selectedConfig ? (
                <div className="synthesis-preview-copy">
                  <p>
                    This area becomes the live final answer once you start a run. Right now you are previewing
                    <strong> {selectedConfig.name}</strong>.
                  </p>
                  <p>
                    The graph above shows exactly which expert and review nodes feed the synthesis step.
                  </p>
                </div>
              ) : (
                <p className="answer-placeholder">Select a workflow to preview its structure.</p>
              )}
            </div>
          </section>
        </div>

        <aside ref={inspectorRef} className="panel inspector inspector-sticky">
          <section>
            <h2 className="section-title">Inspector</h2>
            <InspectorPanel
              snapshot={snapshot}
              selectedConfig={selectedConfig}
              target={selectedInspectorTarget}
            />
          </section>
        </aside>
      </div>
    </main>
  );
}
