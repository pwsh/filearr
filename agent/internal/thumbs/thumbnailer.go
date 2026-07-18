package thumbs

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/time/rate"
)

// Media-type vocabulary (mirrors backend/filearr/models.py:MediaType values, as
// stored on the agent's local items.media_type). Thumbnailable = image + audio
// family + video. document(PDF) is DELIBERATELY excluded on the agent: pure-Go
// PDF rasterization is not production-grade under CGO_ENABLED=0, so agent PDFs get
// no thumbnail until a later slice (documented deviation). Central still generates
// PDF thumbs for CENTRAL-hosted libraries.
const (
	mediaImage     = "image"
	mediaVideo     = "video"
	mediaAudio     = "audio"
	mediaAudiobook = "audiobook"
	mediaSample    = "sample"
)

// isThumbnailable reports whether the agent can produce a thumbnail for a media
// type. Video is included only when ffmpeg is available (capability-gated by the
// Thumbnailer); the type gate here is format-agnostic.
func isThumbnailable(mediaType string) bool {
	switch mediaType {
	case mediaImage, mediaVideo, mediaAudio, mediaAudiobook, mediaSample:
		return true
	default:
		return false
	}
}

// Candidate is one local item eligible for thumbnail generation. RootPath is the
// item's scan-root absolute path; the source file is RootPath joined with RelPath.
// SidecarRels are the rel_paths of this item's linked sidecar children (central's
// source-resolution Rule 0 prefers a linked ARTWORK sidecar — poster/-thumb —
// over a decoded frame; the pass filters these with the injected IsArtwork
// classifier since the index package cannot import scan).
type Candidate struct {
	ItemID      string
	RootPath    string
	RelPath     string
	MediaType   string
	ContentHash string
	QuickHash   string
	SidecarRels []string
}

// hashUsed mirrors central generate_and_store: content_hash preferred, quick_hash
// fallback, "" (skip) when neither is set. Keying on the SAME hash central will
// use is what makes the agent's key match central's derivation.
func (c Candidate) hashUsed() string {
	if c.ContentHash != "" {
		return c.ContentHash
	}
	return c.QuickHash
}

// Marker records which (item, tier) thumbnails an agent has already generated +
// uploaded, keyed on the cache key. A stored key that differs from the freshly
// computed expected key (a changed file → new hash → new key, or a
// GeneratorVersion bump) means "regenerate".
type Marker struct {
	Tier     int
	CacheKey string
}

// Store is the local-index surface the pass needs (satisfied by *index.Store). It
// is intentionally narrow so the pass unit-tests against a fake without a SQLite
// database.
type Store interface {
	// ThumbCandidates returns active, non-sidecar, thumbnailable, hashed items
	// owned locally, each with its linked artwork sidecar (if any).
	ThumbCandidates(ctx context.Context) ([]Candidate, error)
	// ThumbMarkers returns the (tier→cache_key) markers already recorded for an
	// item (empty when none).
	ThumbMarkers(ctx context.Context, itemID string) ([]Marker, error)
	// MarkThumb records that (itemID, tier) was uploaded under cacheKey (upsert).
	MarkThumb(ctx context.Context, itemID string, tier int, cacheKey string) error
}

// Uploader pushes one generated thumbnail to central. The item is referenced by
// (libraryRef, relPath) — NOT a UUID — because central owns item ids and resolves
// an agent item by (the library it materialised for this agent + library_ref) and
// rel_path, exactly as the command/replication planes do (the agent's local ids
// never cross the wire). That resolution ALSO authorises ownership by construction
// (the library is looked up under the uploading agent).
//
// stored=true means central durably accepted+stored it (2xx) and the marker may be
// recorded; stored=false with a nil error is a NON-FATAL decline (item not yet
// replicated → 404, or a transient key race → 409): the pass leaves the marker
// unset and retries on a later pass. A non-nil error is a transport failure (also
// a retry-next-pass signal).
type Uploader interface {
	Upload(ctx context.Context, libraryRef, relPath string, tier int, key string, tb *ThumbBytes) (stored bool, err error)
}

