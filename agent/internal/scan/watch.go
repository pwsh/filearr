package scan

import (
	"context"
	"os"
	"path/filepath"
	"time"

	"github.com/fsnotify/fsnotify"
)

// DefaultSettle is the coalescing window for watch mode: a burst of filesystem
// events (e.g. a large copy) collapses into a single rescan once the tree has
// been idle for this long. Mirrors backend/filearr/watch.py:DEBOUNCE_S = 3.0s.
// fsnotify has no built-in debounce, so the settle is hand-rolled here (ruling:
// watch mode is offered unconditionally — agent roots are local by construction,
// so central's is_network_path refusal is not ported).
const DefaultSettle = 3 * time.Second

// Watch observes root (recursively) and calls onChange once per settled burst of
// changes, until ctx is cancelled. settle is the idle window; pass a small value
// in tests. onChange runs on the watch goroutine — a typical caller defers a
// full/scoped rescan of root.
func Watch(ctx context.Context, root string, settle time.Duration, onChange func()) error {
	if settle <= 0 {
		settle = DefaultSettle
	}
	w, err := fsnotify.NewWatcher()
	if err != nil {
		return err
	}
	defer w.Close()

	if err := addTreeToWatcher(w, root); err != nil {
		return err
	}

	// A single timer coalesces events: every event (re)arms it for `settle`; when
	// it finally fires, exactly one onChange runs for the whole burst.
	timer := time.NewTimer(settle)
	if !timer.Stop() {
		<-timer.C
	}
	armed := false

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case ev, ok := <-w.Events:
			if !ok {
				return nil
			}
			// A newly-created directory must be watched too (fsnotify is not
			// recursive); best-effort — a race where it vanishes is ignored.
			if ev.Op&fsnotify.Create != 0 {
				if fi, statErr := os.Stat(ev.Name); statErr == nil && fi.IsDir() {
					_ = addTreeToWatcher(w, ev.Name)
				}
			}
			if armed && !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			timer.Reset(settle)
			armed = true
		case <-timer.C:
			armed = false
			onChange()
		case _, ok := <-w.Errors:
			if !ok {
				return nil
			}
			// Transient watcher errors must not kill the watch loop.
		}
	}
}

// addTreeToWatcher adds root and every existing subdirectory to the watcher.
// fsnotify watches directories (not whole trees), so we seed the current set;
// new directories are added on their Create event. Unreadable subdirectories are
// skipped rather than aborting the watch.
func addTreeToWatcher(w *fsnotify.Watcher, root string) error {
	return filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return nil // skip unreadable entries
		}
		if d.IsDir() {
			_ = w.Add(p)
		}
		return nil
	})
}
