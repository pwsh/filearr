"""One-time CPU embedding benchmark for Filearr semantic search (P3-T8).

Measures model load time + throughput (texts/sec) for the candidate local
embedders on THIS machine, using filename/metadata-shaped inputs (short texts —
matches Filearr's real payload, not paragraph prose). Results decide the embed
queue's concurrency default and the model pin.

Run (throwaway container, nothing installed on the host):
  docker run --rm -v $(pwd):/s python:3.12-slim \
    bash -c "pip -q install sentence-transformers && python /s/bench_embed.py"
"""

import resource
import time

MODELS = [
    # (hf id, needs trust_remote_code)
    ("BAAI/bge-small-en-v1.5", False),          # fast candidate, 384d
    ("nomic-ai/nomic-embed-text-v1.5", True),   # preferred candidate, 768d
]
N, BATCH = 256, 32

texts = [
    f"Movies/Some Film ({1990 + i % 35})/Some.Film.{1990 + i % 35}.1080p.BluRay.x264.mkv"
    f" title:Some Film {i} artist:Various codec:h264 tags:demo,sample"
    for i in range(N)
]

for model_id, trust in MODELS:
    try:
        from sentence_transformers import SentenceTransformer

        t0 = time.perf_counter()
        m = SentenceTransformer(model_id, device="cpu", trust_remote_code=trust)
        load_s = time.perf_counter() - t0

        m.encode(texts[:8], batch_size=8)  # warmup
        t0 = time.perf_counter()
        vecs = m.encode(texts, batch_size=BATCH, show_progress_bar=False)
        dt = time.perf_counter() - t0

        t0 = time.perf_counter()
        m.encode(["single query latency probe"])
        q_ms = (time.perf_counter() - t0) * 1000

        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(
            f"RESULT model={model_id} dim={vecs.shape[1]} load={load_s:.1f}s "
            f"throughput={N / dt:.1f} texts/s query={q_ms:.0f}ms rss={rss_mb:.0f}MB"
        )
    except Exception as e:  # noqa: BLE001 - report and continue to next model
        print(f"RESULT model={model_id} FAILED: {type(e).__name__}: {e}")
