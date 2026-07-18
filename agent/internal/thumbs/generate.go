package thumbs

import (
	"bytes"
	"context"
	"image"
	"image/color"
	"image/jpeg"
	"io"
	"os"
	"os/exec"
	"strconv"
	"time"

	// stdlib raster decoders (side-effect registration for image.Decode).
	_ "image/gif"
	_ "image/png"

	"github.com/dhowden/tag"
	xdraw "golang.org/x/image/draw"

	// pure-Go extra decoders (side-effect registration).
	_ "golang.org/x/image/bmp"
	_ "golang.org/x/image/tiff"
	_ "golang.org/x/image/webp"
)

// Generation constants mirror backend/filearr/config.py thumbnail defaults. Note
// a DRIFT in edge/quality/byte-cap from central would only change a thumbnail's
// dimensions/size — never its cache KEY (the key uses only hash:gen:tier), so an
// agent thumbnail always addresses the same slot central would. QualityFloor/Step
// walk the encoder quality DOWN to fit the per-tier byte cap (the Nextcloud
// derivative-bloat guard); below the floor we store nothing.
const (
	QualityFloor = 40
	QualityStep  = 10
	// MaxPixels is the source-decode decompression-bomb ceiling (mirrors central
	// thumbnail_max_pixels, ~50 MP): a source whose declared pixel count exceeds
	// this is rejected BEFORE full decode.
	MaxPixels = 50_000_000
	// VideoMinSeekSeconds is the poster-frame seek target when the agent has no
	// duration for the file (it does not run ffprobe): skip a black intro without
	// assuming a percentage (mirrors central's min-seek fallback).
	VideoMinSeekSeconds = 3.0
	// VideoFFmpegTimeout bounds one frame-grab (mirrors central's ffmpeg timeout).
	VideoFFmpegTimeout = 60 * time.Second
	// videoMaxFrameBytes caps the PNG ffmpeg pipes to stdout (defense against a
	// pathological decode); a larger frame collapses to "no thumbnail".
	videoMaxFrameBytes = 32 << 20
)

// TierSpec is the resolved parameters for one thumbnail tier. Defaults mirror
// central's grid/preview tiers.
type TierSpec struct {
	Tier     int
	MaxEdge  int
	Quality  int
	MaxBytes int
}

// GridSpec / PreviewSpec mirror central config.py (thumbnail_grid_* /
// thumbnail_preview_*). The agent pregenerates BOTH tiers because central cannot
// lazily generate a preview for an agent-hosted item (it has no access to the
// source file) — unlike a central-hosted item whose preview is generated on the
// first serve-path miss.
var (
	GridSpec    = TierSpec{Tier: TierGrid, MaxEdge: 320, Quality: 70, MaxBytes: 20_000}
	PreviewSpec = TierSpec{Tier: TierPreview, MaxEdge: 800, Quality: 78, MaxBytes: 60_000}
)

// SpecForTier returns the TierSpec for a tier constant (defaults to grid).
func SpecForTier(tier int) TierSpec {
	if tier == TierPreview {
		return PreviewSpec
	}
	return GridSpec
}

// ThumbBytes is a successfully encoded thumbnail. Format is always "jpeg" for the
// agent (see the package doc's encode-format deviation).
type ThumbBytes struct {
	Data   []byte
	Width  int
	Height int
	Format string
}

// GenerateImageThumb decodes an image file (guarded) and encodes a tier
// thumbnail. Returns nil for ANY failure (undecodable, oversized source, over-cap
// even at the quality floor, missing file) — hostile-file discipline: a bad file
// never produces an error, only "no thumbnail", exactly like central's
// generate_image_thumb returning None.
func GenerateImageThumb(path string, spec TierSpec) *ThumbBytes {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	img := decodeGuarded(f)
	if img == nil {
		return nil
	}
	return encodeCapped(img, spec)
}

// GenerateThumbFromBytes is GenerateImageThumb from in-memory image bytes
// (embedded cover art, a piped video frame).
func GenerateThumbFromBytes(raw []byte, spec TierSpec) *ThumbBytes {
	img := decodeGuarded(bytes.NewReader(raw))
	if img == nil {
		return nil
	}
	return encodeCapped(img, spec)
}

// ExtractAudioCover returns the first embedded cover-art bytes from an audio file
// via the pure-Go github.com/dhowden/tag reader (ID3 APIC, MP4 covr, FLAC/Ogg
// picture). Returns nil when there is no picture or the file is unreadable. Never
// panics on a hostile file (tag.ReadFrom returns an error, which we swallow).
func ExtractAudioCover(path string) []byte {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	m, err := tag.ReadFrom(f)
	if err != nil {
		return nil
	}
	pic := m.Picture()
	if pic == nil || len(pic.Data) == 0 {
		return nil
	}
	return pic.Data
}

// GenerateVideoThumb grabs a single poster frame via an ffmpeg EXEC (capability-
// gated by the caller — an absent ffmpeg is skipped, not an error), following
// central's ffprobe/ffmpeg posture exactly: argv list (no shell), -ss BEFORE -i
// (fast input seek), one frame decoded + scaled to the tier edge inside ffmpeg,
// output PNG piped to stdout, hard timeout, output byte cap, untrusted path
// isolated as the value of -i with "--" before the "-" stdout output. The PNG is
// then handed to the SAME JPEG quality-ladder every other source uses. Returns
// nil on ANY failure.
func GenerateVideoThumb(ctx context.Context, ffmpegPath, path string, spec TierSpec, seekSeconds float64) *ThumbBytes {
	if ffmpegPath == "" || spec.MaxEdge <= 0 {
		return nil
	}
	if seekSeconds < 0 {
		seekSeconds = 0
	}
	png := runFrameGrab(ctx, ffmpegPath, path, seekSeconds, spec.MaxEdge)
	if png == nil {
		return nil
	}
	return GenerateThumbFromBytes(png, spec)
}

