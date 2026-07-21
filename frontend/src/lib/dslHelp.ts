// FIX-12 (Item B) — shared "Query syntax" help for the filter DSL, consumed by
// BOTH the Search page and the Custom-reports builder so the two surfaces
// document the SAME grammar from one source (no drift, no duplication).
//
// The grammar itself is the NORMATIVE reference in backend/filearr/querydsl.py +
// shared/querydsl-vectors.json — nothing here invents syntax. Every `q:` example
// string below is verified by a backend test (test_dsl_help_examples.py): the
// test runs each example through the real parse() (must not raise) AND asserts
// each string appears verbatim in this file, so a documented example can never
// silently diverge from what the parser actually accepts.
//
// searchOnly sections (fuzzy ~terms) are grammar-valid but only meaningful on the
// full-text search page; the reports translator rejects fuzzy, so the shared
// component labels them "not supported in reports" there.

export interface DslExample {
  /** Literal query fragment; clicking its chip inserts this into the query box. */
  q: string;
  /** Short gloss of what it matches (chip tooltip). */
  note?: string;
}

export interface DslSection {
  title: string;
  /** One-line prose description of the construct. */
  body: string;
  examples: DslExample[];
  /** Grammar-valid but search-only (fuzzy); flagged unsupported in reports. */
  searchOnly?: boolean;
}

export const DSL_SECTIONS: DslSection[] = [
  {
    title: "Free text",
    body:
      "Bare words match the filename, title and path. Several words all have to " +
      "match (AND). Wrap a phrase in double quotes to keep its spaces together.",
    examples: [
      { q: "invoice", note: "files mentioning invoice" },
      { q: '"annual report"', note: "the exact phrase" },
      { q: '"quarterly report"', note: "another quoted phrase" },
    ],
  },
  {
    title: "Fuzzy terms",
    searchOnly: true,
    body:
      "Prefix a term with ~ for typo-tolerant matching. Fuzzy applies to free-text " +
      "terms only (never to filters). Available on search; reports need exact filters.",
    examples: [{ q: "~documentaru", note: "matches ‘documentary’ despite the typo" }],
  },
  {
    title: "Negation",
    body: "Prefix any term or filter with - or ! to exclude it.",
    examples: [
      { q: "-draft", note: "exclude the word draft" },
      { q: "-kind:sample", note: "exclude sample-type items" },
      { q: "!ext:tmp", note: "exclude .tmp files" },
    ],
  },
  {
    title: "kind:",
    body: "Filter by media type (video, audio, image, document, …).",
    examples: [
      { q: "kind:video" },
      { q: "kind:audio" },
    ],
  },
  {
    title: "group:",
    body:
      "Filter by the finer file group (raw-photo, audio-lossless, pdf, archive, …) " +
      "— the granular child of kind.",
    examples: [
      { q: "group:raw-photo" },
      { q: "group:audio-lossless" },
    ],
  },
  {
    title: "ext:",
    body:
      "Filter by file extension. List several with ; — the leading dot is optional " +
      "and matching is case-insensitive.",
    examples: [
      { q: "ext:pdf" },
      { q: "ext:mp4;mkv;avi", note: "any of these extensions" },
    ],
  },
  {
    title: "size:",
    body:
      "Filter by file size with a comparator (>, >=, <, <=, =) or an A..B range. " +
      "Units K/M/G/T are binary (1024-based); no unit means bytes.",
    examples: [
      { q: "size:>1G", note: "larger than 1 GiB" },
      { q: "size:<500K", note: "smaller than 500 KiB" },
      { q: "size:100M..4G", note: "inclusive range" },
    ],
  },
  {
    title: "modified: / created:",
    body:
      "Filter by modified or created time. Use a relative duration (s, m, h, d, w) " +
      "or an ISO date (YYYY-MM-DD), with a comparator or an A..B range.",
    examples: [
      { q: "modified:>7d", note: "changed in the last 7 days" },
      { q: "modified:<30d", note: "not touched for 30 days" },
      { q: "modified:>=2025-01-01", note: "on/after a date" },
      { q: "created:2024-01-01..2024-12-31", note: "created within 2024" },
    ],
  },
  {
    title: "path:",
    body: "Match a path glob (kept verbatim). Quote it if it contains spaces.",
    examples: [
      { q: "path:*/backups/*" },
      { q: 'path:"*/Season 01/*"', note: "glob with a space" },
    ],
  },
  {
    title: "tag:",
    body: "Filter by tag name (combine with - to exclude a tag).",
    examples: [
      { q: "tag:archived" },
      { q: "-tag:draft", note: "exclude a tag" },
    ],
  },
  {
    title: "hash:",
    body: "Match a content-hash digest or a hex prefix.",
    examples: [{ q: "hash:e3b0c442", note: "digest prefix" }],
  },
  {
    title: "meta.<key>: and cf.<name>:",
    body:
      "Filter on an extracted metadata value (meta.<key>) or a registered custom " +
      "field (cf.<name>). Both accept a comparator or an A..B range. Keys are " +
      "lowercase [a-z0-9_], dot-separated for nested metadata.",
    examples: [
      { q: "meta.height:>=1080", note: "1080p and up" },
      { q: "meta.duration:>3600", note: "longer than an hour" },
      { q: "meta.width:1920..3840", note: "width range" },
      { q: "cf.rating:>=4", note: "custom field ≥ 4" },
      { q: "cf.shelf_location:A12", note: "exact custom-field value" },
    ],
  },
  {
    title: "Combine",
    body: "Filters and free text combine with AND — mix as many as you like.",
    examples: [
      { q: "kind:video meta.height:>=1080 -tag:archived", note: "HD, not archived" },
      { q: "kind:audio ext:flac size:>50M", note: "large FLAC audio" },
    ],
  },
];
