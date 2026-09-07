import camelCase from "camelcase";

export function normalizeLabel(value: string): string {
  return camelCase(value.trim());
}

export default function labelSummary(values: readonly string[]): string {
  return values.map(normalizeLabel).filter(Boolean).join(", ");
}
