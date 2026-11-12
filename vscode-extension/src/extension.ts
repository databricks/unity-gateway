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
import { listen, type OtelSetting, otelEndpoint, otelSettings, startReceiver } from "./otel";
import { DEFAULT_MODEL_SETTING, syncSettings } from "./settings";

// ug writes the wire name of the model it configured (WIRE_* in src/ucode/agents/copilot.py).
const APIS = ["anthropic", "completions", "responses"] as const;

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
  /** Headers for every gateway request: the workspace admin's, and the selected workspace on a host that serves several. */
  readonly headers?: Readonly<Record<string, string>>;
  /** Where Copilot's agents export traces when the workspace traces Copilot. */
  readonly otel?: OtelSetting;
  /** The workspace's default model, one of `models`, for new chat conversations. */
  readonly defaultModel?: string;
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

const managedHeaders = (value: unknown): Record<string, string> =>
  Object.fromEntries(
    Object.entries(value && typeof value === "object" && !Array.isArray(value) ? value : {}).filter(
      // The extension signs every request itself.
      (entry): entry is [string, string] => typeof entry[1] === "string" && entry[0].trim() !== "" && entry[0].toLowerCase() !== "authorization",
    ),
  );

const otelSetting = (value: unknown): OtelSetting | undefined => {
  const { port, secret } = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
  return typeof port === "number" && Number.isInteger(port) && port > 0 && port < 65536 && typeof secret === "string" && /^[\w-]+$/.test(secret)
    ? { port, secret }
    : undefined;
};

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
  const headers = managedHeaders(get("headers"));
  const otel = otelSetting(get("otel"));
  const defaultModel = get("defaultModel");
  const kept: ModelSetting[] = (Array.isArray(models) ? models : [])
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
    }));
  return {
    workspace: String(get("workspace") ?? "").replace(/\/+$/, ""),
    models: kept,
    authCommand: String(get("authCommand") ?? ""),
    provider: typeof get("provider") === "string" ? String(get("provider")) : "",
    ...(Object.keys(headers).length > 0 ? { headers } : {}),
    ...(otel ? { otel } : {}),
    ...(typeof defaultModel === "string" && kept.some((model) => model.id === defaultModel) ? { defaultModel } : {}),
  };
};


/** The gateway attributes requests by this User-Agent. */
export const userAgent = (copilotChatVersion: unknown): string =>
  `ucode/${version} githubcopilotchat${typeof copilotChatVersion === "string" ? `/${copilotChatVersion}` : ""}`;

/** The User-Agent and the admin's headers, which win by name, as in `ug copilot`. */
export const requestHeaders = (copilotChatVersion: unknown, managed: Readonly<Record<string, string>> = {}): Record<string, string> => {
  const names = new Set(Object.keys(managed).map((name) => name.toLowerCase()));
  return {
    ...(names.has("user-agent") ? {} : { "User-Agent": userAgent(copilotChatVersion) }),
    ...managed,
  };
};

type Model = vscode.LanguageModelChatInformation & { readonly api: ModelSetting["api"] };

export class DatabricksModels implements vscode.LanguageModelChatProvider<Model> {
  private tokens: Tokens | undefined;
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeLanguageModelChatInformation = this.changed.event;

  constructor(
    private readonly settings: () => Settings,
    private readonly log: (line: string) => void,
    private readonly headers: () => Record<string, string> = () => ({}),
  ) {}

  /** Asks VS Code to list the models again. */
  refresh(): void {
    this.changed.fire();
  }

