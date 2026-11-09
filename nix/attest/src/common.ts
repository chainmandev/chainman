import { spawn } from "node:child_process";
import { createHash } from "node:crypto";

export function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error("Expected a JSON object");
  return value as Record<string, unknown>;
}
export function string(value: unknown): string {
  if (typeof value !== "string" || !value)
    throw new Error("Expected a nonempty string");
  return value;
}
export function records(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) throw new Error("Expected an array");
  return value.map((entry: unknown) => object(entry));
}
export function keys(
  value: Record<string, unknown>,
  allowed: readonly string[],
): void {
  for (const key of Object.keys(value))
    if (!allowed.includes(key)) throw new Error(`Unexpected field: ${key}`);
}
export function sha512(bytes: Uint8Array): string {
  return createHash("sha512").update(bytes).digest("hex");
}
export function parse(bytes: Uint8Array): unknown {
  return JSON.parse(Buffer.from(bytes).toString("utf8")) as unknown;
}
export interface CommandResult {
  readonly status: number;
  readonly stdout: string;
  readonly stderr: string;
}
export type Command = (
  program: string,
  args: readonly string[],
  interactive?: boolean,
  env?: NodeJS.ProcessEnv,
) => Promise<CommandResult>;
export const command: Command = async (
  program,
  args,
  interactive = false,
  env = process.env,
) =>
  await new Promise((done, reject) => {
    const child = spawn(program, [...args], {
      stdio: interactive ? "inherit" : ["ignore", "pipe", "pipe"],
      env,
    });
    let stdout = "",
      stderr = "";
    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString();
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });
    child.on("error", reject);
    child.on("close", (status) => {
      done({ status: status ?? 1, stdout, stderr });
    });
  });
