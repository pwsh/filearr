package thumbs

import (
	"bytes"
	"encoding/binary"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"os"
	"path/filepath"
	"testing"
)

// writePNG renders a w×h gradient PNG to a temp file and returns its path.
func writePNG(t *testing.T, w, h int) string {
	t.Helper()
	return writePNGAt(t, t.TempDir(), "src.png", w, h)
}

// writePNGAt renders a w×h gradient PNG to dir/name (the name's extension is
// irrelevant — decode is by content) and returns its path.
func writePNGAt(t *testing.T, dir, name string, w, h int) string {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			img.Set(x, y, color.RGBA{uint8(x % 256), uint8(y % 256), uint8((x + y) % 256), 255})
		}
	}
	path := filepath.Join(dir, name)
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := png.Encode(f, img); err != nil {
		t.Fatal(err)
	}
	return path
}

func isJPEG(b []byte) bool { return len(b) >= 3 && b[0] == 0xFF && b[1] == 0xD8 && b[2] == 0xFF }

func TestGenerateImageThumb_GridDownscale(t *testing.T) {
	src := writePNG(t, 1000, 500) // longest edge 1000 -> grid 320
	tb := GenerateImageThumb(src, GridSpec)
	if tb == nil {
		t.Fatal("expected a thumbnail")
	}
	if !isJPEG(tb.Data) {
		t.Fatalf("expected JPEG magic, got %x", tb.Data[:3])
	}
	if tb.Width != 320 || tb.Height != 160 {
		t.Fatalf("dims = %dx%d, want 320x160 (aspect preserved, longest edge=320)", tb.Width, tb.Height)
	}
	if len(tb.Data) > GridSpec.MaxBytes {
		t.Fatalf("grid thumb %d bytes exceeds cap %d", len(tb.Data), GridSpec.MaxBytes)
	}
	if tb.Format != "jpeg" {
		t.Fatalf("format = %q, want jpeg", tb.Format)
	}
}

func TestGenerateImageThumb_NoUpscale(t *testing.T) {
	src := writePNG(t, 100, 60) // already smaller than 320 -> unchanged dims
	tb := GenerateImageThumb(src, GridSpec)
	if tb == nil {
		t.Fatal("expected a thumbnail")
	}
	if tb.Width != 100 || tb.Height != 60 {
		t.Fatalf("dims = %dx%d, want 100x60 (never upscaled)", tb.Width, tb.Height)
	}
}

func TestGenerateImageThumb_PreviewTier(t *testing.T) {
	src := writePNG(t, 2000, 1000)
	tb := GenerateImageThumb(src, PreviewSpec)
	if tb == nil {
		t.Fatal("expected a thumbnail")
	}
	if tb.Width != 800 || tb.Height != 400 {
		t.Fatalf("dims = %dx%d, want 800x400", tb.Width, tb.Height)
	}
	if len(tb.Data) > PreviewSpec.MaxBytes {
		t.Fatalf("preview thumb %d bytes exceeds cap %d", len(tb.Data), PreviewSpec.MaxBytes)
	}
}

func TestGenerateImageThumb_ByteCapExhausted(t *testing.T) {
	src := writePNG(t, 320, 320)
	// An absurdly tiny cap that even the quality floor cannot meet -> store nothing.
	tight := TierSpec{Tier: TierGrid, MaxEdge: 320, Quality: 70, MaxBytes: 10}
	if tb := GenerateImageThumb(src, tight); tb != nil {
		t.Fatalf("expected nil when even the quality floor overshoots the cap, got %d bytes", len(tb.Data))
	}
}

func TestGenerateThumbFromBytes_Garbage(t *testing.T) {
	if tb := GenerateThumbFromBytes([]byte("not an image at all"), GridSpec); tb != nil {
		t.Fatal("garbage bytes must yield no thumbnail")
	}
	if tb := GenerateThumbFromBytes(nil, GridSpec); tb != nil {
		t.Fatal("nil bytes must yield no thumbnail")
	}
}

func TestGenerateImageThumb_MissingFile(t *testing.T) {
	if tb := GenerateImageThumb(filepath.Join(t.TempDir(), "nope.png"), GridSpec); tb != nil {
		t.Fatal("a missing file must yield no thumbnail (soft-fail)")
	}
}

