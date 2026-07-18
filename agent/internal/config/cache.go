package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// policyFileName is the cached policy document under the agent data dir.
const policyFileName = "policy.json"

// ETagCache is the offline-first persistent cache of the last-fetched policy
// document. It stores <DataDir>/policy.json with an atomic temp-then-rename
// write (the same durability pattern as enroll.CertStore) so a reader never
// observes a half-written file, and loads the last-known policy on startup so
// the agent applies central policy with no network.
type ETagCache struct {
	path string
}

// NewETagCache returns a cache rooted at dataDir. The directory is created
// lazily on the first Save.
func NewETagCache(dataDir string) *ETagCache {
	return &ETagCache{path: filepath.Join(dataDir, policyFileName)}
}

// Path returns the backing file path (handy for diagnostics/tests).
func (c *ETagCache) Path() string { return c.path }

// Load reads the cached policy document. ok is false (with a nil error) when no
// cache file exists yet — the normal first-run state.
func (c *ETagCache) Load() (doc PolicyDoc, ok bool, err error) {
	buf, err := os.ReadFile(c.path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return PolicyDoc{}, false, nil
		}
		return PolicyDoc{}, false, fmt.Errorf("read policy cache: %w", err)
	}
	if err := json.Unmarshal(buf, &doc); err != nil {
		return PolicyDoc{}, false, fmt.Errorf("parse policy cache %s: %w", c.path, err)
	}
	return doc, true, nil
}

// Save atomically persists doc. The policy body is stored as a raw message so
// unknown keys round-trip byte-for-byte.
func (c *ETagCache) Save(doc PolicyDoc) error {
	doc.Policy = normalizeRaw(doc.Policy)
	if err := os.MkdirAll(filepath.Dir(c.path), 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	buf, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal policy doc: %w", err)
	}
	buf = append(buf, '\n')
	return atomicWrite(c.path, buf, 0o644)
}

// atomicWrite writes to a temp file in the same directory then renames over the
// target. Mirrors enroll.CertStore.atomicWrite (temp+sync+rename): a reader
// never sees a partial file, and on Windows os.Rename replaces the target
// atomically for same-volume files.
func atomicWrite(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-policy-*")
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

// LoadCachedPolicy is a convenience for one-shot readers (the `scan` path): it
// loads the cached document and returns its honored-key view. ok is false when
// no policy has been cached yet.
func LoadCachedPolicy(dataDir string) (pol Policy, ok bool, err error) {
	doc, ok, err := NewETagCache(dataDir).Load()
	if err != nil || !ok {
		return Policy{}, ok, err
	}
	pol, err = doc.Parsed()
	if err != nil {
		return Policy{}, true, err
	}
	return pol, true, nil
}