func runFrameGrab(ctx context.Context, ffmpegPath, srcPath string, seek float64, maxEdge int) []byte {
	cctx, cancel := context.WithTimeout(ctx, VideoFFmpegTimeout)
	defer cancel()
	scale := scaleFilter(maxEdge)
	argv := []string{
		"-hide_banner", "-loglevel", "error", "-nostdin",
		"-ss", formatSeconds(seek),
		"-i", srcPath,
		"-map", "0:v:0", // first video stream only (skip attached art)
		"-frames:v", "1",
		"-vf", scale,
		"-f", "image2pipe",
		"-vcodec", "png",
		"--",
		"-", // write the PNG to stdout
	}
	cmd := exec.CommandContext(cctx, ffmpegPath, argv...)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		return nil
	}
	if out.Len() == 0 || out.Len() > videoMaxFrameBytes {
		return nil
	}
	return out.Bytes()
}

// scaleFilter mirrors central _scale_filter: fit within maxEdge x maxEdge
// preserving aspect ratio, never upscaling.
func scaleFilter(maxEdge int) string {
	e := strconv.Itoa(maxEdge)
	return "scale='min(" + e + ",iw)':'min(" + e + ",ih)':force_original_aspect_ratio=decrease"
}

// --- internal decode / encode ---------------------------------------------- //

// decodeGuarded reads the header first to reject a decompression bomb before a
// full decode (mirrors central _open_guarded's header-size check), then decodes.
func decodeGuarded(r io.ReadSeeker) image.Image {
	cfg, _, err := image.DecodeConfig(r)
	if err != nil {
		return nil
	}
	if cfg.Width <= 0 || cfg.Height <= 0 {
		return nil
	}
	if int64(cfg.Width)*int64(cfg.Height) > MaxPixels {
		return nil
	}
	if _, err := r.Seek(0, io.SeekStart); err != nil {
		return nil
	}
	img, _, err := image.Decode(r)
	if err != nil {
		return nil
	}
	return img
}

// encodeCapped downscales img to spec.MaxEdge longest edge (never upscaling) then
// steps JPEG quality down until the encoded size fits spec.MaxBytes; below the
// floor it stores nothing (mirrors central _encode_webp_capped, swapping WebP for
// JPEG per the package deviation).
func encodeCapped(img image.Image, spec TierSpec) *ThumbBytes {
	scaled := downscale(img, spec.MaxEdge)
	b := scaled.Bounds()
	q := spec.Quality
	for q >= QualityFloor {
		var buf bytes.Buffer
		if err := jpeg.Encode(&buf, scaled, &jpeg.Options{Quality: q}); err != nil {
			return nil
		}
		if buf.Len() <= spec.MaxBytes {
			// Copy out of the reusable buffer so callers own the bytes.
			data := make([]byte, buf.Len())
			copy(data, buf.Bytes())
			return &ThumbBytes{Data: data, Width: b.Dx(), Height: b.Dy(), Format: "jpeg"}
		}
		q -= QualityStep
	}
	return nil
}

// downscale returns img scaled so its longest edge is at most maxEdge, preserving
// aspect ratio and never upscaling. Transparency is flattened over WHITE (JPEG
// has no alpha channel; a black default would make transparent posters ugly).
func downscale(img image.Image, maxEdge int) image.Image {
	b := img.Bounds()
	w, h := b.Dx(), b.Dy()
	if maxEdge <= 0 || (w <= maxEdge && h <= maxEdge) {
		return flattenWhite(img)
	}
	var tw, th int
	if w >= h {
		tw = maxEdge
		th = int(float64(h) * float64(maxEdge) / float64(w))
	} else {
		th = maxEdge
		tw = int(float64(w) * float64(maxEdge) / float64(h))
	}
	if tw < 1 {
		tw = 1
	}
	if th < 1 {
		th = 1
	}
	dst := image.NewRGBA(image.Rect(0, 0, tw, th))
	xdraw.Draw(dst, dst.Bounds(), image.NewUniform(color.White), image.Point{}, xdraw.Src)
	xdraw.CatmullRom.Scale(dst, dst.Bounds(), img, b, xdraw.Over, nil)
	return dst
}

// flattenWhite composites img over an opaque white background at native size, so
// a transparent source encodes to JPEG cleanly.
func flattenWhite(img image.Image) image.Image {
	b := img.Bounds()
	dst := image.NewRGBA(image.Rect(0, 0, b.Dx(), b.Dy()))
	xdraw.Draw(dst, dst.Bounds(), image.NewUniform(color.White), image.Point{}, xdraw.Src)
	xdraw.Draw(dst, dst.Bounds(), img, b.Min, xdraw.Over)
	return dst
}

// formatSeconds renders a seek target as central does ("%.3f").
func formatSeconds(s float64) string {
	return strconv.FormatFloat(s, 'f', 3, 64)
}
