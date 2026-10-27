import type { Tokens } from "./auth";

/** POSTs a streaming request to the workspace's AI Gateway and yields each server-sent event's JSON. */
export async function* streamEvents(
  workspace: string,
  path: "chat/completions" | "responses",
  body: object,
  tokens: Tokens,
  headers: Record<string, string>,
  signal: AbortSignal,
): AsyncGenerator<unknown> {
  const post = (token: string) =>
    fetch(`${workspace}/ai-gateway/mlflow/v1/${path}`, {
      method: "POST",
      headers: { ...headers, Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  let token = await tokens.get();
  let response = await post(token);
  if (response.status === 401) {
    await response.body?.cancel();
    token = await tokens.renew(token);
    response = await post(token);
  }
  if (response.status === 401) {
    await response.body?.cancel();
    throw new Error(`Databricks rejected the sign-in. Run \`databricks auth login --host ${workspace}\`, then retry.`);
  }
  if (!response.ok || !response.body) {
    throw new Error(`AI Gateway returned ${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      const data = dataOf(line);
      if (data === "[DONE]") {
        return;
      }
      if (data) {
        yield JSON.parse(data);
      }
    }
  }
  const last = dataOf(buffer + decoder.decode());
  if (last && last !== "[DONE]") {
    yield JSON.parse(last);
  }
}

const dataOf = (line: string): string => (line.startsWith("data:") ? line.slice(5).trim() : "");
