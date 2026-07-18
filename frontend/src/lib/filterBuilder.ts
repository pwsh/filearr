// Visual filter-builder codec (user-requested filter builder page).
//
// PURE, DOM-free logic shared by FilterBuilderPage.svelte and its round-trip on
// CustomReportsPage ("Edit in builder"). The builder compiles structured
// condition rows -> the querydsl string as the SINGLE SOURCE OF TRUTH (the
// backend never sees the rows — only the DSL, which /query/preview parses through
// the exact custom-report machinery). This module is the rows<->DSL codec plus
// the best-effort "open in search" param mapping.
//
// The grammar is normative in backend/filearr/querydsl.py + the dslHelp examples;
// nothing here invents syntax. Every string emitted by `conditionToDsl` for the
// CODEC_VECTORS fixtures below is parse-verified by a backend test
// (test_filter_codec_vectors.py) exactly like the dslHelp chip examples — so a
// codec output can never silently diverge from what the parser accepts.
//
// OR groups: the DSL has NO disjunction, so the builder AND-combines every row
// (matching DSL + query_sql semantics). An "OR groups" construct is a P7 grammar
// backlog item (documented, not implemented here).

// --------------------------------------------------------------------------- //
// Row model                                                                    //
// --------------------------------------------------------------------------- //
export type FieldKind =
  | "text"
  | "kind"
  | "ext"
  | "size"
  | "modified"
  | "created"
  | "path"
  | "tag"
  | "hash"
  | "meta"
  | "cf";

/** A comparator/range operator (the size/time/meta families) or a field-specific
 *  verb ("contains"/"is"/"is_not"/"matches"). */
export type Op =
  | "contains"
  | "is"
  | "is_not"
  | "="
  | ">"
  | ">="
  | "<"
  | "<="
  | "range"
  | "matches";

export type SizeUnit = "" | "K" | "M" | "G" | "T";
export type DurationUnit = "s" | "m" | "h" | "d" | "w";
export type DateMode = "relative" | "absolute";

export interface Condition {
  /** Stable UI key (never emitted). */
  id: number;
  field: FieldKind;
  negated: boolean;
  op: Op;
  value: string;
  value2: string; // range upper bound
  unit: SizeUnit | DurationUnit | "";
  key: string; // meta.<key> / cf.<name> subkey
  dateMode: DateMode; // modified/created only
}

let _seq = 1;
export function newCondition(field: FieldKind = "text"): Condition {
  return {
    id: _seq++,
    field,
    negated: false,
    op: defaultOp(field),
    value: "",
    value2: "",
    unit: field === "size" ? "" : field === "modified" || field === "created" ? "d" : "",
    key: "",
    dateMode: "relative",
  };
}

/** Operators valid for a field, per the grammar (the UI populates its operator
 *  selector from this). */
export function opsFor(field: FieldKind): Op[] {
  switch (field) {
    case "text":
      return ["contains"];
    case "kind":
    case "ext":
    case "tag":
      return ["is", "is_not"];
    case "hash":
      return ["is"];
    case "path":
      return ["matches"];
    case "size":
    case "meta":
    case "cf":
      return ["=", ">", ">=", "<", "<=", "range"];
    case "modified":
    case "created":
      return [">", ">=", "<", "<=", "=", "range"];
  }
}

export function defaultOp(field: FieldKind): Op {
  return opsFor(field)[0];
}

// --------------------------------------------------------------------------- //
// Emit: rows -> DSL                                                            //
// --------------------------------------------------------------------------- //

/** Mirror of querydsl `_needs_quote`: a bare token/value must be quoted when it
 *  is empty, holds whitespace or a colon, or starts with a structural char. */
export function needsQuote(v: string): boolean {
  if (v === "") return true;
  if (/\s/.test(v)) return true;
  if (v.includes(":")) return true;
  return "-!~\"".includes(v[0]);
}

function quoteIfNeeded(v: string): string {
  return needsQuote(v) ? `"${v}"` : v;
}

function sizeAtom(value: string, unit: string): string {
  return `${value}${unit}`;
}

