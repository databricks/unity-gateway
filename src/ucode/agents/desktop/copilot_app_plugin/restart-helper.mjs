import { runHelper } from "./app-restart.mjs";

await runHelper(process.argv.at(-1));
