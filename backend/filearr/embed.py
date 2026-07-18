"""Local embedder config + drift fingerprint + direct ONNX inference (P3-T8).

**Inert-by-default scaffolding.** Only tests import this module at collection
time; the heavy runtime deps (onnxruntime / tokenizers / huggingface_hub) are
imported *inside* ``_load_engine`` so importing this module never pulls them and
the test suite monkeypatches the ``embed_texts`` seam. It ships the pure
``EmbedderConfig`` + ``embedder_fingerprint`` (drift-detection basis) and the
real local ONNX embedding path used when ``FILEARR_SEMANTIC_ENABLED=true``.

FIX-7 (2026-07-12): the engine was **fastembed 0.7.1 → a direct onnxruntime
path**. fastembed 0.7.x pins ``pillow<12`` which is unsatisfiable against our
``pillow==12.3.0`` (pillow 11.x carries CVE-2026-25990 — downgrade forbidden,
security > convenience); fastembed ``main`` relaxed the pin but has NO release
(issue qdrant/fastembed#606). We therefore reproduce fastembed's exact
BAAI/bge-small-en-v1.5 pipeline with the primitives it wraps.

Ground truth (read out of fastembed 0.7.1 sources, ``fastembed/text/*`` +
``fastembed/common/*``) for ``BAAI/bge-small-en-v1.5``:

* **HF repo** ``Qdrant/bge-small-en-v1.5-onnx-Q`` (fastembed's ``ModelSource.hf``
  is ``qdrant/bge-small-en-v1.5-onnx-q``; HF resolves it case-insensitively to
  the canonical mixed-case id used here). dim **384**, license apache-2.0.
* **Model file** ``model_optimized.onnx`` (repo root). Companion files at root:
  ``tokenizer.json``, ``config.json``, ``tokenizer_config.json``,
  ``special_tokens_map.json`` (``vocab.txt`` unused — the fast tokenizer is
  self-contained in tokenizer.json).
* **Tokenization** (``fastembed/common/preprocessor_utils.py::load_tokenizer``):
  ``Tokenizer.from_file(tokenizer.json)``; ``enable_truncation(max_length=L)``
  where ``L = min(model_max_length, max_length)`` from tokenizer_config.json
  (BGE = **512**); ``enable_padding(pad_id=config.pad_token_id or 0,
  pad_token=tokenizer_config.pad_token)`` (dynamic pad to the longest row in the
  batch). Then batch-encode.
* **ONNX inputs** (``fastembed/text/onnx_text_model.py::onnx_embed``): always
  ``input_ids`` (int64); ``attention_mask`` (int64) IFF the model declares it;
  ``token_type_ids`` = zeros (int64) IFF the model declares it. BGE is a BERT →
  all three inputs present. Output ``model.run(None, ...)[0]`` = last hidden
  state, shape ``(batch, seq_len, 384)``.
* **Pooling** (``fastembed/text/onnx_embedding.py::OnnxTextEmbedding.
  _post_process_onnx_output``): BGE lives in the plain ``OnnxTextEmbedding``
  registry (NOT the pooled/mean-pooled ones), so pooling is **CLS = row[:, 0]**
  (first token), *not* mean pooling — verified in source.
* **Normalization** (``fastembed/common/utils.py::normalize``): L2 over the
  embedding axis, ``ord=2, axis=1, eps=1e-12``.

Other design constraints (brief §2 / §8):
- **Local-only inference, never a cloud API** (private files must not leave the
  box). ONNX Runtime CPU EP, deliberately NO torch in the image.
- Conservative threading: ONE intra-op thread by default (R3 — the live-Proxmox
  benchmark fixed the conservatism); overridable via ``FILEARR_EMBED_THREADS``.
- **Vectors live only in Meili** (disposable, invariant 1) — never persisted to
  Postgres. What *is* persisted is the embedder identity+version (a
  ``search_config`` row) so ``rebuild_index`` can detect drift.
  ``embedder_fingerprint`` is that basis: a stable hash of the config (now
  including the resolved HF repo + model file) that changes iff a re-embed is
  required.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Canonical defaults for BAAI/bge-small-en-v1.5 served via ONNX (see docstring).
# Kept identical to the Settings defaults so a hand-built EmbedderConfig and the
# settings-derived one fingerprint the same (the test suite relies on this).
DEFAULT_EMBED_REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
DEFAULT_EMBED_MODEL_FILE = "model_optimized.onnx"


@dataclass(frozen=True)
class EmbedderConfig:
    """Identity of the configured local embedder.

    ``model_id`` is the human-facing model name (display / stats). ``repo`` and
    ``model_file`` are the ACTUAL ONNX artifact resolved on Hugging Face — they
    are part of the fingerprint so swapping the served artifact (even for the
    "same" model_id) is honestly detected as drift and forces a re-embed.
    ``quantized`` records whether binary quantization is enabled — deferred until
    Hannoy BQ is verified against the live Meili and the corpus justifies it;
    part of the fingerprint because toggling it changes stored vectors.
    ``version`` disambiguates two builds of the "same" model id.
    """

    model_id: str
    dim: int
    quantized: bool = False
    version: str = "1"
    repo: str = DEFAULT_EMBED_REPO
    model_file: str = DEFAULT_EMBED_MODEL_FILE


def embedder_fingerprint(cfg: EmbedderConfig) -> str:
    """Return a stable, deterministic fingerprint for ``cfg``.

    Same config → same fingerprint (stability); any field change → a different
    fingerprint (drift detection). Incorporates the resolved HF repo + model file
    so a change of the served ONNX artifact is caught even when the display
    ``model_id`` is unchanged. Pure; fields serialised in a fixed order.
    """
    payload = "\x1f".join(
        [
            cfg.model_id,
            str(cfg.dim),
            "1" if cfg.quantized else "0",
            cfg.version,
            cfg.repo,
            cfg.model_file,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Local ONNX inference (P3-T8; engine swapped fastembed → direct onnxruntime in
# FIX-7). Everything below is LAZY: onnxruntime / tokenizers / huggingface_hub
# are imported only inside ``_load_engine`` so importing this module (as the
# whole test suite does) never pulls them, and tests monkeypatch ``embed_texts``.
# ---------------------------------------------------------------------------

# Only the first N chars of an item's body text feed the embedding — the model has
# a bounded context and the benchmark used a short filename-shaped input. Larger
# bodies add cost without materially improving a filename/title-dominated vector.
BODY_EMBED_CHARS = 512

# Well-known ``metadata_`` keys the embed pipeline writes (extracted-fact column,
# invariant 2 — the embed stage is an extractor). The vector rides per-item so a
# rebuild re-projects it WITHOUT re-embedding; ``_embedding_fp`` is the drift tag.
EMBEDDING_KEY = "_embedding"
FINGERPRINT_KEY = "_embedding_fp"

_MODEL_CACHE: dict[tuple[str, str], _Engine] = {}


@dataclass
class _Engine:
    """A loaded ONNX embedding engine: the onnxruntime session, the configured
    fast tokenizer, and the set of input tensor names the model declares (so we
    only feed ``attention_mask`` / ``token_type_ids`` when the graph wants them —
    matches fastembed's dynamic input selection)."""

    session: Any
    tokenizer: Any
    input_names: frozenset[str]


def _build_tokenizer(model_dir: Path) -> Any:
    """Construct the fast tokenizer exactly as fastembed's ``load_tokenizer``:
    truncation to ``min(model_max_length, max_length)`` and dynamic padding to the
    batch's longest row, using pad id/token from config + tokenizer_config."""
    from tokenizers import AddedToken, Tokenizer

    with open(model_dir / "config.json") as f:
        config = json.load(f)
    with open(model_dir / "tokenizer_config.json") as f:
        tok_cfg = json.load(f)

    has_mml = "model_max_length" in tok_cfg
    has_ml = "max_length" in tok_cfg
    if has_mml and has_ml:
        max_context = min(tok_cfg["model_max_length"], tok_cfg["max_length"])
    elif has_mml:
        max_context = tok_cfg["model_max_length"]
    elif has_ml:
        max_context = tok_cfg["max_length"]
    else:
        # BGE always ships model_max_length; fall back to the model's 512 rather
        # than failing so a stray tokenizer_config never breaks inference.
        max_context = 512

    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=max_context)
    tokenizer.enable_padding(
        pad_id=config.get("pad_token_id", 0), pad_token=tok_cfg["pad_token"]
    )

    special_map_path = model_dir / "special_tokens_map.json"
    if special_map_path.exists():
        with open(special_map_path) as f:
            tokens_map = json.load(f)
        for token in tokens_map.values():
            if isinstance(token, str):
                tokenizer.add_special_tokens([token])
            elif isinstance(token, dict):
                tokenizer.add_special_tokens([AddedToken(**token)])
    return tokenizer


