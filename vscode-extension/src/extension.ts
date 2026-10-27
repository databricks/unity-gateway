import * as vscode from "vscode";
import packageJson from "../package.json";
import { Tokens } from "./auth";
import { chatCompletionsReplies, chatCompletionsRequest, estimateTokens, responsesReplies, responsesRequest, type Reply } from "./formats";
import { streamEvents } from "./gateway";

export interface ModelSetting {
  readonly id: string;
  readonly api: "chat_completions" | "responses";
}

export interface Settings {
  readonly workspace: string;
  readonly models: readonly ModelSetting[];
  readonly authCommand: string;
}

export const readSettings = (get: (key: string) => unknown): Settings => {
  const models = get("models");
  return {
    workspace: String(get("workspace") ?? "").replace(/\/+$/, ""),
    models: (Array.isArray(models) ? models : []).filter(
      (model): model is ModelSetting =>
        typeof model?.id === "string" && model.id !== "" && (model.api === "chat_completions" || model.api === "responses"),
    ),
    authCommand: String(get("authCommand") ?? ""),
  };
};

type Model = vscode.LanguageModelChatInformation & { readonly api: ModelSetting["api"] };

export class DatabricksModels implements vscode.LanguageModelChatProvider<Model> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeLanguageModelChatInformation = this.changed.event;
  private tokens: Tokens | undefined;

  constructor(
    private readonly settings: () => Settings,
    private readonly log: (line: string) => void,
    private readonly headers: () => Record<string, string> = () => ({}),
  ) {}

  /** Re-reads the settings: drops the cached token and asks VS Code to list the models again. */
  refresh(): void {
    this.tokens = undefined;
    this.changed.fire();
  }

  async provideLanguageModelChatInformation(): Promise<Model[]> {
    const { workspace, models, authCommand } = this.settings();
    if (!workspace || !authCommand || models.length === 0) {
      this.log("No Databricks models: run `ug configure --agents vscode` to set them up.");
      return [];
    }
    return models.map(({ id, api }) => ({
      id,
      api,
      name: id,
      // Copilot Chat tunes its prompts and tools by family, so this is the model's own name.
      family: id.split(".").pop() ?? id,
      version: packageJson.version,
      detail: "Databricks",
      // The gateway reports no limits for these models.
      maxInputTokens: 128_000,
      maxOutputTokens: 32_000,
      capabilities: { toolCalling: true, imageInput: api === "responses" || id.includes("claude") },
    }));
  }

  async provideLanguageModelChatResponse(
    model: Model,
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken,
  ): Promise<void> {
    const { workspace, authCommand } = this.settings();
    this.tokens ??= new Tokens(authCommand, (expiresAt) => this.log(`Fetched a Databricks token (expires ${new Date(expiresAt).toISOString()})`));
    const turn = { model: model.id, messages, tools: options.tools ?? [], toolRequired: options.toolMode === vscode.LanguageModelChatToolMode.Required };
    const abort = new AbortController();
    const cancelled = token.onCancellationRequested(() => abort.abort());
    try {
      const replies: AsyncIterable<Reply> =
        model.api === "responses"
          ? responsesReplies(streamEvents(workspace, "responses", responsesRequest(turn), this.tokens, this.headers(), abort.signal))
          : chatCompletionsReplies(streamEvents(workspace, "chat/completions", chatCompletionsRequest(turn), this.tokens, this.headers(), abort.signal));
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

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Unity Gateway", { log: true });
  const settings = () => {
    const configuration = vscode.workspace.getConfiguration("unityGateway");
    return readSettings((key) => configuration.get(key));
  };
  // The gateway attributes requests by this User-Agent.
  const headers = () => {
    const version: unknown = vscode.extensions.getExtension("GitHub.copilot-chat")?.packageJSON?.version;
    return { "User-Agent": `ucode/${packageJson.version} githubcopilotchat${typeof version === "string" ? `/${version}` : ""}` };
  };
  const provider = new DatabricksModels(settings, (line) => output.info(line), headers);
  context.subscriptions.push(
    output,
    vscode.lm.registerLanguageModelChatProvider("databricks-unity-gateway", provider),
    vscode.commands.registerCommand("unityGateway.refreshModels", () => provider.refresh()),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("unityGateway")) {
        provider.refresh();
      }
    }),
  );
}
