"""Extension -> MediaType mapping (user-overridable via settings later)."""

from pathlib import PurePath

from filearr.models import MediaType

EXT_MAP: dict[str, MediaType] = {}


def _register(media_type: MediaType, *exts: str) -> None:
    for e in exts:
        EXT_MAP[e] = media_type


_register(MediaType.video, "mkv", "mp4", "avi", "mov", "wmv", "ts", "m2ts", "mts", "webm", "mpg", "mpeg", "flv",
          "3gp", "3g2")
_register(MediaType.audio, "mp3", "flac", "ogg", "opus", "m4a", "wma", "ape", "wv", "alac", "dsf")
_register(MediaType.audiobook, "m4b", "aax")
_register(MediaType.sample, "wav", "aif", "aiff", "rex", "rx2", "sf2", "sfz")
_register(MediaType.image, "jpg", "jpeg", "png", "gif", "webp", "tif", "tiff", "bmp", "heic", "avif",
          "cr2", "cr3", "nef", "arw", "dng", "raf", "svg", "psd")
_register(MediaType.model3d, "stl", "obj", "3mf", "step", "stp", "fbx", "gltf", "glb", "blend")
_register(MediaType.document, "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "epub", "mobi", "azw3", "cbz", "cbr")
_register(MediaType.spreadsheet, "xls", "xlsx", "ods", "csv", "tsv", "numbers")


# P3-T13 note: archive formats deliberately have NO dedicated MediaType. zip/jar/
# tar/tgz/tar.gz/tar.bz2/tar.xz fall through to ``MediaType.other`` and cbz maps to
# ``document`` (above) -- adding an ``archive`` enum member would be a large,
# invariant-touching enum migration for no payoff. Archive MEMBER LISTING keys off
# the EXTENSION instead (``filearr.tasks.archives.detect_archive``), run as a
# separate extension-gated pass in the extract worker regardless of the file's
# resolved MediaType bucket.


def detect(path: str) -> MediaType:
    ext = PurePath(path).suffix.lstrip(".").lower()
    return EXT_MAP.get(ext, MediaType.other)