// TestDecodeGuardRejectsBomb crafts a PNG whose IHDR declares a pixel count over
// MaxPixels; DecodeConfig reads those dims WITHOUT decoding the (absent) pixels,
// so the guard rejects it before any allocation.
func TestDecodeGuardRejectsBomb(t *testing.T) {
	png := bombPNGHeader(60000, 60000) // 3.6e9 px >> 50 MP ceiling
	if img := decodeGuarded(bytes.NewReader(png)); img != nil {
		t.Fatal("decodeGuarded must reject an over-ceiling source before full decode")
	}
}

// bombPNGHeader builds the 8-byte PNG signature + a valid IHDR chunk declaring
// w×h (no image data — DecodeConfig only needs the IHDR).
func bombPNGHeader(w, h uint32) []byte {
	var buf bytes.Buffer
	buf.Write([]byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'})
	var ihdr bytes.Buffer
	binary.Write(&ihdr, binary.BigEndian, w)
	binary.Write(&ihdr, binary.BigEndian, h)
	ihdr.Write([]byte{8, 2, 0, 0, 0}) // bit depth 8, color type 2 (RGB), no interlace
	// length + "IHDR" + data + CRC(0 — DecodeConfig does not verify the CRC).
	binary.Write(&buf, binary.BigEndian, uint32(ihdr.Len()))
	buf.WriteString("IHDR")
	buf.Write(ihdr.Bytes())
	buf.Write([]byte{0, 0, 0, 0})
	return buf.Bytes()
}

func smallJPEG(t *testing.T) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, 4, 4))
	for i := range img.Pix {
		img.Pix[i] = 200
	}
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, img, &jpeg.Options{Quality: 80}); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// TestExtractAudioCover_ID3APIC builds a minimal ID3v2.3 tag with an APIC picture
// frame and asserts ExtractAudioCover returns the embedded JPEG bytes (the same
// pipeline audio covers feed into GenerateThumbFromBytes).
func TestExtractAudioCover_ID3APIC(t *testing.T) {
	pic := smallJPEG(t)
	tagged := buildID3APIC(pic)
	path := filepath.Join(t.TempDir(), "song.mp3")
	if err := os.WriteFile(path, tagged, 0o644); err != nil {
		t.Fatal(err)
	}
	got := ExtractAudioCover(path)
	if !bytes.Equal(got, pic) {
		t.Fatalf("ExtractAudioCover returned %d bytes, want the %d-byte embedded JPEG", len(got), len(pic))
	}

	// A non-audio / hashless file yields nil, never a panic.
	if ExtractAudioCover(filepath.Join(t.TempDir(), "absent.mp3")) != nil {
		t.Fatal("missing file must yield nil cover")
	}
}

// buildID3APIC constructs an ID3v2.3 tag carrying one APIC frame with a JPEG
// picture, followed by a byte of fake audio. dhowden/tag parses ID3v2.3.
func buildID3APIC(pic []byte) []byte {
	// APIC frame body: text-encoding(0=latin1) + MIME + 0 + pic-type(3=front cover)
	// + description(empty + terminator 0) + picture data.
	var body bytes.Buffer
	body.WriteByte(0x00)
	body.WriteString("image/jpeg")
	body.WriteByte(0x00)
	body.WriteByte(0x03)
	body.WriteByte(0x00) // empty description terminator
	body.Write(pic)

	var frame bytes.Buffer
	frame.WriteString("APIC")
	binary.Write(&frame, binary.BigEndian, uint32(body.Len())) // v2.3: plain big-endian size
	frame.Write([]byte{0x00, 0x00})                            // frame flags
	frame.Write(body.Bytes())

	var out bytes.Buffer
	out.WriteString("ID3")
	out.Write([]byte{0x03, 0x00, 0x00}) // v2.3.0, no flags
	out.Write(synchsafe(uint32(frame.Len())))
	out.Write(frame.Bytes())
	out.WriteByte(0xFF) // a trailing fake-audio byte
	return out.Bytes()
}

// synchsafe encodes n as a 28-bit synchsafe integer (7 bits per byte), the ID3v2
// tag-size format.
func synchsafe(n uint32) []byte {
	return []byte{
		byte((n >> 21) & 0x7f),
		byte((n >> 14) & 0x7f),
		byte((n >> 7) & 0x7f),
		byte(n & 0x7f),
	}
}
