import type { CouncilConfig, RunSnapshot, RunSummary } from "@/lib/types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function fetchConfigs(): Promise<CouncilConfig[]> {
  const data = await request<{ items: CouncilConfig[] }>("/api/configs");
  return data.items;
}

export async function createConfig(config: CouncilConfig): Promise<CouncilConfig> {
  return request<CouncilConfig>("/api/configs", {
    method: "POST",
    body: JSON.stringify(config),
  });
}

export async function fetchRuns(): Promise<RunSummary[]> {
  const data = await request<{ items: RunSummary[] }>("/api/runs");
  return data.items;
}

export async function fetchRun(runId: string): Promise<RunSnapshot> {
  return request<RunSnapshot>(`/api/runs/${runId}`);
}

export async function createRun(payload: {
  query: string;
  config_id?: string;
  config?: CouncilConfig;
}): Promise<RunSnapshot> {
  return request<RunSnapshot>("/api/runs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function saveConfig(config: CouncilConfig): Promise<CouncilConfig> {
  return request<CouncilConfig>(`/api/configs/${config.id}`, {
    method: "PUT",
    body: JSON.stringify(config),
  });
}

export async function deleteConfig(configId: string): Promise<void> {
  await request(`/api/configs/${configId}`, {
    method: "DELETE",
  });
}

export async function cancelRun(runId: string): Promise<void> {
  await request(`/api/runs/${runId}/cancel`, {
    method: "POST",
  });
}

export async function deleteRun(runId: string): Promise<void> {
  await request(`/api/runs/${runId}`, {
    method: "DELETE",
  });
}

export async function clearRuns(): Promise<number> {
  const response = await request<{ deleted: number }>("/api/runs", {
    method: "DELETE",
  });
  return response.deleted;
}

export function eventStreamUrl(runId: string, afterId = 0): string {
  return `${API_BASE}/api/runs/${runId}/events?after_id=${afterId}`;
}