/** Coerce any row value to a string. A `<input type="number">` binding sets
 *  `value`/`value2` to a `number` (or `null` when empty/partial), NOT a string,
 *  so every read of a row value MUST funnel through here. Calling `.trim()` on a
 *  raw number throws, and that throw would take down the whole
 *  `$derived(conditionsToDsl(...))` chain — freezing the compiled-DSL pane, the
 *  live preview, AND the Edit-raw button, all of which read the compiled string.
 *  Keeping the codec TOTAL (never throwing) is the core FIX-14 invariant. */
export function str(v: unknown): string {
  return v == null ? "" : String(v);
}

const DIGITS_RE = /^\d+$/;
const ABS_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Per-row validity for the builder UI, decided with the SAME rules the codec
 *  emits with (so the hint and the compiled output can never disagree):
 *    - `incomplete`: a required field is empty (mid-entry) — silently skipped;
 *    - `invalid`:    a present value is malformed (e.g. non-integer size) —
 *                    skipped WITH a warning shown on the row;
 *    - `ok`:         compiles.
 *  The page always compiles the valid subset; one bad row never freezes it. */
export type RowState = "ok" | "incomplete" | "invalid";
export interface RowStatus {
  state: RowState;
  /** Short hint shown on the row for the incomplete/invalid states. */
  hint: string;
}
const OK: RowStatus = { state: "ok", hint: "" };
function incomplete(hint: string): RowStatus {
  return { state: "incomplete", hint };
}
function invalid(hint: string): RowStatus {
  return { state: "invalid", hint };
}

export function validateCondition(c: Condition): RowStatus {
  const v = str(c.value).trim();
  const v2 = str(c.value2).trim();

  switch (c.field) {
    case "text":
      return v === "" ? incomplete("Enter text to match") : OK;
    case "kind":
      return v === "" ? incomplete("Choose a kind") : OK;
    case "tag":
      return v === "" ? incomplete("Enter a tag") : OK;
    case "ext": {
      const items = v
        .split(";")
        .map((s) => s.trim().replace(/^\.+/, ""))
        .filter(Boolean);
      return items.length ? OK : incomplete("Enter one or more extensions");
    }
    case "hash":
      return v === "" ? incomplete("Enter a hash") : OK;
    case "path":
      return v === "" ? incomplete("Enter a path pattern") : OK;

    case "size": {
      if (c.op === "range") {
        if (v === "" || v2 === "") return incomplete("Enter both size bounds");
        if (!DIGITS_RE.test(v) || !DIGITS_RE.test(v2))
          return invalid("Size must be a whole number");
        return OK;
      }
      if (v === "") return incomplete("Enter a size");
      return DIGITS_RE.test(v) ? OK : invalid("Size must be a whole number");
    }

    case "modified":
    case "created": {
      const okVal = (a: string) =>
        c.dateMode === "absolute" ? ABS_DATE_RE.test(a) : DIGITS_RE.test(a);
      const want =
        c.dateMode === "absolute" ? "a date (YYYY-MM-DD)" : "a whole number";
      if (c.op === "range") {
        if (v === "" || v2 === "") return incomplete("Enter both bounds");
        return okVal(v) && okVal(v2) ? OK : invalid(`Expected ${want}`);
      }
      if (v === "")
        return incomplete(
          c.dateMode === "absolute" ? "Pick a date" : "Enter a number",
        );
      return okVal(v) ? OK : invalid(`Expected ${want}`);
    }

    case "meta":
    case "cf": {
      if (str(c.key).trim() === "")
        return incomplete(
          c.field === "meta" ? "Enter a meta key" : "Enter a field name",
        );
      if (c.op === "range") {
        return v === "" || v2 === "" ? incomplete("Enter both bounds") : OK;
      }
      return v === "" ? incomplete("Enter a value") : OK;
    }
  }
}

/** Serialise a single condition to a DSL token, or `null` when it is not `ok`
 *  (incomplete or invalid) and must be skipped rather than emit broken syntax.
 *  TOTAL by construction: it never throws, whatever the in-progress value type. */
