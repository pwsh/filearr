// P4-T10 — per-`media_type` detail-card registry.
//
// Maps a MediaType value to a hand-written Svelte detail component. An
// unregistered / future media type (e.g. `sample`, `other`, or a type added to
// the backend before its card ships) falls back to the generic KeyFactsCard, so
// extractor work landing ahead of frontend polish is NEVER blocked by a missing
// card. ItemDetail resolves the component via `cardFor(media_type)` and renders
// it with native Svelte-5 dynamic-component syntax (`{@const C = cardFor(mt)}` +
// `<C {item} />` — no `<svelte:component>`).

import type { Component } from "svelte";
import VideoCard from "./VideoCard.svelte";
import AudioCard from "./AudioCard.svelte";
import ImageCard from "./ImageCard.svelte";
import Model3dCard from "./Model3dCard.svelte";
import DocumentCard from "./DocumentCard.svelte";
import SpreadsheetCard from "./SpreadsheetCard.svelte";
import KeyFactsCard from "./KeyFactsCard.svelte";

// A card component always takes at least `{ item }`; KeyFactsCard additionally
// accepts optional `exclude`/`heading` (widened here so all cards share a type).
type CardProps = { item: Record<string, unknown>; exclude?: string[]; heading?: string };
export type CardComponent = Component<CardProps>;

export const CARD_REGISTRY: Record<string, CardComponent> = {
  video: VideoCard as CardComponent,
  audio: AudioCard as CardComponent,
  audiobook: AudioCard as CardComponent,
  image: ImageCard as CardComponent,
  model3d: Model3dCard as CardComponent,
  document: DocumentCard as CardComponent,
  spreadsheet: SpreadsheetCard as CardComponent,
};

// Human tab labels per media type (the fallback keeps a neutral "Details").
const CARD_LABELS: Record<string, string> = {
  video: "Video",
  audio: "Audio",
  audiobook: "Audiobook",
  image: "Image",
  model3d: "3D model",
  document: "Document",
  spreadsheet: "Spreadsheet",
};

/** The detail component for a media type, or the generic KeyFactsCard fallback
 *  for an unregistered / future type (never throws). */
export function cardFor(mediaType: string | undefined | null): CardComponent {
  return (mediaType && CARD_REGISTRY[mediaType]) || (KeyFactsCard as CardComponent);
}

/** The tab label for a media type's card ("Details" for the generic fallback). */
export function cardLabel(mediaType: string | undefined | null): string {
  return (mediaType && CARD_LABELS[mediaType]) || "Details";
}
