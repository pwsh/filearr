// P4-T12 — pure ordering + labelling for the key-facts view.
//
// Fields are shown in this precedence, each contributing only keys that carry a
// non-empty value and are not already shown:
//   1. profile fields (P4-T1) — declared order + declared labels;
//   2. applicable custom fields (P4-T3) — narrowed by media_type + library,
//      declared order + declared labels;
//   3. leftover ad-hoc keys — alphabetical, labelled by their raw key name.
// Values are rendered TEXT-ONLY (no images/embeds here — the visual bits like a
// waveform or mesh preview stay hand-written in the per-type cards). This module
// is pure/DOM-free so it is unit-testable and shared by every card.
//
// KNOWN LIMITATION (P4-T10/T12): the profile's `fields` arrive as a JSONB object
// whose key iteration order is not guaranteed to equal the code-declared
// FieldSpec order. We order by that map's iteration order; a mismatch only
// affects row ORDER within the profile section, never labels or correctness.

export interface FieldLabel {
  /** the metadata key (a user_metadata / metadata_ key). */
  name: string;
  /** the human label to display; falls back to `name` when absent. */
  label: string;
}

export interface KeyFact {
  key: string;
  label: string;
  value: string;
}

/** Render one metadata value to a short display string, or null to OMIT it
 *  (null/undefined, empty string, empty array). Arrays join with ", ";
 *  objects (and array-of-objects elements) fall back to compact JSON so a
 *  structured value is at least visible, never crashes the row. */
export function factValue(v: unknown): string | null {
  if (v == null) return null;
  if (typeof v === "string") return v.trim() ? v : null;
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : null;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) {
    const parts = v
      .map((x) => (x != null && typeof x === "object" ? JSON.stringify(x) : String(x)))
      .filter((s) => s !== "");
    return parts.length ? parts.join(", ") : null;
  }
  if (typeof v === "object") {
    try {
      const s = JSON.stringify(v);
      return s && s !== "{}" ? s : null;
    } catch {
      return null;
    }
  }
  return String(v);
}

/** Order + label the effective metadata into displayable key-facts.
 *  `exclude` drops keys already presented by a per-type card's hand-written
 *  section, so the generic list only shows what the card did not curate. */
export function orderKeyFacts(
  meta: Record<string, unknown>,
  profileOrder: FieldLabel[],
  customFields: FieldLabel[],
  opts: { exclude?: string[] } = {},
): KeyFact[] {
  const exclude = new Set(opts.exclude ?? []);
  const used = new Set<string>();
  const facts: KeyFact[] = [];

  const push = (name: string, label: string) => {
    if (used.has(name) || exclude.has(name)) return;
    const value = factValue(meta[name]);
    if (value == null) return;
    facts.push({ key: name, label: label || name, value });
    used.add(name);
  };

  for (const f of profileOrder) push(f.name, f.label);
  for (const f of customFields) push(f.name, f.label);

  const leftover = Object.keys(meta)
    .filter((k) => !used.has(k) && !exclude.has(k) && !k.startsWith("_"))
    .sort();
  for (const k of leftover) push(k, k);

  return facts;
}

/** Project a profile's `fields` map into ordered {name,label} labels. */
export function profileFieldLabels(
  fields: Record<string, { label?: string }> | undefined,
): FieldLabel[] {
  if (!fields) return [];
  return Object.entries(fields).map(([name, spec]) => ({
    name,
    label: spec?.label || name,
  }));
}

/** Project applicable custom-field defs into ordered {name,label} labels. */
export function customFieldLabels(
  defs: { name: string; label?: string }[] | undefined,
): FieldLabel[] {
  if (!defs) return [];
  return defs.map((d) => ({ name: d.name, label: d.label || d.name }));
}

/** The effective metadata for an item = extracted `metadata` with the
 *  `user_metadata` edit overlay on top (invariant 2). Both may be absent. */
export function effectiveMeta(item: Record<string, unknown>): Record<string, unknown> {
  const m = (item.metadata as Record<string, unknown>) ?? {};
  const u = (item.user_metadata as Record<string, unknown>) ?? {};
  return { ...m, ...u };
}