  async provideLanguageModelChatInformation(): Promise<Model[]> {
    const { workspace, models, authCommand } = this.settings();
    if (!workspace || !authCommand || models.length === 0) {
      this.log("No Databricks models: run `ug configure --agents copilot` to set them up.");
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
      capabilities: { toolCalling: true, imageInput: api !== "completions" || id.includes("claude") },
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
    const apis: Record<ModelSetting["api"], () => AsyncIterable<Reply>> = {
      anthropic: () =>
        anthropicMessagesReplies(
          stream("/ai-gateway/anthropic/v1/messages", anthropicMessagesRequest(turn, model.maxOutputTokens), {
            "anthropic-version": "2023-06-01",
          }),
        ),
      responses: () => responsesReplies(stream(`${openai}/responses`, responsesRequest(turn))),
      completions: () => chatCompletionsReplies(stream(`${openai}/chat/completions`, chatCompletionsRequest(turn))),
    };
    try {
      for await (const reply of apis[model.api]()) {
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

const COPILOT_CHAT = "GitHub.copilot-chat";
const TURN_ON_COMMAND = "workbench.action.chat.manage";
const TURN_ON = "Turn On Copilot Chat";
export const NOTICE_SHOWN_KEY = "copilotChatOffNoticeShown";

/** Runs `ready` now if Copilot Chat is on, or else once it's turned on, asking the user once per profile to turn it
 * on. VS Code turns Copilot Chat off in a profile that never finished its chat setup, and a prompt sent to a
 * Databricks model then asks the user to sign in to GitHub. */
export function whenCopilotChatIsOn(context: vscode.ExtensionContext, ready: (late: boolean) => void, log: (line: string) => void): void {
  if (vscode.extensions.getExtension(COPILOT_CHAT)) {
    ready(false);
    return;
  }
  log("Copilot Chat is turned off in this VS Code profile, so the Databricks models aren't listed yet.");
  const listener = vscode.extensions.onDidChange(() => {
    const chat = vscode.extensions.getExtension(COPILOT_CHAT);
    if (!chat) {
      return;
    }
    listener.dispose();
    Promise.resolve(chat.activate()).then(
      () => ready(true),
      (error: unknown) => log(`Copilot Chat didn't start: ${String(error)}`),
    );
  });
  context.subscriptions.push(listener);
  if (vscode.workspace.getConfiguration("chat").get("disableAIFeatures") === true || context.globalState.get(NOTICE_SHOWN_KEY)) {
    return;
  }
  void context.globalState.update(NOTICE_SHOWN_KEY, true);
  void offerToTurnOn().catch((error: unknown) => log(`Couldn't turn on Copilot Chat: ${String(error)}`));
}

const offerToTurnOn = async (): Promise<void> => {
  if ((await vscode.window.showInformationMessage("Databricks models need Copilot Chat, which is turned off.", TURN_ON)) !== TURN_ON) {
    return;
  }
  try {
    await vscode.commands.executeCommand(TURN_ON_COMMAND);
  } catch {
    // The command is VS Code's own, not API, so it may be gone in a later version.
    await vscode.window.showInformationMessage("To turn on Copilot Chat, click + in the chat's model picker.");
  }
};

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Unity Gateway", { log: true });
  // Read once: ug changes the file only by reinstalling the extension, which takes a restart to load.
  const settings = readConfig(readConfigFile(join(context.extensionPath, CONFIG_FILE)));
  const headers = () => requestHeaders(vscode.extensions.getExtension(COPILOT_CHAT)?.packageJSON?.version, settings.headers);
  const provider = new DatabricksModels(() => settings, (line) => output.info(line), headers);
  context.subscriptions.push(output, vscode.lm.registerLanguageModelChatProvider("databricks-unity-gateway", provider));
  const warn = (line: string) => output.warn(line);
  if (settings.otel && settings.workspace && settings.authCommand) {
    const receiver = startReceiver(settings.otel, settings.workspace, new Tokens(settings.authCommand), headers, warn);
    listen(receiver, settings.otel.port, warn);
    context.subscriptions.push({ dispose: () => receiver.close() });
  }
  // This also removes what an earlier configure had the extension write and this one doesn't.
  const wanted = {
    ...(settings.otel ? otelSettings(otelEndpoint(settings.otel)) : {}),
    ...(settings.defaultModel ? { [DEFAULT_MODEL_SETTING]: settings.defaultModel } : {}),
  };
  const sync = () =>
    syncSettings(vscode.workspace.getConfiguration(), context.globalState, wanted, warn, new Set([DEFAULT_MODEL_SETTING])).catch(
      (error: unknown) => warn(`Could not update VS Code's settings: ${String(error)}`),
    );
  void sync();
  // VS Code lists a provider's models only when a chat view asks or the provider reports a change, so a window that
  // was open when ug installed the extension never lists them otherwise.
  whenCopilotChatIsOn(
    context,
    (late) => {
      provider.refresh();
      if (late) {
        // Copilot Chat's own settings exist only once it's on.
        void sync();
      }
    },
    warn,
  );
}