export function conditionToDsl(c: Condition): string | null {
  if (validateCondition(c).state !== "ok") return null;
  const neg = c.negated ? "-" : "";
  const v = str(c.value).trim();
  const v2 = str(c.value2).trim();
  const unit = str(c.unit);

  switch (c.field) {
    case "text":
      return neg + quoteIfNeeded(v);

    case "kind":
    case "tag": {
      const not = c.op === "is_not" ? "-" : neg;
      return `${not}${c.field}:${quoteIfNeeded(v)}`;
    }

    case "ext": {
      const items = v
        .split(";")
        .map((s) => s.trim().replace(/^\.+/, ""))
        .filter(Boolean);
      const not = c.op === "is_not" ? "-" : neg;
      return `${not}ext:${items.join(";")}`;
    }

    case "hash":
      return `${neg}hash:${v.toLowerCase()}`;

    case "path":
      return `${neg}path:${quoteIfNeeded(v)}`;

    case "size": {
      if (c.op === "range") {
        return `${neg}size:${sizeAtom(v, unit)}..${sizeAtom(v2, unit)}`;
      }
      const cmp = c.op === "=" ? "" : c.op;
      return `${neg}size:${cmp}${sizeAtom(v, unit)}`;
    }

    case "modified":
    case "created": {
      const atom = (n: string) =>
        c.dateMode === "absolute" ? n : `${n}${unit}`;
      if (c.op === "range") {
        return `${neg}${c.field}:${atom(v)}..${atom(v2)}`;
      }
      const cmp = c.op === "=" ? "" : c.op;
      return `${neg}${c.field}:${cmp}${atom(v)}`;
    }

    case "meta":
    case "cf": {
      const key = str(c.key).trim();
      const prefix = c.field === "meta" ? "meta." : "cf.";
      if (c.op === "range") {
        return `${neg}${prefix}${key}:${v}..${v2}`;
      }
      const cmp = c.op === "=" ? "" : c.op;
      return `${neg}${prefix}${key}:${cmp}${v}`;
    }
  }
}

/** Compile all (complete) conditions into a single AND-combined DSL string. */
export function conditionsToDsl(conditions: Condition[]): string {
  return conditions
    .map(conditionToDsl)
    .filter((s): s is string => s !== null)
    .join(" ");
}

// --------------------------------------------------------------------------- //
// Parse: DSL -> rows (best-effort, for "Edit in builder" round-trip)          //
// --------------------------------------------------------------------------- //
const KNOWN_KEYS = new Set([
  "kind",
  "ext",
  "size",
  "modified",
  "created",
  "path",
  "tag",
  "hash",
]);
const CMP_RE = /^(>=|<=|>|<|=)/;
const SIZE_ATOM_RE = /^(\d+)([KMGT]?)$/i;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const DURATION_RE = /^(\d+)([smhdw])$/;
const DYNAMIC_KEY_RE = /^[a-z0-9_]+(?:\.[a-z0-9_]+)*$/;

interface Decoded {
  chars: string;
  quoted: boolean[];
}

/** Split a DSL string into tokens, honouring double-quoted spans (whitespace
 *  inside quotes does not split), returning each token DECODED (quotes removed)
 *  with a parallel per-char `quoted` mask so structural chars (- ! ~ :) can be
 *  told apart from the same char inside a quoted value. Returns null on an
 *  unterminated quote (the caller keeps the raw query). */
function tokenizeDecoded(s: string): Decoded[] | null {
  const out: Decoded[] = [];
  let cur: Decoded | null = null;
  let inQuote = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (inQuote) {
      if (ch === '"') {
        inQuote = false;
      } else {
        if (!cur) cur = { chars: "", quoted: [] };
        cur.chars += ch;
        cur.quoted.push(true);
      }
      continue;
    }
    if (ch === '"') {
      if (!cur) cur = { chars: "", quoted: [] };
      inQuote = true;
      continue;
    }
    if (/\s/.test(ch)) {
      if (cur) {
        out.push(cur);
        cur = null;
      }
      continue;
    }
    if (!cur) cur = { chars: "", quoted: [] };
    cur.chars += ch;
    cur.quoted.push(false);
  }
  if (inQuote) return null;
  if (cur) out.push(cur);
  return out;
}

/** Result of parsing a DSL string back to rows. `advanced` signals a construct
 *  the builder cannot represent (fuzzy ~, unknown key, mixed-unit range, ...);
 *  the caller then keeps the RAW query and shows an "editing raw" banner — the
 *  query is never destroyed. */
