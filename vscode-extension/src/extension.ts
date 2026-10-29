import { readFileSync } from "node:fs";
import { join } from "node:path";
import * as vscode from "vscode";
import { version } from "../package.json";
import { Tokens } from "./auth";
import {
  anthropicMessagesReplies,
  anthropicMessagesRequest,
  chatCompletionsReplies,
  chatCompletionsRequest,
  estimateTokens,
  responsesReplies,
  responsesRequest,
  type Reply,
} from "./formats";
import { streamEvents } from "./gateway";

const APIS = ["anthropic_messages", "chat_completions", "responses"] as const;

export interface ModelSetting {
  readonly id: string;
  readonly name?: string;
  readonly family?: string;
  readonly api: (typeof APIS)[number];
  readonly maxInputTokens?: number;
  readonly maxOutputTokens?: number;
}

export interface Settings {
  readonly workspace: string;
  readonly models: readonly ModelSetting[];
  readonly authCommand: string;
  /** The Model Provider Service the models belong to, or "" for the workspace's own models. */
  readonly provider: string;
}

/** Routes a request to a Model Provider Service; its models aren't served without it. */
export const PROVIDER_HEADER = "Databricks-Model-Provider-Service";

const tokenLimits = (model: ModelSetting): Partial<ModelSetting> =>
  Object.fromEntries(
    (["maxInputTokens", "maxOutputTokens"] as const).flatMap((key) => {
      const value: unknown = model[key];
      return typeof value === "number" && Number.isInteger(value) && value > 0 ? [[key, value]] : [];
    }),
  );

/** `ug configure` writes this file into the extension it packs, so each install carries its own setup. */
export const CONFIG_FILE = "ug-config.json";

/** Reads the config file's text; a missing or malformed file gives no models. */
export const readConfig = (text: string | undefined): Settings => {
  let config: Record<string, unknown> = {};
  try {
    const parsed: unknown = JSON.parse(text ?? "{}");
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      config = parsed as Record<string, unknown>;
    }
  } catch {
    // Treated like a missing file.
  }
  const get = (key: string) => config[key];
  const models = get("models");
  return {
    workspace: String(get("workspace") ?? "").replace(/\/+$/, ""),
    models: (Array.isArray(models) ? models : [])
      .filter(
        (model): model is ModelSetting =>
          typeof model?.id === "string" && model.id !== "" && APIS.includes(model.api),
      )
      .map((model) => ({
        id: model.id,
        ...(typeof model.name === "string" && model.name.trim() ? { name: model.name.trim() } : {}),
        ...(typeof model.family === "string" && model.family ? { family: model.family } : {}),
        api: model.api,
        ...tokenLimits(model),
      })),
    authCommand: String(get("authCommand") ?? ""),
    provider: typeof get("provider") === "string" ? String(get("provider")) : "",
  };
};

/** The gateway attributes requests by this User-Agent. */
export const userAgent = (copilotChatVersion: unknown): string =>
  `ucode/${version} githubcopilotchat${typeof copilotChatVersion === "string" ? `/${copilotChatVersion}` : ""}`;

type Model = vscode.LanguageModelChatInformation & { readonly api: ModelSetting["api"] };

export class DatabricksModels implements vscode.LanguageModelChatProvider<Model> {
  private tokens: Tokens | undefined;

  constructor(
    private readonly settings: () => Settings,
    private readonly log: (line: string) => void,
    private readonly headers: () => Record<string, string> = () => ({}),
  ) {}

  async provideLanguageModelChatInformation(): Promise<Model[]> {
    const { workspace, models, authCommand } = this.settings();
    if (!workspace || !authCommand || models.length === 0) {
      this.log("No Databricks models: run `ug configure --agents vscode` to set them up.");
      return [];
    }
    return models.map(({ id, name, family, api, maxInputTokens, maxOutputTokens }) => ({
      id,
      api,
      name: name ?? id,
      // Copilot Chat tunes its prompts and tools by family, so this is the model's own name.
      family: family ?? id.split(".").pop() ?? id,
      version,
      detail: "Databricks",
      // The gateway reports no limits, so a model ug configure has none for gets Copilot's 128K default.
      maxInputTokens: maxInputTokens ?? 128_000,
      maxOutputTokens: maxOutputTokens ?? 32_000,
      capabilities: { toolCalling: true, imageInput: api !== "chat_completions" || id.includes("claude") },
    }));
  }

  async provideLanguageModelChatResponse(
    model: Model,
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken,
  ): Promise<void> {
    const { workspace, authCommand, provider } = this.settings();
    this.tokens ??= new Tokens(authCommand, (expiresAt) => this.log(`Fetched a Databricks token (expires ${new Date(expiresAt).toISOString()})`));
    const turn = { model: model.id, messages, tools: options.tools ?? [], toolRequired: options.toolMode === vscode.LanguageModelChatToolMode.Required };
    const abort = new AbortController();
    const cancelled = token.onCancellationRequested(() => abort.abort());
    const headers = { ...this.headers(), ...(provider ? { [PROVIDER_HEADER]: provider } : {}) };
    const tokens = this.tokens;
    const stream = (path: string, body: object, extra: Record<string, string> = {}) =>
      streamEvents(workspace, path, body, tokens, { ...headers, ...extra }, abort.signal);
    // The gateway serves a Model Provider Service's OpenAI models only on its OpenAI route.
    const openai = provider ? "/ai-gateway/openai/v1" : "/ai-gateway/mlflow/v1";
    try {
      const replies: AsyncIterable<Reply> =
        model.api === "anthropic_messages"
          ? anthropicMessagesReplies(
              stream("/ai-gateway/anthropic/v1/messages", anthropicMessagesRequest(turn, model.maxOutputTokens), {
                "anthropic-version": "2023-06-01",
              }),
            )
          : model.api === "responses"
            ? responsesReplies(stream(`${openai}/responses`, responsesRequest(turn)))
            : chatCompletionsReplies(stream(`${openai}/chat/completions`, chatCompletionsRequest(turn)));
      for await (const reply of replies) {
        progress.report(reply);
      }
    } catch (error) {
      if (!token.isCancellationRequested) {
        this.log(`${model.id} failed: ${error instanceof Error ? error.message : String(error)}`);
        throw error;
      }
    } finally {
      cancelled.dispose();
    }
  }

  async provideTokenCount(_model: Model, input: string | vscode.LanguageModelChatRequestMessage): Promise<number> {
    return estimateTokens(input);
  }
}

const readConfigFile = (path: string): string | undefined => {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return undefined;
  }
};

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Unity Gateway", { log: true });
  // Read once: ug changes the file only by reinstalling the extension, which takes a restart to load.
  const settings = readConfig(readConfigFile(join(context.extensionPath, CONFIG_FILE)));
  const headers = () => ({ "User-Agent": userAgent(vscode.extensions.getExtension("GitHub.copilot-chat")?.packageJSON?.version) });
  const provider = new DatabricksModels(() => settings, (line) => output.info(line), headers);
  context.subscriptions.push(output, vscode.lm.registerLanguageModelChatProvider("databricks-unity-gateway", provider));
}