// Config configures a Thumbnailer.
type Config struct {
	Store    Store
	Uploader Uploader

	// FFmpegPath is the resolved ffmpeg binary for video frames, or "" when ffmpeg
	// is not on PATH (video thumbnails are then skipped, logged once). The caller
	// (daemon wiring) probes PATH once and passes the result.
	FFmpegPath string

	// Tiers to generate per item (default: grid + preview). Central pregenerates
	// grid and lazily makes preview; the agent must make both because central
	// cannot reach the source file to lazily generate a preview for an agent item.
	Tiers []TierSpec

	// IsArtwork classifies a sidecar rel_path as artwork (poster/-thumb/…) for the
	// Rule-0 source preference. Injected (default nil → no sidecar preference, only
	// native generation) because the index package cannot import scan.
	IsArtwork func(relPath string) bool

	// RatePerSec throttles generation to keep the pass a LOW-PRIORITY background
	// walk (items/sec; <=0 → a small default). Concurrency is always 1.
	RatePerSec float64
	// Interval between passes in Run (default 5m). A pass processes every pending
	// candidate then sleeps.
	Interval time.Duration

	Logger *slog.Logger
}

// Thumbnailer runs the post-scan thumbnail generation pass: for each candidate
// item missing an up-to-date marker, resolve a source, generate each tier, upload
// to central, and record the marker on success. Concurrency 1, rate-limited,
// cancellable with the daemon ctx.
type Thumbnailer struct {
	store       Store
	uploader    Uploader
	ffmpegPath  string
	tiers       []TierSpec
	isArtwork   func(string) bool
	interval    time.Duration
	limiter     *rate.Limiter
	log         *slog.Logger
	loggedNoFFm bool
}

// PassStats is the per-pass tally (returned by RunPass, logged by Run).
type PassStats struct {
	Candidates int
	Generated  int // tiers successfully generated + stored on central
	Uploaded   int // tiers central accepted (== Generated on success)
	Skipped    int // tiers with no decodable source / not yet needed
	Deferred   int // tiers central declined (not replicated yet / transient)
	Errors     int
}

// New wires a Thumbnailer, applying defaults.
func New(cfg Config) *Thumbnailer {
	tiers := cfg.Tiers
	if len(tiers) == 0 {
		tiers = []TierSpec{GridSpec, PreviewSpec}
	}
	interval := cfg.Interval
	if interval <= 0 {
		interval = 5 * time.Minute
	}
	rps := cfg.RatePerSec
	if rps <= 0 {
		rps = 5 // gentle default: ~5 items/sec
	}
	log := cfg.Logger
	if log == nil {
		log = slog.New(slog.NewTextHandler(nopWriter{}, nil))
	}
	return &Thumbnailer{
		store:      cfg.Store,
		uploader:   cfg.Uploader,
		ffmpegPath: cfg.FFmpegPath,
		tiers:      tiers,
		isArtwork:  cfg.IsArtwork,
		interval:   interval,
		limiter:    rate.NewLimiter(rate.Limit(rps), 1),
		log:        log,
	}
}

// Run walks the candidate set every Interval until ctx is cancelled. Each pass is
// bounded (it processes the current pending set and returns); a failure is logged
// and the loop continues (thumbnails are disposable — a transient error must
// never crash the daemon).
func (t *Thumbnailer) Run(ctx context.Context) error {
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		stats, err := t.RunPass(ctx)
		if err != nil && ctx.Err() == nil {
			t.log.Warn("thumbnail pass failed", "err", err)
		} else if stats.Generated > 0 || stats.Deferred > 0 {
			t.log.Info("thumbnail pass",
				"candidates", stats.Candidates, "generated", stats.Generated,
				"deferred", stats.Deferred, "skipped", stats.Skipped, "errors", stats.Errors)
		}
		if !sleepCtx(ctx, t.interval) {
			return ctx.Err()
		}
	}
}

// RunPass processes every pending candidate exactly once and returns the tally.
// It is the unit-testable core (Run just loops it on a timer).
func (t *Thumbnailer) RunPass(ctx context.Context) (PassStats, error) {
	var stats PassStats
	cands, err := t.store.ThumbCandidates(ctx)
	if err != nil {
		return stats, err
	}
	stats.Candidates = len(cands)
	for _, c := range cands {
		if err := ctx.Err(); err != nil {
			return stats, err
		}
		t.processCandidate(ctx, c, &stats)
	}
	return stats, nil
}