export type DslToRows =
  | { rows: Condition[]; advanced: false }
  | { rows: Condition[]; advanced: true };

function parseSizeAtom(a: string): { n: string; u: SizeUnit } | null {
  const m = a.match(SIZE_ATOM_RE);
  if (!m) return null;
  return { n: m[1], u: (m[2] || "").toUpperCase() as SizeUnit };
}

function fail(): DslToRows {
  return { rows: [], advanced: true };
}

export function dslToRows(dsl: string): DslToRows {
  const toks = tokenizeDecoded(dsl);
  if (toks === null) return fail();
  const rows: Condition[] = [];

  for (const tok of toks) {
    const { chars, quoted } = tok;
    if (chars === "") continue;
    let start = 0;
    let negated = false;
    if (!quoted[0] && (chars[0] === "-" || chars[0] === "!")) {
      negated = true;
      start = 1;
    }
    if (!quoted[start] && chars[start] === "~") {
      // Fuzzy term — unsupported in the builder (and in reports/SQL).
      return fail();
    }
    // First UNQUOTED colon splits a filter key from its value.
    let colon = -1;
    for (let j = start; j < chars.length; j++) {
      if (chars[j] === ":" && !quoted[j]) {
        colon = j;
        break;
      }
    }
    const keyRaw = colon > start ? chars.slice(start, colon) : "";
    const keyUnquoted =
      colon > start ? quoted.slice(start, colon).every((q) => !q) : false;
    const lkey = keyRaw.toLowerCase();
    const isKnown = keyUnquoted && KNOWN_KEYS.has(lkey);
    const isMeta = keyUnquoted && keyRaw.startsWith("meta.");
    const isCf = keyUnquoted && keyRaw.startsWith("cf.");

    if (colon > start && (isKnown || isMeta || isCf)) {
      const value = chars.slice(colon + 1);
      const c = filterToCondition(lkey, keyRaw, value, negated, isMeta, isCf);
      if (!c) return fail();
      rows.push(c);
      continue;
    }

    // Free-text term (bare word, quoted phrase, or an unknown key:value which the
    // grammar itself treats as free text). Negation preserved.
    const c = newCondition("text");
    c.negated = negated;
    c.op = "contains";
    c.value = chars;
    rows.push(c);
  }

  return { rows, advanced: false };
}

function filterToCondition(
  lkey: string,
  keyRaw: string,
  value: string,
  negated: boolean,
  isMeta: boolean,
  isCf: boolean,
): Condition | null {
  if (value === "") return null;

  if (isMeta || isCf) {
    const sub = keyRaw.slice(isMeta ? 5 : 3);
    if (!DYNAMIC_KEY_RE.test(sub)) return null;
    const c = newCondition(isMeta ? "meta" : "cf");
    c.key = sub;
    c.negated = negated;
    return applyPred(c, value) ? c : null;
  }

  switch (lkey) {
    case "kind":
    case "tag": {
      const c = newCondition(lkey as FieldKind);
      c.op = negated ? "is_not" : "is";
      c.negated = false;
      c.value = value;
      return c;
    }
    case "ext": {
      const c = newCondition("ext");
      c.op = negated ? "is_not" : "is";
      c.negated = false;
      c.value = value;
      return c;
    }
    case "hash": {
      const c = newCondition("hash");
      c.negated = negated;
      c.value = value;
      return c;
    }
    case "path": {
      const c = newCondition("path");
      c.negated = negated;
      c.value = value;
      return c;
    }
    case "size": {
      const c = newCondition("size");
      c.negated = negated;
      return applySize(c, value) ? c : null;
    }
    case "modified":
    case "created": {
      const c = newCondition(lkey as FieldKind);
      c.negated = negated;
      return applyTime(c, value) ? c : null;
    }
  }
  return null;
}

/** Split a comparator/range predicate into {op, lo, hi}. Returns null when it is
 *  a comparator-carrying range (grammar-invalid) so the caller can bail to raw. */
function splitPred(
  value: string,
): { op: Op; lo: string; hi: string } | null {
  const m = value.match(CMP_RE);
  if (!m) {
    if (value.includes("..")) {
      const parts = value.split("..");
      if (parts.length !== 2 || !parts[0] || !parts[1]) return null;
      return { op: "range", lo: parts[0], hi: parts[1] };
    }
    return { op: "=", lo: value, hi: "" };
  }
  const rest = value.slice(m[0].length);
  if (rest.includes("..")) return null; // range may not carry a comparator
  if (rest === "") return null;
  return { op: m[0] as Op, lo: rest, hi: "" };
}