def _load_engine(cfg: EmbedderConfig) -> _Engine:
    """Lazily download (once) + process-cache the ONNX engine for ``cfg``.

    Files are fetched with ``huggingface_hub.hf_hub_download`` into
    ``settings.embed_model_cache`` (a persistent /config volume); a second call
    reuses the local cache and works offline. The heavy deps are imported INSIDE
    this function so module import stays cheap. The ONE embed worker holds a
    single cached engine (memory-capped per the ruling)."""
    key = (cfg.repo, cfg.model_file)
    engine = _MODEL_CACHE.get(key)
    if engine is not None:
        return engine

    import onnxruntime as ort
    from huggingface_hub import hf_hub_download

    from filearr.config import get_settings

    settings = get_settings()
    cache_dir = settings.embed_model_cache

    # FIX-11: the ONNX model (~130 MB) downloads into the /config cache volume.
    # Refuse the download when that filesystem is at the critical low-space floor
    # so a first-enable on an already-tight box cannot be the write that fills the
    # disk. Cached (already-downloaded) engines are served above without ever
    # reaching this guard, so an offline/steady-state worker is unaffected.
    from filearr import diskguard

    diskguard.guard_write(cache_dir, settings)

    # Download every companion the tokenizer/session needs; they land in the same
    # snapshot dir (hf_hub_download returns per-file paths — the model file's
    # parent is that shared dir).
    model_path = hf_hub_download(
        repo_id=cfg.repo, filename=cfg.model_file, cache_dir=cache_dir
    )
    for companion in (
        "tokenizer.json",
        "config.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    ):
        hf_hub_download(repo_id=cfg.repo, filename=companion, cache_dir=cache_dir)

    tokenizer = _build_tokenizer(Path(model_path).parent)

    so = ort.SessionOptions()
    # Conservative CPU threading (R3): one intra-op thread by default so the ONE
    # lowest-priority embed worker never fans out and starves extraction; tunable.
    so.intra_op_num_threads = max(1, settings.embed_threads)
    so.inter_op_num_threads = 1
    session = ort.InferenceSession(
        model_path, sess_options=so, providers=["CPUExecutionProvider"]
    )
    input_names = frozenset(i.name for i in session.get_inputs())

    engine = _Engine(session=session, tokenizer=tokenizer, input_names=input_names)
    _MODEL_CACHE[key] = engine
    return engine


def _cls_pool_normalize(model_output: Any) -> Any:
    """CLS-pool + L2-normalize a raw model output, matching fastembed's
    ``OnnxTextEmbedding._post_process_onnx_output`` for BGE.

    ``model_output`` is the last hidden state: 3-D ``(batch, seq_len, dim)`` →
    take the CLS token (``[:, 0]``); already-pooled 2-D ``(batch, dim)`` is passed
    through. Then L2-normalize over the embedding axis (ord=2, eps=1e-12). Pure /
    numpy-only so it is unit-testable with hand-built matrices."""
    import numpy as np

    arr = np.asarray(model_output, dtype=np.float32)
    if arr.ndim == 3:
        pooled = arr[:, 0]
    elif arr.ndim == 2:
        pooled = arr
    else:
        raise ValueError(f"Unsupported embedding shape: {arr.shape}")
    norm = np.linalg.norm(pooled, ord=2, axis=1, keepdims=True)
    norm = np.maximum(norm, 1e-12)
    return pooled / norm


def _embed_batch(engine: _Engine, texts: list[str]) -> Any:
    """Tokenize → run the session → CLS-pool → L2-normalize one batch. Returns a
    numpy array ``(len(texts), dim)``. Feeds ``attention_mask`` /
    ``token_type_ids`` only when the graph declares them (fastembed parity)."""
    import numpy as np

    encoded = engine.tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
    onnx_input: dict[str, Any] = {"input_ids": input_ids}
    if "attention_mask" in engine.input_names:
        onnx_input["attention_mask"] = np.array(
            [e.attention_mask for e in encoded], dtype=np.int64
        )
    if "token_type_ids" in engine.input_names:
        onnx_input["token_type_ids"] = np.zeros_like(input_ids)
    model_output = engine.session.run(None, onnx_input)[0]
    return _cls_pool_normalize(model_output)


def embed_texts(texts: Sequence[str], cfg: EmbedderConfig) -> list[list[float]]:
    """Embed ``texts`` with the configured local ONNX model — one dense vector per
    input, in order. Empty input short-circuits (no model load). Vectors are
    plain ``list[float]`` (JSON/JSONB-serialisable) so they can ride an item's
    ``metadata_`` and a Meili document ``_vectors`` block unchanged."""
    texts = list(texts)
    if not texts:
        return []
    from filearr.config import get_settings

    engine = _load_engine(cfg)
    batch = max(1, get_settings().embed_batch)
    out: list[list[float]] = []
    for start in range(0, len(texts), batch):
        chunk = texts[start : start + batch]
        vecs = _embed_batch(engine, chunk)
        out.extend([[float(x) for x in row] for row in vecs])
    return out


def embed_query(text: str, cfg: EmbedderConfig | None = None) -> list[float]:
    """Embed a single query string (the API's query-time path — 39 ms, approved).
    Uses the configured embedder when ``cfg`` is omitted."""
    if cfg is None:
        from filearr.config import get_settings

        cfg = get_settings().embedder_config
    out = embed_texts([text], cfg)
    return out[0] if out else []


def embed_text_for_item(doc: Mapping[str, Any]) -> str:
    """Build the text embedded for one item (matches the benchmark shape):
    ``filename + title + artist/album/author + tags + first 512 chars body_text``.

    ``doc`` is any mapping carrying those keys — a ``build_doc`` search document
    or the item-derived dict from :func:`embed_source_from_item`. Missing/empty
    fields are simply skipped; order is stable so the same item always yields the
    same text (and therefore the same vector)."""
    parts: list[str] = []
    for key in ("filename", "title", "artist", "album", "author"):
        v = doc.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    tags = doc.get("tags")
    if isinstance(tags, (list, tuple)):
        parts.extend(str(t) for t in tags if t)
    body = doc.get("body_text")
    if isinstance(body, str) and body.strip():
        parts.append(body.strip()[:BODY_EMBED_CHARS])
    return " ".join(parts)


def embed_source_from_item(item: Any) -> dict[str, Any]:
    """Project an ``Item`` down to the fields :func:`embed_text_for_item` reads
    (effective_metadata overlay applied), so the embed task stays DB-thin and the
    text builder stays a pure mapping consumer. Body text prefers native
    ``body_text`` then OCR ``ocr_text`` — the same combined body search indexes."""
    meta = item.effective_metadata
    return {
        "filename": item.filename,
        "title": item.title or meta.get("title") or item.filename,
        "artist": meta.get("artist"),
        "album": meta.get("album"),
        "author": meta.get("author"),
        "tags": item.tags,
        "body_text": meta.get("body_text") or meta.get("ocr_text"),
    }


def has_current_embedding(meta: Mapping[str, Any], cfg: EmbedderConfig) -> bool:
    """True when ``meta`` carries a vector whose stored fingerprint matches ``cfg``
    (i.e. the current embedder produced it). A mismatch means the model changed
    (drift) — the vector must be regenerated and is meanwhile omitted from the
    projection. A missing vector/fingerprint is simply "not embedded yet"."""
    emb = meta.get(EMBEDDING_KEY)
    fp = meta.get(FINGERPRINT_KEY)
    return isinstance(emb, list) and bool(emb) and fp == embedder_fingerprint(cfg)


def strip_embedding(meta: dict[str, Any]) -> dict[str, Any]:
    """Return ``meta`` without the internal embedding keys — used by the item API
    so a raw-metadata response never ships the ~1.5 KB vector (or its fp) to a
    client. Non-mutating (like ``exif.strip_gps``)."""
    if EMBEDDING_KEY not in meta and FINGERPRINT_KEY not in meta:
        return meta
    return {k: v for k, v in meta.items() if k not in (EMBEDDING_KEY, FINGERPRINT_KEY)}