func (t *Thumbnailer) processCandidate(ctx context.Context, c Candidate, stats *PassStats) {
	hash := c.hashUsed()
	if hash == "" || !isThumbnailable(c.MediaType) {
		return
	}
	markers, err := t.store.ThumbMarkers(ctx, c.ItemID)
	if err != nil {
		stats.Errors++
		return
	}
	have := map[int]string{}
	for _, m := range markers {
		have[m.Tier] = m.CacheKey
	}
	for _, spec := range t.tiers {
		key := CacheKey(hash, GeneratorVersion, spec.Tier)
		if have[spec.Tier] == key {
			continue // already generated + uploaded under this exact key
		}
		// Rate-limit BEFORE the (potentially heavy) decode/ffmpeg to keep the pass
		// a low-priority background walk.
		if err := t.limiter.Wait(ctx); err != nil {
			stats.Errors++
			return
		}
		tb := t.generate(ctx, c, spec)
		if tb == nil {
			stats.Skipped++
			continue
		}
		stored, err := t.uploader.Upload(ctx, c.RootPath, c.RelPath, spec.Tier, key, tb)
		if err != nil {
			stats.Errors++
			t.log.Debug("thumbnail upload failed", "item", c.ItemID, "tier", spec.Tier, "err", err)
			continue
		}
		if !stored {
			// Central declined (item not yet replicated / transient key race):
			// don't mark, retry next pass.
			stats.Deferred++
			continue
		}
		if err := t.store.MarkThumb(ctx, c.ItemID, spec.Tier, key); err != nil {
			// The blob is safely on central; a marker write failure just means we
			// re-upload (idempotent write-if-absent) next pass. Count it, move on.
			stats.Errors++
			continue
		}
		stats.Generated++
		stats.Uploaded++
	}
}

// generate resolves a source and produces the tier thumbnail, mirroring central's
// _resolve_source order: a linked artwork sidecar (Rule 0) FIRST, then the item's
// own file by media type. Returns nil when no source yields a thumbnail.
func (t *Thumbnailer) generate(ctx context.Context, c Candidate, spec TierSpec) *ThumbBytes {
	// Rule 0: an artwork sidecar (poster.jpg / -thumb.jpg …) wins for ANY parent
	// media type — zero-cost, often better than a decoded frame.
	if t.isArtwork != nil {
		for _, rel := range c.SidecarRels {
			if !t.isArtwork(rel) {
				continue
			}
			art := filepath.Join(c.RootPath, filepath.FromSlash(rel))
			if fileExists(art) {
				if tb := GenerateImageThumb(art, spec); tb != nil {
					return tb
				}
			}
		}
	}

	full := filepath.Join(c.RootPath, filepath.FromSlash(c.RelPath))
	switch c.MediaType {
	case mediaImage:
		return GenerateImageThumb(full, spec)
	case mediaAudio, mediaAudiobook, mediaSample:
		raw := ExtractAudioCover(full)
		if raw == nil {
			return nil
		}
		return GenerateThumbFromBytes(raw, spec)
	case mediaVideo:
		if t.ffmpegPath == "" {
			if !t.loggedNoFFm {
				t.log.Info("ffmpeg not found on PATH; agent video thumbnails disabled")
				t.loggedNoFFm = true
			}
			return nil
		}
		return GenerateVideoThumb(ctx, t.ffmpegPath, full, spec, VideoMinSeekSeconds)
	default:
		return nil
	}
}

func fileExists(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && !fi.IsDir()
}

// sleepCtx sleeps for d or until ctx is cancelled; false => cancelled first.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		select {
		case <-ctx.Done():
			return false
		default:
			return true
		}
	}
	tm := time.NewTimer(d)
	defer tm.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-tm.C:
		return true
	}
}

// nopWriter discards logger output when no logger is supplied.
type nopWriter struct{}

func (nopWriter) Write(p []byte) (int, error) { return len(p), nil }