function applySize(c: Condition, value: string): boolean {
  const p = splitPred(value);
  if (!p) return false;
  if (p.op === "range") {
    const lo = parseSizeAtom(p.lo);
    const hi = parseSizeAtom(p.hi);
    if (!lo || !hi) return false;
    if (lo.u !== hi.u) return false; // one unit per range in the row UI
    c.op = "range";
    c.value = lo.n;
    c.value2 = hi.n;
    c.unit = lo.u;
    return true;
  }
  const atom = parseSizeAtom(p.lo);
  if (!atom) return false;
  c.op = p.op;
  c.value = atom.n;
  c.unit = atom.u;
  return true;
}

function applyTime(c: Condition, value: string): boolean {
  const p = splitPred(value);
  if (!p) return false;
  const classify = (a: string): DateMode | null => {
    if (DATE_RE.test(a)) return "absolute";
    if (DURATION_RE.test(a)) return "relative";
    return null;
  };
  if (p.op === "range") {
    const k1 = classify(p.lo);
    const k2 = classify(p.hi);
    if (!k1 || k1 !== k2) return false;
    c.op = "range";
    c.dateMode = k1;
    if (k1 === "relative") {
      const m1 = p.lo.match(DURATION_RE)!;
      const m2 = p.hi.match(DURATION_RE)!;
      if (m1[2] !== m2[2]) return false; // one duration unit per range
      c.value = m1[1];
      c.value2 = m2[1];
      c.unit = m1[2] as DurationUnit;
    } else {
      c.value = p.lo;
      c.value2 = p.hi;
    }
    return true;
  }
  const k = classify(p.lo);
  if (!k) return false;
  c.op = p.op;
  c.dateMode = k;
  if (k === "relative") {
    const mm = p.lo.match(DURATION_RE)!;
    c.value = mm[1];
    c.unit = mm[2] as DurationUnit;
  } else {
    c.value = p.lo;
  }
  return true;
}

function applyPred(c: Condition, value: string): boolean {
  const p = splitPred(value);
  if (!p) return false;
  c.op = p.op;
  c.value = p.lo;
  c.value2 = p.hi;
  return true;
}

// --------------------------------------------------------------------------- //
// Best-effort "Open in search" mapping                                         //
// --------------------------------------------------------------------------- //
// The search page consumes FLAT structured params (kind/ext/size/dates/tag/hash),
// NOT the DSL (a known gap). We map the 1:1-expressible conditions and REPORT the
// rest so the UI can warn the user which conditions are dropped. Mapping table:
//   text (contains, not-negated) -> q (space-joined)
//   kind:is                      -> type
//   ext:is (single)              -> extension
//   size                         -> size_gte / size_lte (bytes)
//   modified                     -> mtime_gte / mtime_lte (epoch seconds)
//   tag:is                       -> tags (comma, AND)
//   hash:is                      -> hash
// Unmappable: any negated/is_not filter, created:, path:, meta./cf.,
// multi-value ext, and free-text ranges the flat params cannot express.
const UNIT_BYTES: Record<string, number> = {
  "": 1,
  K: 1024,
  M: 1024 ** 2,
  G: 1024 ** 3,
  T: 1024 ** 4,
};
const DURATION_SECONDS: Record<string, number> = {
  s: 1,
  m: 60,
  h: 3600,
  d: 86400,
  w: 604800,
};

export interface SearchMapping {
  params: Record<string, string>;
  unmapped: Condition[];
}

function sizeBytes(value: string, unit: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  return parseInt(value, 10) * (UNIT_BYTES[unit] ?? 1);
}

