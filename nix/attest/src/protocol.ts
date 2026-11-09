import { isAbsolute } from "node:path";

export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export interface Subject {
  readonly name: string;
  readonly digest: { readonly sha512: string };
}
export type Request =
  | {
      readonly protocol: 1;
      readonly operation: "attest-provenance";
      readonly subject: Subject;
    }
  | {
      readonly protocol: 1;
      readonly operation: "npm-inspect";
      readonly tarball: string;
    };
export type Response =
  | {
      readonly protocol: 1;
      readonly operation: "attest-provenance";
      readonly bundle: JsonObject;
    }
  | {
      readonly protocol: 1;
      readonly operation: "npm-inspect";
      readonly manifest: JsonObject;
      readonly subject: string;
      readonly repository: string | null;
    };

export function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error("Expected a JSON object");
  return value as Record<string, unknown>;
}
export function string(value: unknown): string {
  if (typeof value !== "string" || !value || value.includes("\0"))
    throw new Error("Expected a nonempty string");
  return value;
}
function keys(
  value: Record<string, unknown>,
  expected: readonly string[],
): void {
  const actual = Object.keys(value);
  if (
    actual.length !== expected.length ||
    actual.some((key) => !expected.includes(key))
  )
    throw new Error("Unexpected or missing adapter request fields");
}
export function absolute(value: unknown): string {
  const path = string(value);
  if (!isAbsolute(path)) throw new Error("An absolute path is required");
  return path;
}
export function request(value: unknown): Request {
  const input = object(value);
  if (input["protocol"] !== 1) throw new Error("Unsupported adapter protocol");
  switch (input["operation"]) {
    case "attest-provenance": {
      keys(input, ["protocol", "operation", "subject"]);
      const subject = object(input["subject"]);
      keys(subject, ["name", "digest"]);
      const digest = object(subject["digest"]);
      keys(digest, ["sha512"]);
      const sha512 = string(digest["sha512"]);
      if (!/^[a-f0-9]{128}$/.test(sha512))
        throw new Error("Expected a SHA-512 hex digest");
      return {
        protocol: 1,
        operation: "attest-provenance",
        subject: { name: string(subject["name"]), digest: { sha512 } },
      };
    }
    case "npm-inspect":
      keys(input, ["protocol", "operation", "tarball"]);
      return {
        protocol: 1,
        operation: "npm-inspect",
        tarball: absolute(input["tarball"]),
      };
    default:
      throw new Error("Unsupported adapter operation");
  }
}
function json(value: unknown): Json {
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map((item: unknown) => json(item));
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(object(value)))
    Object.defineProperty(result, key, {
      value: json(item),
      enumerable: true,
      configurable: true,
      writable: true,
    });
  return result;
}
// Native libraries may attach non-JSON properties. Match their JSON serialization.
export function normalizedObject(value: unknown): JsonObject {
  const serialized = JSON.stringify(value);
  if (serialized === undefined)
    throw new Error("Expected serializable native metadata");
  const result = json(JSON.parse(serialized) as unknown);
  if (typeof result !== "object" || result === null || Array.isArray(result))
    throw new Error("Expected serialized native metadata object");
  return result;
}
