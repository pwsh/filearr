package install

import (
	"fmt"
	"io"
	"os"
)

// OSFS is the production FS backed by the real filesystem.
type OSFS struct{}

func (OSFS) MkdirAll(path string, perm os.FileMode) error { return os.MkdirAll(path, perm) }

func (OSFS) Remove(path string) error    { return os.Remove(path) }
func (OSFS) RemoveAll(path string) error { return os.RemoveAll(path) }

// CopyFile copies src to dst via a temp-then-rename in dst's directory so a
// half-written binary is never observed, then sets perm. Used to place the agent
// binary into the install dir (a plain rename would fail across volumes, and
// overwriting a running binary in place is what the updater's A/B swap handles;
// here dst is a fresh/idle install path).
func (OSFS) CopyFile(src, dst string, perm os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return fmt.Errorf("open %s: %w", src, err)
	}
	defer in.Close()
	tmp := dst + ".tmp"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, perm)
	if err != nil {
		return fmt.Errorf("create %s: %w", tmp, err)
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("copy: %w", err)
	}
	if err := out.Sync(); err != nil {
		out.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := out.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Chmod(tmp, perm); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, dst)
}

// SameFile reports whether src and dst are the same on-disk file (os.SameFile on
// their FileInfo). A missing dst is not the same file.
func (OSFS) SameFile(src, dst string) (bool, error) {
	si, err := os.Stat(src)
	if err != nil {
		return false, err
	}
	di, err := os.Stat(dst)
	if err != nil {
		return false, nil //nolint:nilerr // missing/unreadable dst => not the same file
	}
	return os.SameFile(si, di), nil
}
