import { exec } from "node:child_process";

// Fetch a new token this long before the current one expires.
const EXPIRY_MARGIN_MS = 5 * 60_000;
// Tokens without a readable `exp` (a PAT, say) are fetched again after an hour.
const DEFAULT_LIFETIME_MS = 60 * 60_000;

const run = (command: string): Promise<string> =>
  new Promise((resolve, reject) => {
    exec(command, { timeout: 60_000, windowsHide: true }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(`Couldn't get a Databricks token: \`${command}\` failed: ${String(stderr).trim().slice(0, 500) || error.message}`));
      } else {
        resolve(String(stdout).trim());
      }
    });
  });

export const expiresAt = (token: string): number => {
  try {
    const { exp } = JSON.parse(Buffer.from(token.split(".")[1] ?? "", "base64url").toString());
    if (typeof exp === "number") {
      return exp * 1000;
    }
  } catch {
    // Not a JWT.
  }
  return Date.now() + DEFAULT_LIFETIME_MS;
};

/** Runs the auth command for a token and keeps it until it nears expiry or is rejected. */
export class Tokens {
  private cached: { value: string; expiresAt: number } | undefined;
  private pending: Promise<string> | undefined;

  constructor(
    private readonly command: string,
    private readonly onFetch: (expiresAt: number) => void = () => {},
  ) {}

  get(): Promise<string> {
    if (this.cached && this.cached.expiresAt - EXPIRY_MARGIN_MS > Date.now()) {
      return Promise.resolve(this.cached.value);
    }
    return this.fetch(this.command);
  }

  /** A token the gateway rejected. The CLI can hand back its cached copy while `exp` is still ahead, so force a new one. */
  async renew(rejected: string): Promise<string> {
    await this.pending?.catch(() => {});
    if (this.cached && this.cached.value !== rejected) {
      return this.cached.value;
    }
    this.cached = undefined;
    return this.fetch(`${this.command} --force-refresh`);
  }

  private fetch(command: string): Promise<string> {
    this.pending ??= run(command)
      .then((value) => {
        if (!value) {
          throw new Error(`Couldn't get a Databricks token: \`${command}\` printed nothing.`);
        }
        this.cached = { value, expiresAt: expiresAt(value) };
        this.onFetch(this.cached.expiresAt);
        return value;
      })
      .finally(() => {
        this.pending = undefined;
      });
    return this.pending;
  }
}
