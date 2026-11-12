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
  reset(): void {
    this.copilotChat = undefined;
    this.extensionListeners = [];
    this.settings = {};
    this.commands = [];
    this.executed = [];
    this.notices = [];
    this.choice = undefined;
    this.remoteName = undefined;
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
  executeCommand: async (id: string) => {
    if (!host.commands.includes(id)) {
      throw new Error(`command '${id}' not found`);
    }
    host.executed.push(id);
  },
};
export const window = {
  showInformationMessage: (message: string, ...actions: string[]) => {
    host.notices.push({ message, actions });
    return host.showNotice();
  },
};
