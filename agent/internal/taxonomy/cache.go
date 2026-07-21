package taxonomy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
)

// cacheFileName is the persisted compact taxonomy under the agent data dir.
const cacheFileName = "taxonomy.json"

// Cache is the process-shared, offline-first taxonomy the scanner classifies
// against. It holds the current immutable snapshot behind an RWMutex, loads the
// last-known snapshot from <dataDir>/taxonomy.json on start (falling back to the
// baked-in seed), and version-gates a Refresh off the policy's taxonomy_version.
//
// A snapshot is never mutated in place — Refresh swaps a whole new pointer — so a
// concurrent scan reading Current() sees a consistent snapshot for its whole run.
type Cache struct {
	mu   sync.RWMutex
	snap *Taxonomy
	path string
	log  *slog.Logger
}

// NewCache builds a cache rooted at dataDir and loads its initial snapshot: the
// persisted taxonomy.json when present and valid, else the baked-in seed. It
// never fails — a missing/corrupt cache degrades to the seed so classification
// always works.
func NewCache(dataDir string, log *slog.Logger) *Cache {
	if log == nil {
		log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	c := &Cache{path: filepath.Join(dataDir, cacheFileName), log: log}
	c.snap = c.loadInitial()
	return c
}

// loadInitial reads the persisted snapshot, falling back to the seed on any
// absence/parse error (never nil).
func (c *Cache) loadInitial() *Taxonomy {
	buf, err := os.ReadFile(c.path)
	if err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			c.log.Warn("read taxonomy cache failed; using baked-in seed", "err", err)
		}
		return SeedOrEmpty()
	}
	snap, err := ParsePayload(buf)
	if err != nil {
		c.log.Warn("parse taxonomy cache failed; using baked-in seed", "path", c.path, "err", err)
		return SeedOrEmpty()
	}
	return snap
}

// Current returns the current immutable snapshot (never nil). Safe for
// concurrent use; the returned snapshot is stable even across a later Refresh.
func (c *Cache) Current() *Taxonomy {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.snap
}

// Version reports the cached snapshot's central version (SeedVersion==0 when the
// agent has never fetched a real taxonomy).
func (c *Cache) Version() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.snap.version
}

// Refresh version-gates a fetch: when wantVersion exceeds the cached snapshot's
// version it pulls the compact payload via client, atomically persists it, and
// swaps in the new snapshot. A no-op (nil) when wantVersion is not newer. On a
// fetch/parse error the current snapshot is kept and the error returned. A
// fetched snapshot that is not actually newer than the cache is ignored (guards
// against a downgrade / a stale central read).
func (c *Cache) Refresh(ctx context.Context, client *Client, wantVersion int) error {
	if wantVersion <= c.Version() {
		return nil
	}
	snap, err := client.Fetch(ctx)
	if err != nil {
		return fmt.Errorf("fetch taxonomy v%d: %w", wantVersion, err)
	}
	// Guard: never replace a snapshot with an older/equal one.
	if snap.version <= c.Version() {
		c.log.Warn("fetched taxonomy is not newer than cache; keeping current",
			"fetched", snap.version, "cached", c.Version())
		return nil
	}
	if err := c.persist(snap); err != nil {
		// Persistence failed, but the fetched snapshot is valid — apply it in memory
		// so the running scanner uses the current taxonomy; a restart re-fetches.
		c.log.Warn("persist taxonomy failed; applying in memory only", "err", err)
	}
	c.mu.Lock()
	c.snap = snap
	c.mu.Unlock()
	c.log.Info("applied taxonomy", "version", snap.version)
	return nil
}

// persist atomically writes the snapshot to <dataDir>/taxonomy.json (temp file +
// rename), the same durability pattern as the policy ETag cache.
func (c *Cache) persist(snap *Taxonomy) error {
	buf, err := snap.Marshal()
	if err != nil {
		return err
	}
	buf = append(buf, '\n')
	if err := os.MkdirAll(filepath.Dir(c.path), 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	return atomicWrite(c.path, buf, 0o644)
}

// atomicWrite writes to a temp file in the same directory then renames over the
// target (mirrors config.atomicWrite): a reader never sees a partial file.
func atomicWrite(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-taxonomy-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmpName, perm); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}
