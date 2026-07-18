package scan

import (
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// WalkEntry is one file yielded by Walk (directories are never yielded, only
// descended). Rel is posix-separated and relative to the walk root; MtimeNs is
// ModTime().UnixNano() (ruling 2: mtime stored as int nanoseconds).
type WalkEntry struct {
	Path    string // absolute path
	Rel     string // root-relative, posix separators
	Size    int64
	MtimeNs int64
}

// ScanRootError signals that the scan root is missing, not a directory, or
// unreadable — raised BEFORE the diff/tombstone phase so a dead mount aborts the
// scan cleanly instead of walking an empty tree and tombstoning everything
// (invariant 7). Mirrors scan.ScanRootError / assert_scannable_root.
type ScanRootError struct{ msg string }

func (e *ScanRootError) Error() string { return e.msg }

// assertScannableRoot is the pre-flight guard: the root must exist, be a
// directory, and be readable. Probes with an opendir + read-one-entry so a stale
// handle that errors only on read is still caught. Any error means the walk's
// emptiness cannot be trusted, so we refuse to proceed (never mass-tombstone).
// An empty but genuinely-mounted directory passes. Ports assert_scannable_root.
func assertScannableRoot(root string) error {
	fi, err := os.Stat(root)
	if err != nil || !fi.IsDir() {
		return &ScanRootError{msg: "scan root is missing or not a directory: " + root}
	}
	f, err := os.Open(root)
	if err != nil {
		return &ScanRootError{msg: "scan root is unreadable: " + root + " (" + err.Error() + ")"}
	}
	defer f.Close()
	// ReadDir(1) consumes exactly one entry so a stale handle that errors only on
	// read is caught; io.EOF just means the directory is empty (a clean pass).
	if _, err := f.ReadDir(1); err != nil && !errors.Is(err, io.EOF) {
		return &ScanRootError{msg: "scan root is unreadable: " + root + " (" + err.Error() + ")"}
	}
	return nil
}

// Walk performs the explicit-stack scandir walk, prune-then-descend, yielding
// each surviving file to visit. It is a behavioural port of scan.walk:
//
//   - Directories are pruned BEFORE descent via Spec.PruneDir (directory-only
//     gitignore pattern OR signature-verified CACHEDIR.TAG). Ruling R1: a pruned
//     tree is never entered, so nothing inside it can resurface.
//   - Files the spec excludes are dropped UNLESS classify() claims the file, in
//     which case it is kept as a sidecar (R1, file level).
//   - startRel seeds the walk at a subtree of root; emitted Rel stays relative to
//     root. The start directory itself is never prune-checked.
//   - Symlinks are not followed (treated as non-directories, like Python's
//     entry.is_dir(follow_symlinks=False)); a permission error on a directory
//     skips just that directory, mirroring the walk's except PermissionError.
//
// A visit callback returning a non-nil error aborts the walk with that error.
func Walk(root, startRel string, spec *Spec, visit func(WalkEntry) error) error {
	stack := []string{startRel}
	for len(stack) > 0 {
		relDir := stack[len(stack)-1]
		stack = stack[:len(stack)-1]

		current := root
		if relDir != "" {
			current = filepath.Join(root, filepath.FromSlash(relDir))
		}
		entries, err := os.ReadDir(current)
		if err != nil {
			if errors.Is(err, fs.ErrPermission) {
				continue // skip an unreadable directory, like except PermissionError
			}
			return err
		}
		for _, entry := range entries {
			var rel string
			if relDir != "" {
				rel = relDir + "/" + entry.Name()
			} else {
				rel = entry.Name()
			}
			abs := filepath.Join(current, entry.Name())
			if entry.IsDir() {
				// entry.IsDir() is false for symlinks (Type()==ModeSymlink), so
				// symlinked directories fall through to the file branch and are
				// yielded via lstat, exactly like the Python walk.
				if !spec.PruneDir(rel, abs) {
					stack = append(stack, rel)
				}
				continue
			}
			// File-level exclusion, R1-aware: an excluded file that a sidecar
			// classifier claims is kept (its parent is indexed by construction).
			if spec.MatchFile(rel) && classify(rel) == nil {
				continue
			}
			info, err := entry.Info()
			if err != nil {
				continue // vanished mid-walk (race); skip
			}
			if err := visit(WalkEntry{
				Path:    abs,
				Rel:     rel,
				Size:    info.Size(),
				MtimeNs: info.ModTime().UnixNano(),
			}); err != nil {
				return err
			}
		}
	}
	return nil
}

// normScope normalises a scan scope rel_path: strip slashes; "" => full scan.
// Mirrors scan._norm_scope.
func normScope(rel string) string {
	return strings.Trim(rel, "/")
}

// underScope reports whether a root-relative rel lies within scope
// (segment-aware). scope=="" (full scan) covers everything. Mirrors
// scan._under_scope.
func underScope(rel, scope string) bool {
	return scope == "" || rel == scope || strings.HasPrefix(rel, scope+"/")
}

// scopeDirMissing reports whether a non-empty scope subtree does not exist under
// root. A full scan (scope=="") is never "missing". Mirrors scan._scope_dir_missing.
func scopeDirMissing(root, scope string) bool {
	if scope == "" {
		return false
	}
	fi, err := os.Stat(filepath.Join(root, filepath.FromSlash(scope)))
	return err != nil || !fi.IsDir()
}
