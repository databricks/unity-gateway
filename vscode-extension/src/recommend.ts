import type { Tokens } from "./auth";

/** Answers with the agent and model the caller's budget tier allows (see `fetch_model_recommendation` in ug). */
export const RECOMMEND_PATH = "/api/ai-gateway/v2/coding-agent-configs:recommendModel";

export const COPILOT_AGENT = "CODING_AGENT_GITHUB_COPILOT";

/** How often a running VS Code checks the tier again: a window stays open for days, and spend moves meanwhile. */
export const RECHECK_INTERVAL_MS = 15 * 60_000;

const TIMEOUT_MS = 30_000;

export interface Recommendation {
  readonly agent?: string;
  readonly model?: string;
}

/** The caller's recommendation, or undefined when the workspace has no config (or the gateway predates the API). */
export const fetchRecommendation = async (
  workspace: string,
  tokens: Tokens,
  headers: Readonly<Record<string, string>>,
): Promise<Recommendation | undefined> => {
  const post = (token: string) =>
    fetch(`${workspace}${RECOMMEND_PATH}`, {
      method: "POST",
      headers: { ...headers, Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: "{}",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  let token = await tokens.get();
  let response = await post(token);
  if (response.status === 401) {
    await response.body?.cancel();
    token = await tokens.renew(token);
    response = await post(token);
  }
  if (response.status === 404) {
    await response.body?.cancel();
    return undefined;
  }
  const body = await response.text();
  if (!response.ok) {
    throw new Error(`AI Gateway returned ${response.status}: ${body.slice(0, 300)}`);
  }
  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    // A host that serves several workspaces answers with a web page when it can't tell which one is meant.
    throw new Error(`AI Gateway answered with something other than JSON: ${body.slice(0, 100)}`);
  }
  const { recommended_agent: agent, recommended_model: model } = (payload && typeof payload === "object" ? payload : {}) as Record<string, unknown>;
  return {
    ...(typeof agent === "string" && agent ? { agent } : {}),
    ...(typeof model === "string" && model ? { model } : {}),
  };
};

const normalized = (id: string) => (id.split("[", 1)[0] ?? id).toLowerCase();

/** The id in `ids` that `requested` names, by the rules `ug copilot --model` matches with (`resolve_copilot_model`). */
export const matchModel = (requested: string, ids: readonly string[]): string | undefined => {
  const name = normalized(requested);
  const rules: ((id: string) => boolean)[] = [
    (id) => id === requested,
    (id) => normalized(id) === name,
    (id) => normalized(id) === `system.ai.${name}`,
    (id) => normalized(id) === `databricks-${name}`,
  ];
  for (const rule of rules) {
    const found = ids.filter(rule);
    if (found.length > 0) {
      return found.find((id) => id.startsWith("system.ai.")) ?? found[0];
    }
  }
  return undefined;
};

/**
 * The model new chats should start on: the tier's when it is for Copilot and Copilot has it, else the
 * config's default. A tier for another agent is ignored, as `ug copilot` ignores it: VS Code can't switch agents.
 */
export const chooseDefault = (
  recommendation: Recommendation | undefined,
  ids: readonly string[],
  configDefault: string | undefined,
): string | undefined => {
  const { agent, model } = recommendation ?? {};
  const tierModel = model && (agent === undefined || agent === COPILOT_AGENT) ? matchModel(model, ids) : undefined;
  return tierModel ?? configDefault;
};

/**
 * Keeps `chat.defaultModel` on the tier's model. The first check falls back to the config's default when
 * the read fails, as `ug copilot` does; a later failed check keeps what is set.
 */
export class SmartDefaults {
  private chosen: string | undefined;
  private checked = false;
  private lastFailure: string | undefined;

  constructor(
    private readonly recommend: () => Promise<Recommendation | undefined>,
    private readonly ids: readonly string[],
    private readonly configDefault: string | undefined,
    private readonly apply: (model: string | undefined) => Promise<void>,
    private readonly log: (line: string) => void,
  ) {}

  async check(): Promise<void> {
    let recommendation: Recommendation | undefined;
    try {
      recommendation = await this.recommend();
      this.lastFailure = undefined;
    } catch (error) {
      const failure = error instanceof Error ? error.message : String(error);
      if (failure !== this.lastFailure) {
        this.log(`Could not check your workspace's budget tier (${failure}); new chats keep their default model.`);
        this.lastFailure = failure;
      }
      if (this.checked) {
        return;
      }
    }
    const chosen = chooseDefault(recommendation, this.ids, this.configDefault);
    if (this.checked && chosen === this.chosen) {
      return;
    }
    if (this.checked || chosen !== this.configDefault) {
      this.log(
        chosen && chosen !== this.configDefault
          ? `Your workspace's budget tier starts new chats on ${chosen}.`
          : `New chats start on ${chosen ?? "VS Code's own default model"} again.`,
      );
    }
    this.checked = true;
    this.chosen = chosen;
    await this.apply(chosen);
  }
}
