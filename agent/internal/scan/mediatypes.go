package scan

import (
	"path/filepath"
	"strings"
)

// MediaType mirrors backend/filearr/models.py:MediaType string values. The agent
// stores it for walk-time enabled-types gating only (no kind column semantics);
// unknown extensions resolve to MediaOther.
type MediaType string

const (
	MediaVideo       MediaType = "video"
	MediaAudio       MediaType = "audio"
	MediaAudiobook   MediaType = "audiobook"
	MediaSample      MediaType = "sample"
	MediaImage       MediaType = "image"
	MediaModel3D     MediaType = "model3d"
	MediaDocument    MediaType = "document"
	MediaSpreadsheet MediaType = "spreadsheet"
	MediaOther       MediaType = "other"
)

// extMap ports backend/filearr/media_types.py:EXT_MAP verbatim (bare extension,
// lower-case, no leading dot). Archive formats deliberately have no dedicated
// type (fall through to MediaOther); cbz/cbr map to document, matching central.
var extMap = func() map[string]MediaType {
	m := map[string]MediaType{}
	reg := func(t MediaType, exts ...string) {
		for _, e := range exts {
			m[e] = t
		}
	}
	reg(MediaVideo, "mkv", "mp4", "avi", "mov", "wmv", "ts", "m2ts", "mts", "webm", "mpg", "mpeg", "flv", "3gp", "3g2")
	reg(MediaAudio, "mp3", "flac", "ogg", "opus", "m4a", "wma", "ape", "wv", "alac", "dsf")
	reg(MediaAudiobook, "m4b", "aax")
	reg(MediaSample, "wav", "aif", "aiff", "rex", "rx2", "sf2", "sfz")
	reg(MediaImage, "jpg", "jpeg", "png", "gif", "webp", "tif", "tiff", "bmp", "heic", "avif",
		"cr2", "cr3", "nef", "arw", "dng", "raf", "svg", "psd")
	reg(MediaModel3D, "stl", "obj", "3mf", "step", "stp", "fbx", "gltf", "glb", "blend")
	reg(MediaDocument, "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "epub", "mobi", "azw3", "cbz", "cbr")
	reg(MediaSpreadsheet, "xls", "xlsx", "ods", "csv", "tsv", "numbers")
	return m
}()

// detectMediaType maps a path's extension to a MediaType, mirroring
// media_types.detect: PurePath(path).suffix, lower-cased, no dot.
func detectMediaType(path string) MediaType {
	ext := strings.ToLower(strings.TrimPrefix(filepath.Ext(path), "."))
	if t, ok := extMap[ext]; ok {
		return t
	}
	return MediaOther
}

// fileExtension returns the bare lower-case extension (no dot) for storage in
// Item.extension, or "" when absent. Mirrors os.path.splitext(...).lstrip(".").
func fileExtension(name string) string {
	return strings.ToLower(strings.TrimPrefix(filepath.Ext(name), "."))
}
