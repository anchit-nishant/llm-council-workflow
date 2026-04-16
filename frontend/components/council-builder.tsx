"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { createConfig, deleteConfig, fetchConfigs, saveConfig } from "@/lib/api";
import type { CouncilConfig, ExpertSpec } from "@/lib/types";

function cloneConfig(config: CouncilConfig): CouncilConfig {
  return JSON.parse(JSON.stringify(config)) as CouncilConfig;
}

function createExpertDraft(index: number): ExpertSpec {
  const ordinal = index + 1;
  return {
    id: `expert-${ordinal}`,
    label: `Expert ${ordinal}`,
    model: "provider/model-name",
    persona: "Add a focused perspective for this workflow expert.",
    system_prompt: "You are a workflow expert. Contribute a distinct, grounded perspective to the user query.",
    enabled: true,
    timeout_seconds: 150,
  };
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function nextUniqueName(baseName: string, existingNames: string[]): string {
  if (!existingNames.includes(baseName)) {
    return baseName;
  }
  let index = 2;
  while (existingNames.includes(`${baseName} ${index}`)) {
    index += 1;
  }
  return `${baseName} ${index}`;
}

function nextUniqueId(baseName: string, existingIds: string[]): string {
  const baseId = slugify(baseName) || "new-workflow";
  if (!existingIds.includes(baseId)) {
    return baseId;
  }
  let index = 2;
  while (existingIds.includes(`${baseId}-${index}`)) {
    index += 1;
  }
  return `${baseId}-${index}`;
}

function createCouncilDraft(
  existingConfigs: CouncilConfig[],
  source?: CouncilConfig | null
): CouncilConfig {
  const existingNames = existingConfigs.map((config) => config.name);
  const existingIds = existingConfigs.map((config) => config.id);
  const name = nextUniqueName("New Workflow", existingNames);
  const id = nextUniqueId(name, existingIds);

  if (source) {
    return {
      ...cloneConfig(source),
      id,
      name,
      description: source.description || "Describe what this workflow is for.",
      version: 1,
    };
  }

  return {
    id,
    name,
    description: "Describe what this workflow is for.",
    experts: [
      createExpertDraft(0),
      createExpertDraft(1),
      createExpertDraft(2),
      createExpertDraft(3),
    ],
    review_prompt_template:
      "Review the anonymized expert answers for the same user query. Evaluate them on overall quality, architecture rigor, execution practicality, and operator clarity. If more than one answer is good, say what should be merged instead of pretending there is only one valid winner.",
    synthesis_model: "provider/model-name",
    synthesis_prompt_template:
      "Synthesize the best final answer from the expert outputs and the peer reviews. Merge complementary strengths, resolve disagreement directly, and surface uncertainty when it matters.",
    synthesis_timeout_seconds: 180,
    version: 1,
  };
}

export function CouncilBuilder() {
  const [configs, setConfigs] = useState<CouncilConfig[]>([]);
  const [selectedConfigId, setSelectedConfigId] = useState("");
  const [builderConfig, setBuilderConfig] = useState<CouncilConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void initialize();
  }, []);

  async function initialize() {
    setLoading(true);
    setError(null);
    try {
      const items = await fetchConfigs();
      setConfigs(items);
      if (items[0]) {
        setSelectedConfigId(items[0].id);
        setBuilderConfig(cloneConfig(items[0]));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load configs.");
    } finally {
      setLoading(false);
    }
  }

  function updateBuilder(updater: (draft: CouncilConfig) => CouncilConfig) {
    setBuilderConfig((current) => (current ? updater(cloneConfig(current)) : current));
  }

  async function handleSave() {
    if (!builderConfig) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveConfig(builderConfig);
      const items = await fetchConfigs();
      setConfigs(items);
      setSelectedConfigId(saved.id);
      setBuilderConfig(cloneConfig(saved));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save config.");
    } finally {
      setSaving(false);
    }
  }

  async function handleCreateCouncil() {
    setCreating(true);
    setError(null);
    try {
      const draft = createCouncilDraft(configs, builderConfig);
      const created = await createConfig(draft);
      const items = await fetchConfigs();
      setConfigs(items);
      setSelectedConfigId(created.id);
      setBuilderConfig(cloneConfig(created));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create workflow.");
    } finally {
      setCreating(false);
    }
  }

  async function handleDeleteWorkflow() {
    if (!builderConfig) return;
    if (!window.confirm(`Delete "${builderConfig.name}"? This cannot be undone.`)) return;

    setDeleting(true);
    setError(null);
    try {
      await deleteConfig(builderConfig.id);
      const items = await fetchConfigs();
      setConfigs(items);
      const next = items[0] ?? null;
      setSelectedConfigId(next?.id ?? "");
      setBuilderConfig(next ? cloneConfig(next) : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete workflow.");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <main className="page-shell">
      <div className="builder-shell">
        <aside className="panel sidebar">
          <section>
            <div className="toolbar-line" style={{ justifyContent: "space-between" }}>
              <h2 className="section-title">Workflow Builder</h2>
              <div className="toolbar-line">
                <button
                  className="button-secondary"
                  type="button"
                  onClick={handleCreateCouncil}
                  disabled={loading || creating}
                >
                  {creating ? "Creating..." : "New Workflow"}
                </button>
                <button
                  className="button-tertiary"
                  type="button"
                  onClick={handleDeleteWorkflow}
                  disabled={!builderConfig || deleting}
                >
                  {deleting ? "Deleting..." : "Delete Workflow"}
                </button>
                <Link href="/" className="mini-link">
                  Back to Runs
                </Link>
              </div>
            </div>
            <div className="history-list" style={{ marginTop: 14 }}>
              {configs.map((config) => (
                <button
                  key={config.id}
                  type="button"
                  className={`history-item ${selectedConfigId === config.id ? "selected" : ""}`}
                  onClick={() => {
                    setSelectedConfigId(config.id);
                    setBuilderConfig(cloneConfig(config));
                  }}
                >
                  <strong>{config.name}</strong>
                  <div className="soft" style={{ marginTop: 6 }}>
                    {config.experts.filter((expert) => expert.enabled).length} enabled experts
                  </div>
                </button>
              ))}
            </div>
          </section>
        </aside>

        <section className="panel builder-panel">
            <div className="toolbar-line" style={{ justifyContent: "space-between" }}>
            <div>
              <p className="eyebrow">Workflow Builder</p>
              <h1 className="hero-title" style={{ fontSize: 34 }}>Edit the workflow config away from the run console.</h1>
            </div>
            <div className="toolbar-line">
              <button
                className="button-secondary"
                type="button"
                onClick={handleCreateCouncil}
                disabled={loading || creating}
              >
                {creating ? "Creating..." : "New Workflow"}
              </button>
              <button
                className="button-secondary"
                type="button"
                onClick={() =>
                  updateBuilder((draft) => ({
                    ...draft,
                    experts: [...draft.experts, createExpertDraft(draft.experts.length)],
                  }))
                }
                disabled={!builderConfig}
              >
                Add Expert
              </button>
              <button
                className="button-tertiary"
                type="button"
                onClick={handleDeleteWorkflow}
                disabled={!builderConfig || deleting}
              >
                {deleting ? "Deleting..." : "Delete Workflow"}
              </button>
              <button className="button-primary" type="button" onClick={handleSave} disabled={!builderConfig || saving || creating}>
                {saving ? "Saving..." : "Save Config"}
              </button>
            </div>
          </div>

          {error ? <div className="empty-state" style={{ marginTop: 16, color: "var(--bad)" }}>{error}</div> : null}
          {loading ? <div className="empty-state" style={{ marginTop: 16 }}>Loading configs...</div> : null}

          {builderConfig ? (
            <div className="builder-grid">
              <div className="builder-column">
                <div className="field">
                  <label htmlFor="builder-name">Workflow Name</label>
                  <input
                    id="builder-name"
                    value={builderConfig.name}
                    onChange={(event) =>
                      updateBuilder((draft) => ({ ...draft, name: event.target.value }))
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor="builder-description">Description</label>
                  <textarea
                    id="builder-description"
                    rows={4}
                    value={builderConfig.description}
                    onChange={(event) =>
                      updateBuilder((draft) => ({ ...draft, description: event.target.value }))
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor="builder-review-prompt">Peer Review Prompt</label>
                  <textarea
                    id="builder-review-prompt"
                    rows={6}
                    value={builderConfig.review_prompt_template}
                    onChange={(event) =>
                      updateBuilder((draft) => ({
                        ...draft,
                        review_prompt_template: event.target.value,
                      }))
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor="builder-synthesis-prompt">Synthesis Prompt</label>
                  <textarea
                    id="builder-synthesis-prompt"
                    rows={6}
                    value={builderConfig.synthesis_prompt_template}
                    onChange={(event) =>
                      updateBuilder((draft) => ({
                        ...draft,
                        synthesis_prompt_template: event.target.value,
                      }))
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor="builder-synthesis-model">Synthesis Model</label>
                  <input
                    id="builder-synthesis-model"
                    placeholder="provider/model-name"
                    value={builderConfig.synthesis_model}
                    onChange={(event) =>
                      updateBuilder((draft) => ({
                        ...draft,
                        synthesis_model: event.target.value,
                      }))
                    }
                  />
                </div>

                {builderConfig.experts.map((expert, index) => (
                  <div key={`${builderConfig.id}:${index}`} className="panel" style={{ padding: 14 }}>
                    <div className="toolbar-line" style={{ justifyContent: "space-between" }}>
                      <strong>{expert.label}</strong>
                      <div className="toolbar-line">
                        <label className="toggle-row">
                          <input
                            type="checkbox"
                            checked={expert.enabled}
                            onChange={(event) =>
                              updateBuilder((draft) => {
                                draft.experts[index].enabled = event.target.checked;
                                return draft;
                              })
                            }
                          />
                          <span className={`status-pill ${expert.enabled ? "completed" : "skipped"}`}>
                            {expert.enabled ? "enabled" : "disabled"}
                          </span>
                        </label>
                        <button
                          className="button-secondary"
                          type="button"
                          onClick={() =>
                            updateBuilder((draft) => ({
                              ...draft,
                              experts: draft.experts.filter((_, expertIndex) => expertIndex !== index),
                            }))
                          }
                          disabled={builderConfig.experts.length <= 1}
                        >
                          Remove
                        </button>
                      </div>
                    </div>

                    <div className="builder-fields" style={{ marginTop: 12 }}>
                      <div className="field">
                        <label>Label</label>
                        <input
                          value={expert.label}
                          onChange={(event) =>
                            updateBuilder((draft) => {
                              draft.experts[index].label = event.target.value;
                              return draft;
                            })
                          }
                        />
                      </div>
                      <div className="field">
                        <label>Expert ID</label>
                        <input
                          value={expert.id}
                          onChange={(event) =>
                            updateBuilder((draft) => {
                              draft.experts[index].id = event.target.value;
                              return draft;
                            })
                          }
                        />
                      </div>
                      <div className="field">
                        <label>Model</label>
                        <input
                          placeholder="provider/model-name"
                          value={expert.model}
                          onChange={(event) =>
                            updateBuilder((draft) => {
                              draft.experts[index].model = event.target.value;
                              return draft;
                            })
                          }
                        />
                      </div>
                      <div className="field">
                        <label>Persona</label>
                        <textarea
                          rows={3}
                          value={expert.persona}
                          onChange={(event) =>
                            updateBuilder((draft) => {
                              draft.experts[index].persona = event.target.value;
                              return draft;
                            })
                          }
                        />
                      </div>
                      <div className="field">
                        <label>System Prompt</label>
                        <textarea
                          rows={5}
                          value={expert.system_prompt}
                          onChange={(event) =>
                            updateBuilder((draft) => {
                              draft.experts[index].system_prompt = event.target.value;
                              return draft;
                            })
                          }
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>

              <div className="builder-column">
                <pre className="json-preview">{JSON.stringify(builderConfig, null, 2)}</pre>
              </div>
            </div>
          ) : null}
        </section>
      </div>
    </main>
  );
}
