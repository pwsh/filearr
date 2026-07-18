package scan

import (
	"path"
	"strings"
)

// sidecarInfo is the result of classifying a path as a sidecar. Mirrors
// backend/filearr/sidecar.py:SidecarInfo.
//
//	Kind        — "nfo" | "jriver" | "artwork" | "xmp"
//	ParentStem  — stem of the sibling media file this sidecar belongs to, or ""
//	              (hasParent=false) for directory-level artwork
//	Directory   — the sidecar's containing directory (posix rel form as given)
type sidecarInfo struct {
	Kind       string
	ParentStem string
	HasParent  bool
	Directory  string
}

// dirArtworkNames are directory-level artwork filenames (decorate the containing
// folder's primary item, not a specific stem). Compared case-insensitively
// against the full filename. Ported verbatim from sidecar.py:_DIR_ARTWORK_NAMES.
var dirArtworkNames = map[string]bool{
	"poster.jpg": true, "poster.png": true, "poster.jpeg": true,
	"folder.jpg": true, "folder.png": true, "folder.jpeg": true,
	"cover.jpg": true, "cover.png": true, "cover.jpeg": true,
	"fanart.jpg": true, "fanart.png": true, "fanart.jpeg": true,
	"banner.jpg": true, "banner.png": true, "banner.jpeg": true,
	"clearart.jpg": true, "clearart.png": true,
	"clearlogo.jpg": true, "clearlogo.png": true,
	"landscape.jpg": true, "landscape.png": true,
	"thumb.jpg": true, "thumb.png": true,
	"logo.jpg": true, "logo.png": true,
	"disc.jpg": true, "disc.png": true,
	"season-all-poster.jpg": true, "season-all-banner.jpg": true,
}

// stemSidecarExts maps an always-per-item sidecar extension to its emitted kind.
// .xmp — Adobe/XMP metadata; .thm — camera thumbnail. From _STEM_SIDECAR_EXTS.
var stemSidecarExts = map[string]string{".xmp": "xmp", ".thm": "artwork"}

// stemSuffixes mark a per-item sidecar (e.g. "Movie (2020)-thumb.jpg"). Lower.
var stemSuffixes = []string{
	"-thumb", "-poster", "-fanart", "-banner", "-landscape",
	"-clearart", "-clearlogo", "-disc", "-logo",
}

// artExts are image extensions eligible to be a per-stem/directory artwork
// sidecar. From _ART_EXTS.
var artExts = map[string]bool{".jpg": true, ".jpeg": true, ".png": true, ".webp": true, ".tbn": true}

// splitSidecar returns (directory, filename, stem, extLower) for a rel path,
// mirroring sidecar._split (os.path.dirname/basename/splitext).
func splitSidecar(relPath string) (dir, filename, stem, ext string) {
	dir = path.Dir(relPath)
	if dir == "." {
		dir = "" // match os.path.dirname("file") == ""
	}
	filename = path.Base(relPath)
	ext = strings.ToLower(pathExt(filename))
	stem = filename[:len(filename)-len(pathExt(filename))]
	return dir, filename, stem, ext
}

// pathExt returns the extension including the dot (or "") using os.path.splitext
// semantics: a leading-dot-only name (".xmp") has ext "" and stem ".xmp".
func pathExt(name string) string {
	// os.path.splitext: the extension is the last '.'-suffix, but a name that
	// is all leading dots (".", "..", ".xmp") yields no extension.
	dot := strings.LastIndex(name, ".")
	if dot <= 0 {
		return ""
	}
	// A run of leading dots is part of the stem, never the extension:
	// splitext(".bashrc") == (".bashrc", "").
	allDots := true
	for i := 0; i < dot; i++ {
		if name[i] != '.' {
			allDots = false
			break
		}
	}
	if allDots {
		return ""
	}
	return name[dot:]
}

// classify classifies a relative path. Returns nil if it is NOT a sidecar.
// Detection is purely lexical (cheap, rescan-idempotent). Ported from
// sidecar.classify.
func classify(relPath string) *sidecarInfo {
	dir, filename, stem, ext := splitSidecar(relPath)
	fnameLower := strings.ToLower(filename)
	stemLower := strings.ToLower(stem)

	// 1. JRiver sidecar: "<anything>_JRSidecar.xml".
	if strings.HasSuffix(fnameLower, "_jrsidecar.xml") {
		base := ""
		hasParent := false
		if strings.HasSuffix(stemLower, "_jrsidecar") {
			base = stem[:len(stem)-len("_JRSidecar")]
		}
		if base != "" {
			hasParent = true
		}
		return &sidecarInfo{Kind: "jriver", ParentStem: base, HasParent: hasParent, Directory: dir}
	}

	// 2. .nfo — Kodi/Emby metadata. A bare movie/tvshow/season/album/artist.nfo
	//    is directory-level; "<stem>.nfo" is per-item.
	if ext == ".nfo" {
		switch stemLower {
		case "movie", "tvshow", "season", "album", "artist":
			return &sidecarInfo{Kind: "nfo", HasParent: false, Directory: dir}
		}
		return &sidecarInfo{Kind: "nfo", ParentStem: stem, HasParent: true, Directory: dir}
	}

	// 2b. Same-stem-only metadata/thumbnail sidecars (.xmp / .thm). A non-empty
	//     stem is required (a bare ".xmp" dotfile has ext="" and never lands here).
	if kind, ok := stemSidecarExts[ext]; ok && stem != "" {
		return &sidecarInfo{Kind: kind, ParentStem: stem, HasParent: true, Directory: dir}
	}

	// 3. Artwork images.
	if artExts[ext] {
		// 3a. Directory-level artwork by conventional filename.
		if dirArtworkNames[fnameLower] {
			return &sidecarInfo{Kind: "artwork", HasParent: false, Directory: dir}
		}
		// 3b. Per-stem artwork: "<parent-stem><suffix>.<ext>".
		for _, suffix := range stemSuffixes {
			if strings.HasSuffix(stemLower, suffix) {
				base := stem[:len(stem)-len(suffix)]
				if base != "" {
					return &sidecarInfo{Kind: "artwork", ParentStem: base, HasParent: true, Directory: dir}
				}
				// e.g. "-poster.jpg" with empty base → directory artwork.
				return &sidecarInfo{Kind: "artwork", HasParent: false, Directory: dir}
			}
		}
		// 3c. Season posters.
		if strings.HasPrefix(stemLower, "season") && (strings.Contains(stemLower, "poster") || strings.Contains(stemLower, "banner")) {
			return &sidecarInfo{Kind: "artwork", HasParent: false, Directory: dir}
		}
	}

	return nil
}

// isSidecar reports whether relPath classifies as a sidecar.
func isSidecar(relPath string) bool { return classify(relPath) != nil }

// IsArtworkSidecar reports whether relPath is an artwork sidecar (poster/-thumb/
// cover/fanart/…). Exported for the P12-T13 thumbnail pass: central's
// source-resolution Rule 0 prefers a linked artwork sidecar over a decoded frame,
// and the pass injects this classifier to mirror that precedence (the index
// package cannot import scan — an import cycle — so the classifier is passed in).
func IsArtworkSidecar(relPath string) bool {
	info := classify(relPath)
	return info != nil && info.Kind == "artwork"
}