export function conditionsToSearchParams(conditions: Condition[]): SearchMapping {
  const params: Record<string, string> = {};
  const unmapped: Condition[] = [];
  const terms: string[] = [];
  const tags: string[] = [];

  for (const c of conditions) {
    const v = str(c.value).trim();
    switch (c.field) {
      case "text":
        if (!c.negated && v) terms.push(v);
        else unmapped.push(c);
        break;
      case "kind":
        if (c.op === "is" && !c.negated && v) params.type = v;
        else unmapped.push(c);
        break;
      case "ext": {
        const items = v.split(";").map((s) => s.trim()).filter(Boolean);
        if (c.op === "is" && !c.negated && items.length === 1) params.extension = items[0];
        else unmapped.push(c);
        break;
      }
      case "tag":
        if (c.op === "is" && !c.negated && v) tags.push(v);
        else unmapped.push(c);
        break;
      case "hash":
        if (!c.negated && v) params.hash = v.toLowerCase();
        else unmapped.push(c);
        break;
      case "size": {
        if (c.negated) {
          unmapped.push(c);
          break;
        }
        if (c.op === "range") {
          const lo = sizeBytes(v, c.unit);
          const hi = sizeBytes(str(c.value2).trim(), c.unit);
          if (lo == null || hi == null) { unmapped.push(c); break; }
          params.size_gte = String(lo);
          params.size_lte = String(hi);
        } else {
          const n = sizeBytes(v, c.unit);
          if (n == null) { unmapped.push(c); break; }
          if (c.op === ">=") params.size_gte = String(n);
          else if (c.op === ">") params.size_gte = String(n + 1);
          else if (c.op === "<=") params.size_lte = String(n);
          else if (c.op === "<") params.size_lte = String(Math.max(0, n - 1));
          else if (c.op === "=") { params.size_gte = String(n); params.size_lte = String(n); }
          else { unmapped.push(c); }
        }
        break;
      }
      case "modified": {
        // Only relative durations + comparators map cleanly to mtime epoch bounds.
        if (c.negated || c.dateMode !== "relative" || c.op === "range") {
          unmapped.push(c);
          break;
        }
        if (!/^\d+$/.test(v)) { unmapped.push(c); break; }
        const secs = parseInt(v, 10) * (DURATION_SECONDS[c.unit] ?? 0);
        const now = Math.floor(Date.now() / 1000);
        // modified:<7d = within the last 7d = mtime_gte now-secs; >7d = older.
        if (c.op === "<" || c.op === "<=" || c.op === "=") params.mtime_gte = String(now - secs);
        else if (c.op === ">" || c.op === ">=") params.mtime_lte = String(now - secs);
        else unmapped.push(c);
        break;
      }
      default:
        // created:, path:, meta., cf. have no flat search param.
        unmapped.push(c);
    }
  }

  if (terms.length) params.q = terms.join(" ");
  if (tags.length) params.tags = tags.join(",");
  return { params, unmapped };
}

// --------------------------------------------------------------------------- //
// Codec parse-verification fixtures                                            //
// --------------------------------------------------------------------------- //
// Every string here is a real `conditionToDsl` output for a representative row.
// A backend test (test_filter_codec_vectors.py) parses each through the normative
// reference parser (must not raise) and asserts a stable re-parse — mirroring the
// dslHelp chip guard. Keep in sync with the emit logic above.
export const CODEC_VECTORS: { dsl: string }[] = [
  { dsl: "invoice" },
  { dsl: '"annual report"' },
  { dsl: "-draft" },
  { dsl: "kind:video" },
  { dsl: "-kind:sample" },
  { dsl: "ext:pdf" },
  { dsl: "ext:mp4;mkv;avi" },
  { dsl: "-ext:tmp" },
  { dsl: "size:>1G" },
  { dsl: "size:<500K" },
  { dsl: "size:=0" },
  { dsl: "size:100M..4G" },
  { dsl: "modified:>7d" },
  { dsl: "modified:<=30d" },
  { dsl: "modified:>=2025-01-01" },
  { dsl: "created:2024-01-01..2024-12-31" },
  { dsl: "path:*/backups/*" },
  { dsl: 'path:"*/Season 01/*"' },
  { dsl: "tag:archived" },
  { dsl: "-tag:draft" },
  { dsl: "hash:e3b0c442" },
  { dsl: "meta.height:>=1080" },
  { dsl: "meta.width:1920..3840" },
  { dsl: "cf.rating:>=4" },
  { dsl: "cf.shelf_location:A12" },
  { dsl: "kind:video meta.height:>=1080 -tag:archived" },
];
