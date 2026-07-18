package main

import (
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"io"
	"os"
)

// newFlagSet returns a flag set that reports parse errors to stderr and does not
// os.Exit on -h (the caller controls the exit code).
func newFlagSet(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	return fs
}

// sha256File streams a file and returns its lowercase-hex sha256 + byte size.
func sha256File(path string) (sum string, size int64, err error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}
