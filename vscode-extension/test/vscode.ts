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
