import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

// Unit tests run outside VS Code, so `vscode` resolves to a small stand-in for the API types we use.
export default defineConfig({
  resolve: { alias: { vscode: resolve(__dirname, "test/vscode.ts") } },
  test: { include: ["test/**/*.test.ts"] },
});
