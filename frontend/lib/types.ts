export type RunStatus = "pending" | "running" | "completed" | "failed" | "canceled";
export type StageStatus = "pending" | "running" | "completed" | "failed";
export type NodeStatus = "pending" | "running" | "completed" | "failed" | "skipped";
export type StageName = "experts" | "peer_review" | "synthesis";
export type NodeType = "expert" | "review" | "synthesis";

export interface ExpertSpec {
  id: string;
  label: string;
  model: string;
  persona: string;
  system_prompt: string;
  enabled: boolean;
  temperature?: number | null;
  max_tokens?: number | null;
  timeout_seconds: number;
}

export interface CouncilConfig {
  id: string;
  name: string;
  description: string;
  experts: ExpertSpec[];
  review_prompt_template: string;
  synthesis_model: string;
  synthesis_prompt_template: string;
  synthesis_temperature?: number | null;
  synthesis_max_tokens?: number | null;
  synthesis_timeout_seconds: number;
  version: number;
}

export interface StageSnapshot {
  stage: StageName;
  label: string;
  status: StageStatus;
  started_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
  total_nodes: number;
  completed_nodes: number;
  failed_nodes: number;
}

export interface NodeSnapshot {
  node_id: string;
  stage: StageName;
  node_type: NodeType;
  label: string;
  model: string;
  persona?: string | null;
  status: NodeStatus;
  display_order: number;
  started_at?: string | null;
  completed_at?: string | null;
  output_preview: string;
  output?: unknown;
  error?: string | null;
}

export interface ExpertOutput {
  expert_id: string;
  expert_label: string;
  model: string;
  persona: string;
  answer: string;
  claims: string[];
  uncertainties: string[];
  citations: string[];
  confidence: number;
}

export interface ReviewOutput {
  reviewer_id: string;
  reviewer_label: string;
  model: string;
  persona: string;
  ranking_labels: string[];
  ranking_expert_ids: string[];
  best_overall_expert_id?: string | null;
  best_for_architecture_expert_id?: string | null;
  best_for_execution_expert_id?: string | null;
  best_for_clarity_expert_id?: string | null;
  summary: string;
  merge_recommendations: string[];
  critical_disagreements: string[];
  per_response_feedback: Record<string, string>;
}

export interface AggregateReview {
  ranking_expert_ids: string[];
  scores: Record<string, number>;
  best_overall_expert_id?: string | null;
  best_for_architecture_expert_id?: string | null;
  best_for_execution_expert_id?: string | null;
  best_for_clarity_expert_id?: string | null;
  merge_recommendations: string[];
  critical_disagreements: string[];
  summary: string;
}

export interface RunSnapshot {
  run_id: string;
  query: string;
  status: RunStatus;
  latest_event_id: number;
  config_snapshot: CouncilConfig;
  stage_snapshots: Record<string, StageSnapshot>;
  node_snapshots: Record<string, NodeSnapshot>;
  expert_outputs: ExpertOutput[];
  anonymized_responses: Array<{
    label: string;
    expert_id: string;
    expert_label: string;
    answer: string;
    claims: string[];
    uncertainties: string[];
  }>;
  review_outputs: ReviewOutput[];
  aggregate_review?: AggregateReview | null;
  final_answer?: string | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunSummary {
  run_id: string;
  query: string;
  status: RunStatus;
  config_id: string;
  config_name: string;
  created_at: string;
  updated_at: string;
  final_answer_preview?: string | null;
}

export interface RunEvent {
  id?: number | null;
  run_id: string;
  type: string;
  stage?: StageName | null;
  node_id?: string | null;
  timestamp: string;
  payload: Record<string, unknown>;
}
