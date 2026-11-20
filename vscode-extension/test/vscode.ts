// Unit tests run outside VS Code; this provides the parts of the `vscode` API the extension touches.
export class LanguageModelTextPart {
  constructor(public value: string) {}
}
export class LanguageModelToolCallPart {
  constructor(
    public callId: string,
    public name: string,
    public input: object,
  ) {}
}
export class LanguageModelToolResultPart {
  constructor(
    public callId: string,
    public content: unknown[],
  ) {}
}
export class LanguageModelDataPart {
  constructor(
    public data: Uint8Array,
    public mimeType: string,
  ) {}
}
export class LanguageModelPromptTsxPart {
  constructor(public value: unknown) {}
}
export const LanguageModelChatMessageRole = { User: 1, Assistant: 2 } as const;
export const LanguageModelChatToolMode = { Auto: 1, Required: 2 } as const;
export class EventEmitter<T> {
  private listeners: ((value: T) => void)[] = [];
  event = (listener: (value: T) => void) => {
    this.listeners.push(listener);
    return { dispose: () => {} };
  };
  fire(value: T): void {
    for (const listener of this.listeners) {
      listener(value);
    }
  }
}

/** What the stubs below report; tests set it in place. */
export const host = {
  copilotChat: undefined as { activate: () => Promise<void> } | undefined,
  extensionListeners: [] as (() => void)[],
  settings: {} as Record<string, unknown>,
  commands: [] as string[],
  executed: [] as string[],
  notices: [] as { message: string; actions: string[] }[],
  choice: undefined as string | undefined,
  remoteName: undefined as string | undefined,
  showNotice: (): Promise<string | undefined> => Promise.resolve(host.choice),
  chatModels: [] as { vendor: string; id: string; family: string }[],
  /** Ids of the copies VS Code's agent host lists; only the chat's own model selector finds them. */
  agentHostModels: [] as string[],
  modelListeners: [] as (() => void)[],
  commandArgs: [] as unknown[],
  openAttempts: 0,
  reset(): void {
    this.copilotChat = undefined;
    this.extensionListeners = [];
    this.settings = {};
    this.commands = [];
    this.executed = [];
    this.notices = [];
    this.choice = undefined;
    this.remoteName = undefined;
    this.chatModels = [];
    this.agentHostModels = [];
    this.modelListeners = [];
    this.commandArgs = [];
    this.openAttempts = 0;
  },
};
export const lm = {
  selectChatModels: async (selector: { vendor?: string; id?: string }) =>
    host.chatModels.filter((model) => model.vendor === selector.vendor && (!selector.id || model.id === selector.id)),
  onDidChangeChatModels: (listener: () => void) => {
    host.modelListeners.push(listener);
    return { dispose: () => (host.modelListeners = host.modelListeners.filter((other) => other !== listener)) };
  },
};
export const env = {
  get remoteName() {
    return host.remoteName;
  },
};
export const extensions = {
  getExtension: (id: string) => (id === "GitHub.copilot-chat" ? host.copilotChat : undefined),
  onDidChange: (listener: () => void) => {
    host.extensionListeners.push(listener);
    return { dispose: () => (host.extensionListeners = host.extensionListeners.filter((other) => other !== listener)) };
  },
};
export const workspace = {
  getConfiguration: (section: string) => ({ get: (key: string) => host.settings[`${section}.${key}`] }),
};
export const commands = {
  executeCommand: async (id: string, ...args: unknown[]) => {
    if (!host.commands.includes(id)) {
      throw new Error(`command '${id}' not found`);
    }
    const selector = (args[0] as { modelSelector?: { vendor?: string; id?: string } } | undefined)?.modelSelector;
    if (selector && !selector.vendor) {
      host.openAttempts += 1;
      await Promise.resolve();
    }
    if (selector && !selector.vendor && !host.agentHostModels.includes(selector.id ?? "")) {
      throw new Error(`No language models found matching selector: ${JSON.stringify(selector)}.`);
    }
    host.executed.push(id);
    host.commandArgs.push(...args);
  },
};
export const window = {
  showInformationMessage: (message: string, ...actions: string[]) => {
    host.notices.push({ message, actions });
    return host.showNotice();
  },
};
