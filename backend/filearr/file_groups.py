"""Extension → *file group* / *file category* similarity taxonomy (SEED source).

This module is the pure, session-free SEED for the File Extension Similarity
Taxonomy: a two-level tree (``file_category`` coarse parent → ``file_group`` finer
child) derived from the file *extension*. W8-B made this taxonomy AUTHORITATIVE —
it removed the old ``MediaType`` enum entirely; ``file_category`` is now the coarse
type bucket and ``file_group`` its finer subdivision. The DB-backed, editable
runtime service (:mod:`filearr.taxonomy`) reads operator-edited tables SEEDED from
this data; these pure functions are the boot/empty-DB fallback AND the search
projection's classifier (``search.build_doc`` has no DB session).

``file_group`` is the finer layer. It:

* is a pure function of the item's *extension* (case-insensitive; a recognised
  compound ending like ``.tar.gz`` consulted first);
* both SUBDIVIDES a coarse category (RAW vs raster photos; lossy vs lossless audio)
  and gives signal to otherwise-opaque files (archives, installers, source code,
  fonts, configs, subtitles) that all shared the old ``other`` bucket;
* is a low-cardinality controlled vocabulary → a Meili ``filterableAttributes`` +
  facet-searchable field, accepted as a repeatable ``file_group`` filter on
  ``/search`` (``file_category`` likewise).

Distinct from ``presets.EXTENSION_GROUPS`` (P2-T3): that is a small, curated set of
per-library *scan-inclusion* toggles (union semantics at scan time). This module is
a COMPLETE, catalog-wide *classification* taxonomy consumed at index/search time.

Multi-part extensions
---------------------
``detect_group`` consults a recognized COMPOUND ending first (``.tar.gz``,
``.tar.bz2``, ``.tar.xz``, ``.tar.zst``, …) and classifies the whole file as
``archive``; otherwise only the final extension is used (mirroring
``media_types.detect``). Because the individual wrapper extensions (``gz``/``xz``/
``zst``/…) also map to ``archive`` on their own, the compound rule mainly guarantees
consistent, intention-revealing classification. Leading-dot names with no stem
(``.bashrc``, ``.gitignore``) have no extension and resolve to ``other``.

Extension collisions
--------------------
An extension can belong to at most ONE group (``EXT_GROUP_MAP`` is a plain dict),
so a genuinely ambiguous suffix is assigned to its more common catalog meaning and
the decision recorded in the affected groups' ``notes``. Examples: ``obj`` →
``3d-model`` (Wavefront) not a compiled object; ``stl`` → ``3d-model``
(stereolithography) not EBU subtitle; ``bin`` → ``executable-binary`` not a disc
image; ``mdf`` → ``database`` (SQL Server) not a disc image; ``cdr`` →
``vector-image`` (CorelDRAW) not a macOS disc image; ``ptx`` → ``audio-project``
(Pro Tools) not Pentax RAW; ``key`` → ``certificate-key`` (private key) not Keynote;
``sql`` → ``database`` not source; ``xml`` → ``markup`` not config; ``asc`` →
``certificate-key`` (PGP-armored) not plain text; ``egg`` → ``package-installer``
(Python) not the ALZip archive. ``detect_group`` classifies by extension ALONE, so
a few splits are impossible by extension (an animated vs still ``.gif``/``.webp``;
a generic ``.dat``/``.bin``) — those follow the documented default.

Research / sources
------------------
Extension membership was assembled from general domain knowledge and cross-checked
against widely used authoritative catalogs. Any host names in examples use the
reserved documentation domain ``example.com``.

* Wikipedia, "List of file formats" — https://en.wikipedia.org/wiki/List_of_file_formats
* Wikipedia, "Raw image format" (per-manufacturer RAW) —
  https://en.wikipedia.org/wiki/Raw_image_format
* Wikipedia, "List of archive formats" (compound ``tar.*`` handling) —
  https://en.wikipedia.org/wiki/List_of_archive_formats
* IANA Media Types — https://www.iana.org/assignments/media-types/media-types.xhtml
* Library of Congress, Sustainability of Digital Formats —
  https://www.loc.gov/preservation/digital/formats/
* FileInfo file-extension database — https://fileinfo.com/

Purity: no I/O, no ORM, no network — every function here is a pure function of its
arguments and unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

#: The catch-all group id for an unknown / unmapped extension. ``detect_group``
#: returns this when no rule matches.
GROUP_OTHER = "other"


@dataclass(frozen=True)
class FileGroup:
    """One similarity bucket — the finer child of a :class:`FileCategory`.

    Its coarse parent is the ``file_category`` from :data:`_GROUP_CATEGORY`
    (queryable via :func:`category_for_group`); the group itself carries no
    separate nominal parent (the removed ``MediaType`` rollup)."""

    id: str
    label: str
    description: str
    notes: str | None = None


# --------------------------------------------------------------------------- #
# The taxonomy. ``_GROUP_EXTENSIONS`` is the AUTHORING source of truth (group →  #
# extensions); ``EXT_GROUP_MAP`` is derived by inversion so the two can never    #
# drift and a duplicated extension is caught at import (see ``_invert``). Order  #
# here is the canonical order of ``FILE_GROUPS`` / the reference doc / the API.  #
# --------------------------------------------------------------------------- #

_GROUPS: tuple[FileGroup, ...] = (
    # -- Images -------------------------------------------------------------- #
    FileGroup(
        "raster-photo", "Raster / photo",
        "Pixel (bitmap) images — photographs, screenshots, web graphics and their "
        "HDR/high-bit-depth cousins. The everyday image formats.",
    ),
    FileGroup(
        "raw-photo", "Camera RAW",
        "Unprocessed camera sensor data (mostly one proprietary format per "
        "manufacturer) plus the open Adobe DNG. Needs a RAW developer, not a plain "
        "image viewer.",
    ),
    FileGroup(
        "vector-image", "Vector image",
        "Resolution-independent geometry (paths/shapes) rather than pixels — "
        "illustration, logos, diagrams.",
        notes="``cdr`` is CorelDRAW here (not a macOS disc image); ``eps`` is the "
        "vector Encapsulated PostScript (raster print PostScript ``ps`` is grouped "
        "under ``pdf``).",
    ),
    FileGroup(
        "layered-image", "Layered / authoring image",
        "Editable multi-layer image project documents from raster editors "
        "(Photoshop, GIMP, Krita, Affinity, …) — not a flattened export.",
    ),
    FileGroup(
        "animated-image", "Animated image",
        "Short looping animations delivered as an image rather than a video "
        "container.",
        notes="``gif`` is classified here (its canonical animated use) rather than "
        "under raster-photo; APNG/animated-WebP share the ``png``/``webp`` "
        "extensions and so cannot be split out by extension alone.",
    ),
    # -- Audio / video ------------------------------------------------------- #
    FileGroup(
        "video", "Video",
        "Moving-image containers and streams (movies, clips, recordings).",
        notes="``ts``/``mts`` are MPEG transport streams here, not TypeScript — "
        "``tsx``/``cts`` still classify as source code.",
    ),
    FileGroup(
        "audio-lossy", "Lossy audio",
        "Perceptually compressed audio (MP3/AAC/Ogg/Opus/…) — smaller files, "
        "irreversible quality loss.",
    ),
    FileGroup(
        "audio-lossless", "Lossless / PCM audio",
        "Losslessly compressed or uncompressed PCM/DSD audio (FLAC/ALAC/WAV/AIFF/"
        "APE/…) — bit-exact reconstruction.",
        notes="Raw PCM sample files (``wav``/``aiff``) group here as lossless "
        "audio (file_category ``audio``), alongside their sampler cousins in "
        "``audio-project``.",
    ),
    FileGroup(
        "audiobook", "Audiobook",
        "Chapterised spoken-word audiobook containers (M4B and Audible formats).",
    ),
    FileGroup(
        "audio-project", "Audio project, sampler & instrument",
        "Digital-audio-workstation project/session files and sampler / synth "
        "instrument, patch, sound-font and loop formats — production assets, not "
        "finished audio.",
        notes="``ptx`` is a Pro Tools session here (not Pentax RAW).",
    ),
    FileGroup(
        "playlist", "Playlist",
        "Ordered references to media tracks/clips (and cue sheets) — the playlist "
        "itself carries no audio/video payload.",
    ),
    FileGroup(
        "subtitle", "Subtitle / caption",
        "Timed-text subtitle, caption and synced-lyric sidecar formats.",
        notes="``stl`` is claimed by ``3d-model`` (stereolithography); the EBU STL "
        "subtitle format is therefore not represented by that extension here.",
    ),
    # -- Documents ----------------------------------------------------------- #
    FileGroup(
        "document-text", "Plain text document",
        "Human-readable plain-text documents with no rich formatting model.",
    ),
    FileGroup(
        "document-office", "Word-processor / office document",
        "Rich word-processor documents (Word/OpenDocument/Pages/WordPerfect/…) with "
        "styles, layout and embedded objects.",
    ),
    FileGroup(
        "pdf", "PDF & page description",
        "Fixed-layout page-description documents — PDF and the PostScript / XPS "
        "print family.",
    ),
    FileGroup(
        "presentation", "Presentation",
        "Slide decks (PowerPoint / Keynote / Impress / Google Slides).",
        notes="Slide decks group here under file_category ``document``.",
    ),
    FileGroup(
        "spreadsheet", "Spreadsheet / tabular",
        "Spreadsheet workbooks and delimited tabular data (CSV/TSV).",
    ),
    FileGroup(
        "ebook", "E-book",
        "Reflowable and fixed e-book formats (EPUB, Kindle, FictionBook, DjVu, …).",
    ),
    FileGroup(
        "comic", "Comic archive",
        "Comic-book archives (a page-image bundle in a ZIP/RAR/7z/tar wrapper).",
    ),
    FileGroup(
        "markup", "Markup & typesetting source",
        "Human-authored markup, template and typesetting SOURCE — Markdown, HTML/"
        "XML, reStructuredText, AsciiDoc, LaTeX, and friends.",
        notes="``xml`` is grouped here as a markup language rather than under "
        "``config-data``.",
    ),
    # -- 3D / CAD ------------------------------------------------------------ #
    FileGroup(
        "3d-model", "3D model / mesh",
        "3D meshes, scenes and printable models (STL/OBJ/glTF/FBX/3MF/…).",
        notes="``obj`` is the Wavefront 3D mesh here (not a compiled object file); "
        "``stl`` is stereolithography (not EBU subtitle).",
    ),
    FileGroup(
        "cad", "CAD & engineering",
        "Computer-aided-design drawings and engineering interchange formats "
        "(DWG/DXF/STEP/IGES/native part & assembly files).",
    ),
    # -- Fonts / dev / data / system ---------------------------------------- #
    FileGroup(
        "font", "Font",
        "Digital typefaces and font-editor sources (TrueType/OpenType/WOFF/…).",
    ),
    FileGroup(
        "source-code", "Source code",
        "Programming-language source files across the common language ecosystems.",
        notes="``m`` is grouped as source (Objective-C / MATLAB share it); ``sql`` "
        "is grouped under ``database``.",
    ),
    FileGroup(
        "script", "Shell / automation script",
        "Shell, batch and automation scripts (Bash/PowerShell/Batch/AppleScript/…).",
    ),
    FileGroup(
        "web-asset", "Web asset",
        "Front-end web build assets — stylesheets and WebAssembly.",
        notes="HTML is grouped under ``markup``; web fonts under ``font``.",
    ),
    FileGroup(
        "notebook", "Computational notebook",
        "Literate computational notebooks (Jupyter, R Markdown, Quarto).",
    ),
    FileGroup(
        "config-data", "Config & structured data",
        "Machine-readable configuration and structured-data / serialization files "
        "(JSON/YAML/TOML/INI/env/infrastructure-as-code/…).",
    ),
    FileGroup(
        "database", "Database & dataset",
        "On-disk databases, database dumps, and columnar/analytics dataset files "
        "(SQLite/Access/SQL dumps/Parquet/Avro/…).",
        notes="``mdf`` is a SQL Server data file here (not a disc image); ``nsf`` is "
        "a Lotus Notes database (not e-mail).",
    ),
    # -- Bundles / system ---------------------------------------------------- #
    FileGroup(
        "archive", "Archive / compressed",
        "General-purpose archive and compression containers (ZIP/RAR/7z/tar/gz/"
        "zst/…), including multi-part ``tar.*`` bundles and generic Java archives.",
    ),
    FileGroup(
        "disk-image", "Disk / filesystem image",
        "Whole-disc, filesystem and virtual-machine disk images (ISO/DMG/VHD/VMDK/"
        "…).",
    ),
    FileGroup(
        "package-installer", "Package / installer",
        "OS, application and language-ecosystem installable packages (deb/rpm/msi/"
        "apk/AppImage/wheel/gem/…).",
    ),
    FileGroup(
        "executable-binary", "Executable / binary",
        "Native executables, shared/static libraries, compiled object code and "
        "byte-code.",
    ),
    FileGroup(
        "email", "E-mail & mailbox",
        "Individual messages and mailbox stores (EML/MSG/mbox/PST/…).",
    ),
    FileGroup(
        "certificate-key", "Certificate & key",
        "X.509 certificates, cryptographic keys, keystores and signatures.",
        notes="``key`` is a cryptographic private key here (not an Apple Keynote "
        "deck); ``asc`` is treated as PGP-armored key/signature material.",
    ),
    FileGroup(
        "log", "Log & diagnostic",
        "Application/system logs and diagnostic event traces.",
    ),
    FileGroup(
        GROUP_OTHER, "Other / unknown",
        "No matching group — an unrecognised or absent extension. The bucket "
        "``file_group`` is designed to shrink.",
    ),
)

#: Ordered registry, ``group_id -> FileGroup`` (dict preserves the order above).
FILE_GROUPS: dict[str, FileGroup] = {g.id: g for g in _GROUPS}


# --------------------------------------------------------------------------- #
# File CATEGORY layer (W8-A) — the coarse parent the 37 ``file_group``s roll up  #
# into. This is the SEED source of truth for the DB-backed, editable "File       #
# Extension Similarity Taxonomy" (``file_categories`` / ``file_groups`` /        #
# ``file_group_extensions`` tables, seeded by the W8-A migration FROM the data    #
# below). ``file_category`` will REPLACE ``media_type`` (W8-B removes media_type  #
# and routes extraction off the category's ``extractor``); in this unit it is     #
# additive and media_type stays as a derived alias.                               #
#                                                                                 #
# SEED vs RUNTIME split (documented, load-bearing):                               #
#   * SEED (this module) — pure, sync, session-free functions (``detect_group`` / #
#     ``detect_category`` / ``category_for_group``) used by the SEARCH PROJECTION #
#     (``search.build_doc`` has no DB session) and as the boot/empty-DB FALLBACK  #
#     for the runtime service. It documents the DEFAULT taxonomy (the reference   #
#     doc renders from here).                                                     #
#   * RUNTIME (``filearr.taxonomy``) — reads the (editable) DB tables into an     #
#     in-process cache and classifies items at scan/extract/replication time      #
#     (stored ``items.file_category`` / ``items.file_group``). A live install may #
#     have edited its taxonomy away from this seed; see ``GET /api/v1/taxonomy``. #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FileCategory:
    """One coarse parent category the ``file_group``s roll up into (W8-A).

    ``extractor`` is the extraction PIPELINE the category routes to (W8-B routes on
    it): ``image`` / ``audio`` / ``video`` / ``document`` / ``model3d`` or ``None``
    (no extractor — archives, dev files, system files, other). ``sort_order`` is the
    category's index in :data:`FILE_CATEGORIES` (canonical order)."""

    key: str
    label: str
    description: str
    extractor: str | None


#: The category registry (canonical order). ~9 categories the 37 groups roll up
#: into. ``extractor`` mirrors TODAY's extraction routing (W8-B consumes it).
_CATEGORIES: tuple[FileCategory, ...] = (
    FileCategory(
        "image", "Image",
        "Still and animated raster/vector images — photos, camera RAW, illustration, "
        "layered editor projects, and short looping animations.",
        "image",
    ),
    FileCategory(
        "audio", "Audio",
        "Sound: lossy and lossless/PCM audio, audiobooks, DAW/sampler project & "
        "instrument assets, and playlists.",
        "audio",
    ),
    FileCategory(
        "video", "Video & subtitle",
        "Moving-image containers and their timed-text subtitle/caption sidecars.",
        "video",
    ),
    FileCategory(
        "document", "Document",
        "Human-readable documents: plain text, office/word-processor, PDF & page "
        "description, presentations, spreadsheets, e-books, comics, and markup source.",
        "document",
    ),
    FileCategory(
        "three-d-cad", "3D & CAD",
        "3D meshes/scenes/printable models and computer-aided-design & engineering "
        "interchange formats.",
        "model3d",
    ),
    FileCategory(
        "development", "Development & data",
        "Source code, shell/automation scripts, web build assets, computational "
        "notebooks, and machine-readable configuration / structured data.",
        None,
    ),
    FileCategory(
        "archive", "Archive & image",
        "General-purpose archives/compression, whole-disc & filesystem/VM images, and "
        "OS/application/language installable packages.",
        None,
    ),
    FileCategory(
        "system", "System & data files",
        "Native executables/binaries, on-disk databases & datasets, fonts, "
        "certificates & keys, logs/diagnostics, and e-mail/mailbox stores.",
        None,
    ),
    FileCategory(
        GROUP_OTHER, "Other / unknown",
        "No matching category — an unrecognised or absent extension. Designed to "
        "shrink as the taxonomy grows.",
        None,
    ),
)

#: Ordered category registry, ``category_key -> FileCategory``.
FILE_CATEGORIES: dict[str, FileCategory] = {c.key: c for c in _CATEGORIES}


#: The group -> category rollup (every group id from ``FILE_GROUPS`` appears once).
#: This is the parentage the W8-A migration seeds into ``file_groups.category_id``.
_GROUP_CATEGORY: dict[str, str] = {
    # image
    "raster-photo": "image", "raw-photo": "image", "vector-image": "image",
    "layered-image": "image", "animated-image": "image",
    # audio
    "audio-lossy": "audio", "audio-lossless": "audio", "audiobook": "audio",
    "audio-project": "audio", "playlist": "audio",
    # video (+ subtitle sidecars)
    "video": "video", "subtitle": "video",
    # document
    "document-text": "document", "document-office": "document", "pdf": "document",
    "presentation": "document", "spreadsheet": "document", "ebook": "document",
    "comic": "document", "markup": "document",
    # 3D / CAD
    "3d-model": "three-d-cad", "cad": "three-d-cad",
    # development & data
    "source-code": "development", "script": "development", "web-asset": "development",
    "notebook": "development", "config-data": "development",
    # archive & image
    "archive": "archive", "disk-image": "archive", "package-installer": "archive",
    # system & data files
    "executable-binary": "system", "database": "system", "font": "system",
    "certificate-key": "system", "log": "system", "email": "system",
    # catch-all
    GROUP_OTHER: "other",
}


def _validate_categories() -> None:
    """Import-time integrity: every group is assigned to a REAL category, and every
    category (except possibly ``other``) actually parents at least one group."""
    missing = set(FILE_GROUPS) - set(_GROUP_CATEGORY)
    if missing:
        raise RuntimeError(f"file_groups: group(s) with no category: {sorted(missing)}")
    unknown_cat = set(_GROUP_CATEGORY.values()) - set(FILE_CATEGORIES)
    if unknown_cat:
        raise RuntimeError(
            f"file_groups: group(s) reference unknown category: {sorted(unknown_cat)}"
        )
    unknown_group = set(_GROUP_CATEGORY) - set(FILE_GROUPS)
    if unknown_group:
        raise RuntimeError(
            f"file_groups: category map references unknown group: {sorted(unknown_group)}"
        )


_validate_categories()


# --------------------------------------------------------------------------- #
# Extension membership (group_id -> extensions). Bare, lowercase, no dot. Long   #
# data rows are E501-ignored for this module (see pyproject per-file-ignores),   #
# same rationale as media_types.py.                                              #
# --------------------------------------------------------------------------- #
_GROUP_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "raster-photo": (
        "jpg", "jpeg", "jpe", "jfif", "jif", "png", "webp", "bmp", "dib", "tif", "tiff", "heic",
        "heif", "heics", "hif", "avif", "avifs", "jxl", "ico", "cur", "tga", "targa", "pcx", "ppm",
        "pgm", "pbm", "pnm", "pam", "ras", "sgi", "rgb", "xbm", "xpm", "wbmp", "hdr", "exr", "dds",
        "qoi", "jng", "pict", "pct", "jp2", "j2k", "jpf", "jpx", "jpm", "fits", "fit", "flif", "bpg",
    ),
    "raw-photo": (
        "raw", "dng", "cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "raf", "orf", "rw2",
        "rwl", "pef", "srw", "x3f", "3fr", "fff", "iiq", "cap", "eip", "dcs", "dcr", "drf", "k25",
        "kdc", "mef", "mos", "erf", "mrw", "mdc", "bay", "gpr", "ari", "nksc", "cs1", "kc2", "rwz",
    ),
    "vector-image": (
        "svg", "svgz", "ai", "eps", "epsf", "epsi", "cdr", "cmx", "cgm", "wmf", "emf", "emz", "wmz",
        "vsd", "vsdx", "vss", "drw", "fig", "odg", "fodg", "hpgl", "plt", "drawio", "dia",
    ),
    "layered-image": (
        "psd", "psb", "xcf", "kra", "ora", "pdn", "clip", "csp", "sai", "sai2", "cpt", "pdd",
        "procreate", "tvpp", "afphoto", "afdesign", "sketch", "pxm", "indd", "idml",
    ),
    "animated-image": (
        "gif", "apng", "mng", "ani", "fli", "flc", "gifv",
    ),
    "video": (
        "mkv", "mp4", "m4v", "mov", "qt", "avi", "wmv", "asf", "ts", "m2ts", "mts", "m2t", "tts",
        "webm", "mpg", "mpeg", "mpe", "m1v", "m2v", "mpv", "vob", "ifo", "flv", "f4v", "swf", "3gp",
        "3g2", "3gpp", "ogv", "ogm", "rm", "rmvb", "divx", "dv", "dav", "gxf", "mxf", "y4m", "nsv",
        "roq", "mod", "tod", "amv", "wtv", "dvr-ms", "m4s", "h264", "h265", "hevc", "av1", "braw", "r3d",
    ),
    "audio-lossy": (
        "mp3", "mp2", "mp1", "mpa", "aac", "m4a", "m4r", "ogg", "oga", "opus", "weba", "wma", "ra",
        "ram", "amr", "3ga", "spx", "mpc", "mka", "ac3", "eac3", "dts", "gsm", "awb", "vqf", "qcp",
    ),
    "audio-lossless": (
        "flac", "alac", "wav", "wave", "aif", "aiff", "aifc", "ape", "wv", "wvc", "tta", "tak",
        "mlp", "shn", "la", "ofr", "ofs", "dsf", "dff", "dsd", "l16", "w64", "rf64", "bwf", "caf",
        "au", "snd", "pcm",
    ),
    "audiobook": (
        "m4b", "aax", "aaxc", "aa",
    ),
    "audio-project": (
        "als", "alp", "flp", "logicx", "logic", "ptx", "ptf", "pts", "cpr", "npr", "rpp",
        "mmp", "mmpz", "sesx", "ses", "aup", "aup3", "rns", "bwproject", "dawproject", "sng", "cwp",
        "cwb", "reapeaks", "sf2", "sf3", "sfz", "sfark", "exs", "nki", "nkm", "nkc", "nksn", "nksf",
        "nkx", "gig", "dls", "kit", "sxt", "fxp", "fxb", "vstpreset", "aupreset", "adg", "adv",
        "agr", "ffp", "h2song", "rex", "rx2", "rcy", "akp", "syx", "pat",
    ),
    "playlist": (
        "m3u", "m3u8", "pls", "cue", "xspf", "wpl", "asx", "wax", "wvx", "b4s", "kpl", "zpl", "pla",
        "aimppl", "fpl",
    ),
    "subtitle": (
        "srt", "ass", "ssa", "vtt", "sub", "sbv", "smi", "sami", "ttml", "dfxp", "scc", "mcc",
        "usf", "lrc", "sup", "idx", "rt", "mpsub", "jss", "aqt", "pjs",
    ),
    "document-text": (
        "txt", "text", "me", "1st", "diz", "ans", "wtx", "etx", "nfo", "readme",
    ),
    "document-office": (
        "doc", "docx", "docm", "dot", "dotx", "dotm", "odt", "ott", "fodt", "rtf", "wpd", "wps",
        "wpt", "hwp", "hwpx", "abw", "zabw", "sxw", "stw", "sdw", "lwp", "pages", "gdoc", "uot",
        "uof", "cwk", "mcw", "wri", "602", "kwd",
    ),
    "pdf": (
        "pdf", "fdf", "xfdf", "xdp", "ps", "prn", "xps", "oxps",
    ),
    "presentation": (
        "ppt", "pptx", "pptm", "pps", "ppsx", "ppsm", "pot", "potx", "potm", "odp", "otp", "fodp",
        "sxi", "sti", "sdd", "gslides", "shw", "prz", "sldx", "sldm", "uop",
    ),
    "spreadsheet": (
        "xls", "xlsx", "xlsm", "xlsb", "xlt", "xltx", "xltm", "xlw", "xlam", "xla", "ods", "ots",
        "fods", "sxc", "stc", "csv", "tsv", "tab", "numbers", "gsheet", "dif", "slk", "sylk", "wk1",
        "wk3", "wk4", "wks", "wq1", "qpw", "123", "wb2", "gnumeric", "et", "uos",
    ),
    "ebook": (
        "epub", "mobi", "azw", "azw3", "azw4", "kfx", "kf8", "prc", "pdb", "fb2", "fb2z", "fbz",
        "lit", "lrf", "lrx", "ceb", "cebx", "djvu", "djv", "ibooks", "opf", "tcr", "snb", "tpz",
        "kpf", "ncx", "oeb", "acsm", "chm",
    ),
    "comic": (
        "cbz", "cbr", "cb7", "cbt", "cba", "cbw", "acbf",
    ),
    "markup": (
        "md", "markdown", "mdown", "mkd", "mkdn", "mdwn", "mdx", "html", "htm", "xhtml", "xht",
        "shtml", "xml", "xsl", "xslt", "xsd", "dtd", "rng", "rss", "atom", "rst", "adoc", "asciidoc",
        "textile", "creole", "org", "pod", "roff", "man", "nroff", "troff", "texi", "texinfo",
        "tex", "latex", "ltx", "sty", "cls", "dtx", "bib", "bst", "sgml", "dita", "docbook", "wiki",
        "mediawiki", "typ", "haml", "slim", "pug", "jade", "ejs", "hbs", "handlebars", "mustache",
        "njk", "nunjucks", "liquid", "erb", "twig", "jinja", "jinja2", "j2", "rdoc",
    ),
    "3d-model": (
        "stl", "obj", "mtl", "fbx", "gltf", "gltf2", "glb", "3mf", "ply", "blend", "blend1", "dae",
        "collada", "3ds", "max", "ma", "mb", "c4d", "lwo", "lws", "lxo", "lxl", "ztl", "zpr", "abc",
        "usd", "usda", "usdc", "usdz", "x3d", "x3db", "x3dv", "wrl", "vrml", "off", "gcode", "gco",
        "amf", "vox", "qb", "pmx", "pmd", "mmd", "mqo", "splat", "ksplat", "e57", "pcd", "las",
        "laz", "xyz", "mesh",
    ),
    "cad": (
        "dwg", "dxf", "dwf", "dwfx", "dgn", "step", "stp", "stpz", "iges", "igs", "sat", "sab",
        "skp", "f3d", "f3z", "ipt", "iam", "idw", "prt", "sldprt", "sldasm", "slddrw", "catpart",
        "catproduct", "catdrawing", "3dm", "rvt", "rfa", "rte", "pln", "3dxml", "jt", "x_t", "x_b",
        "model", "ipn", "ifc", "ifcxml", "ifczip", "brd", "sch", "kicad_pcb", "kicad_sch", "gbr",
        "drl", "emn", "neu", "vwx", "mcd", "dwt", "par", "psm", "scdoc", "scad", "fcstd", "nc",
        "cnc", "tap",
    ),
    "font": (
        "ttf", "otf", "ttc", "otc", "woff", "woff2", "eot", "fon", "fnt", "pfb", "pfa", "pfm",
        "afm", "bdf", "pcf", "snf", "dfont", "suit", "fond", "sfd", "ufo", "glyphs", "glyphspackage",
        "vfb", "pf2", "gf", "pk", "tfm", "t1", "cff", "fot",
    ),
    "source-code": (
        "c", "h", "i", "cpp", "cxx", "cc", "c++", "hpp", "hxx", "hh", "h++", "tcc", "ipp", "tpp",
        "inl", "cs", "csx", "java", "jav", "kt", "kts", "ktm", "scala", "sc", "groovy", "gvy", "gy",
        "py", "pyw", "pyx", "pxd", "pxi", "pyi", "rb", "rbw", "rake", "gemspec", "php", "php3",
        "php4", "php5", "phtml", "phps", "go", "rs", "swift", "m", "mm", "js", "mjs", "cjs", "jsx",
        "tsx", "cts", "coffee", "litcoffee", "dart", "lua", "pl", "pm", "t", "tcl",
        "r", "jl", "hs", "lhs", "ml", "mli", "fs", "fsi", "fsx", "fsscript", "clj", "cljs", "cljc",
        "edn", "erl", "hrl", "ex", "eex", "leex", "heex", "elm", "cr", "nim", "nims", "zig",
        "v", "sv", "svh", "vhdl", "d", "pas", "pp", "dpr", "inc", "f", "f90", "f95", "f03", "f08",
        "for", "ftn", "ada", "adb", "ads", "cob", "cbl", "cobol", "cpy", "lisp", "lsp", "cl", "el",
        "scm", "ss", "rkt", "vb", "bas", "asm", "s", "nasm", "sol", "move", "cairo", "wat", "gd",
        "rpy", "ino", "au3", "purs", "re", "rei", "vala", "vapi", "hx", "hxml", "wgsl", "glsl",
        "hlsl", "metal", "vert", "frag", "comp", "geom", "cu", "cuh",
    ),
    "script": (
        "sh", "bash", "zsh", "fish", "ksh", "csh", "tcsh", "ash", "dash", "command", "tool", "ps1",
        "psm1", "psd1", "ps1xml", "bat", "cmd", "btm", "vbs", "vbe", "wsf", "wsh", "hta", "awk",
        "sed", "expect", "exp", "nu", "xonsh", "elv", "ahk", "ahk2", "applescript", "scpt", "scptd",
        "cgi",
    ),
    "web-asset": (
        "css", "scss", "sass", "less", "styl", "stylus", "pcss", "postcss", "wasm", "webmanifest",
        "importmap", "vue", "svelte", "astro",
    ),
    "notebook": (
        "ipynb", "rmd", "rmarkdown", "qmd", "rnw", "zpln", "livemd", "nb",
    ),
    "config-data": (
        "json", "json5", "jsonc", "jsonl", "ndjson", "geojson", "topojson", "yaml", "yml", "toml",
        "ini", "cfg", "conf", "config", "cnf", "env", "dotenv", "properties", "prop", "plist", "hcl",
        "tf", "tfvars", "tfstate", "nix", "dhall", "ron", "kdl", "jsonnet", "libsonnet", "proto",
        "thrift", "capnp", "fbs", "avsc", "graphql", "gql", "reg", "desktop", "service", "unit",
        "rc", "npmrc", "yarnrc", "babelrc", "eslintrc", "prettierrc", "editorconfig", "cmake", "mk",
        "mak", "ninja", "bazel", "bzl", "sbt", "gradle", "lock", "resx", "pbxproj", "xcconfig",
        "csproj", "vcxproj", "sln", "pri", "prefs", "containerfile", "dockerfile",
    ),
    "database": (
        "db", "db3", "sqlite", "sqlite3", "sqlitedb", "s3db", "sl3", "mdb", "accdb", "accde", "mde",
        "mdf", "ndf", "ldf", "myd", "myi", "frm", "ibd", "dbf", "gdb", "fdb", "nsf", "ntf", "kdbx",
        "kdb", "sql", "ddl", "dump", "odb", "sdf", "fp7", "fmp12", "realm", "wdb", "bson", "parquet",
        "avro", "orc", "feather", "arrow", "h5", "hdf5", "cdf", "npy", "npz", "pkl", "pickle",
        "rdata", "rds", "sav", "dta", "por", "sas7bdat", "xpt",
    ),
    "archive": (
        "zip", "zipx", "7z", "s7z", "rar", "r00", "r01", "tar", "gz", "gzip", "tgz", "taz", "bz2",
        "tbz", "tbz2", "tb2", "xz", "txz", "lz", "tlz", "lzma", "tlzma", "zst", "zstd", "tzst",
        "lz4", "tlz4", "lzo", "tlzo", "lzop", "lha", "lzh", "lrz", "br", "sz", "cpio", "ar", "cab",
        "arj", "arc", "ace", "alz", "sit", "sitx", "sea", "hqx", "z", "zoo", "zpaq", "paq", "pea",
        "xar", "pak", "pk3", "pk4", "vpk", "gcf", "shar", "zz", "rz", "dgc", "gca", "kgb", "uc2",
        "uha", "yz1", "dar", "afa", "ba", "b1", "jar", "war", "ear", "sar",
    ),
    "disk-image": (
        "iso", "img", "dmg", "toast", "nrg", "mds", "ccd", "cif", "vhd", "vhdx", "vmdk", "vdi",
        "qcow", "qcow2", "qed", "hdd", "vfd", "dsk", "d64", "adf", "adz", "ova", "ovf", "wim", "swm",
        "esd", "gho", "ghs", "tib", "udf", "fdi", "sparseimage", "sparsebundle", "ffu", "wbfs",
        "nsp", "xci", "cso", "chd", "gi", "daa", "uif", "isz", "e01", "aff", "b5t", "b6t", "gcm",
    ),
    "package-installer": (
        "deb", "udeb", "rpm", "drpm", "srpm", "msi", "msp", "msix", "msixbundle", "appx",
        "appxbundle", "apk", "apks", "apkm", "xapk", "aab", "aar", "appimage", "flatpak",
        "flatpakref", "snap", "pkg", "mpkg", "ipa", "tipa", "xpi", "crx", "vsix", "whl", "egg",
        "gem", "nupkg", "jmod", "eopkg", "xbps", "tazpkg", "apkg", "run",
    ),
    "executable-binary": (
        "exe", "com", "dll", "so", "dylib", "bin", "o", "a", "lib", "ko", "elf", "out", "axf",
        "class", "pyc", "pyo", "pyd", "beam", "jsa", "rlib", "rmeta", "node", "efi", "sys", "drv",
        "ocx", "cpl", "ax", "scr", "bundle", "prx", "self", "xex", "nro", "nso", "rpx", "dol",
    ),
    "email": (
        "eml", "emlx", "msg", "oft", "mbox", "mbx", "mbs", "pst", "ost", "dbx", "nws", "mim", "mime",
        "mht", "mhtml", "tnef", "p7m",
    ),
    "certificate-key": (
        "pem", "crt", "cert", "cer", "der", "key", "pub", "csr", "req", "pfx", "p12", "p7b", "p7c",
        "p7r", "p7s", "p8", "pk8", "jks", "keystore", "bks", "jceks", "gpg", "pgp", "sig", "asc",
        "kbx", "ppk", "crl", "spc", "jwk", "jwks", "pkr", "skr",
    ),
    "log": (
        "log", "log1", "log2", "logs", "err", "trace", "journal", "ltsv", "evtx", "evt", "etl",
        "dmp", "mdmp", "hprof",
    ),
}


def _invert(group_exts: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Build the flat ``ext -> group_id`` map, refusing duplicate extensions.

    An extension may belong to at most one group (``file_group`` is a single value
    per document), so each ambiguous suffix is placed in exactly ONE authoring row
    (its documented winner — see the group ``notes``). A duplicate across rows is a
    programming error and raises at import rather than silently letting inversion
    pick a winner."""
    ext_map: dict[str, str] = {}
    dupes: dict[str, list[str]] = {}
    for gid, exts in group_exts.items():
        for e in exts:
            if e in ext_map and ext_map[e] != gid:
                dupes.setdefault(e, [ext_map[e]]).append(gid)
                continue
            ext_map[e] = gid
    if dupes:
        raise RuntimeError(
            f"file_groups: extension(s) mapped to multiple groups: {dict(dupes)}"
        )
    unknown = {gid for gid in ext_map.values() if gid not in FILE_GROUPS}
    if unknown:
        raise RuntimeError(f"file_groups: unknown group id(s) referenced: {unknown}")
    return ext_map


#: Flat ``ext -> group_id`` map (bare lowercase extension). The public researched
#: map; derived from ``_GROUP_EXTENSIONS`` so the two can never drift.
EXT_GROUP_MAP: dict[str, str] = _invert(_GROUP_EXTENSIONS)


#: Recognised multi-part endings that classify the WHOLE file (see module doc).
#: All the ``tar.*`` bundles resolve to ``archive``.
_COMPOUND_GROUP_MAP: dict[str, str] = {
    f"tar.{w}": "archive"
    for w in ("gz", "bz2", "xz", "zst", "zstd", "lz", "lz4", "lzma", "lzo", "z", "br")
}


def detect_group(path: str) -> str:
    """Classify ``path`` into a ``file_group`` id (pure; unknown -> ``other``).

    Mirrors :func:`filearr.media_types.detect`: case-insensitive, extension-only.
    A recognised compound ending (``.tar.gz`` …) is consulted first and wins as a
    whole (``archive``); otherwise the final extension decides. A name with no
    usable extension (``.bashrc``, ``README``) resolves to ``other``."""
    p = PurePath(path)
    suffixes = [s.lstrip(".").lower() for s in p.suffixes]
    if len(suffixes) >= 2:
        compound = f"{suffixes[-2]}.{suffixes[-1]}"
        gid = _COMPOUND_GROUP_MAP.get(compound)
        if gid is not None:
            return gid
    ext = p.suffix.lstrip(".").lower()
    return EXT_GROUP_MAP.get(ext, GROUP_OTHER)


def category_for_group(group_id: str) -> str:
    """The parent ``file_category`` key of ``group_id`` (seed rollup). Unknown ids
    (defensively) resolve to ``other``."""
    return _GROUP_CATEGORY.get(group_id, GROUP_OTHER)


def detect_category(path: str) -> str:
    """Classify ``path`` into a ``file_category`` key (pure; unknown -> ``other``).

    Composes :func:`detect_group` with the seed group->category rollup, so it shares
    every rule ``detect_group`` applies (case-insensitivity, compound ``tar.*``,
    dotfiles -> ``other``). This is the SEED classifier used by the search
    projection (``search.build_doc``); the RUNTIME item classifier is the DB-backed
    ``filearr.taxonomy`` service."""
    return category_for_group(detect_group(path))


def category_extractor(category_key: str) -> str | None:
    """The extraction pipeline the seed category routes to (W8-B), or ``None``.
    Unknown category keys raise ``KeyError``."""
    return FILE_CATEGORIES[category_key].extractor


def taxonomy_seed_payload() -> dict:
    """The full DEFAULT taxonomy as plain data — the seed the W8-A migration writes
    into the ``file_categories`` / ``file_groups`` / ``file_group_extensions``
    tables, and the fallback the runtime service builds from an empty/unreachable
    DB. Shape (order == registry order)::

        {
          "categories": [{key,label,description,extractor,sort_order}, ...],
          "groups":     [{key,label,description,category,sort_order}, ...],
          "extensions": [{ext,group}, ...],   # sorted by ext
        }
    """
    categories = [
        {
            "key": c.key,
            "label": c.label,
            "description": c.description,
            "extractor": c.extractor,
            "sort_order": i,
        }
        for i, c in enumerate(FILE_CATEGORIES.values())
    ]
    groups = [
        {
            "key": g.id,
            "label": g.label,
            "description": g.description,
            "category": category_for_group(g.id),
            "sort_order": i,
        }
        for i, g in enumerate(FILE_GROUPS.values())
    ]
    extensions = [
        {"ext": ext, "group": EXT_GROUP_MAP[ext]} for ext in sorted(EXT_GROUP_MAP)
    ]
    return {"categories": categories, "groups": groups, "extensions": extensions}


def extensions_for_group(group_id: str) -> list[str]:
    """Sorted, de-duplicated extensions currently mapped to ``group_id`` (derived
    from ``EXT_GROUP_MAP``, so it reflects collision resolution). Unknown or
    member-less groups (e.g. ``other``) return ``[]``."""
    return sorted(e for e, gid in EXT_GROUP_MAP.items() if gid == group_id)


def registry_payload() -> list[dict]:
    """The ``/api/v1/system/file-groups`` response body (also the source the
    reference doc renders from): one ``{id, label, file_category, description,
    extensions}`` object per group, in registry order. ``file_category`` is the
    parent category key; ``extensions`` is the sorted member list. (W8-B replaced
    the removed ``media_type`` nominal-parent field with ``file_category``.)"""
    return [
        {
            "id": g.id,
            "label": g.label,
            "file_category": category_for_group(g.id),
            "description": g.description,
            "extensions": extensions_for_group(g.id),
        }
        for g in FILE_GROUPS.values()
    ]


# --------------------------------------------------------------------------- #
# Reference-doc generator. The committed docs-site page is generated FROM this   #
# (a test regenerates and diffs, so the doc can never silently drift from the    #
# registry). Pure string assembly — no file I/O here.                            #
# --------------------------------------------------------------------------- #
_DOC_INTRO = """\
<!--
  GENERATED FILE — do not edit by hand.
  Source of truth: backend/filearr/file_groups.py (render_reference_markdown()).
  Regenerate: python -c "from filearr.file_groups import write_reference_doc; write_reference_doc()"
  A test (tests/test_file_groups.py) asserts this file matches the generator.
-->

# File-extension groups

Filearr classifies every catalogued file by the **File Extension Similarity
Taxonomy** — a two-level tree derived from the file *extension*:

* **`file_category`** — the coarse parent (`image`, `audio`, `video`, `document`,
  `three-d-cad`, `development`, `archive`, `system`, `other`). Each category carries
  an `extractor` (`image`/`audio`/`video`/`document`/`model3d` or none) — the
  extraction pipeline it routes to. `file_category` is the authoritative coarse
  bucket (it replaced the removed `media_type` enum in W8-B).
* **`file_group`** — the finer child (37 groups). It both **subdivides** its
  category (RAW vs. raster photos; lossy vs. lossless audio) and gives signal to the
  otherwise-opaque `other`/system files (archives, installers, source code, fonts,
  configs, subtitles, …).

The taxonomy is **DB-backed and editable** at runtime (see the CRUD API below);
this page documents the shipped DEFAULT — a live install may have edited it.
`file_category` and `file_group` are computed together from the extension, so a
`.wav` is `file_category=audio` / `file_group=audio-lossless` and a `.zip` is
`file_category=archive` / `file_group=archive`.

## Filtering by group

`file_group` and `file_category` are Meilisearch filters and facets. Pass one or
more `file_group` (or `file_category`) values to the search API (repeatable = OR):

```
GET /api/v1/search?q=&file_group=raw-photo
GET /api/v1/search?q=invoice&file_group=pdf&file_group=document-office
```

The machine-readable DEFAULT registry (this table, as JSON) is served from:

```
GET /api/v1/system/file-groups
```

The **live, possibly-edited** taxonomy tree (categories → groups → extensions) and
its admin CRUD (create/update/delete categories & groups, add/remove/reparent
extensions) live under:

```
GET    /api/v1/taxonomy
POST   /api/v1/taxonomy/categories        PATCH/DELETE .../categories/{key}
POST   /api/v1/taxonomy/groups            PATCH/DELETE .../groups/{key}
POST   /api/v1/taxonomy/groups/{key}/extensions   DELETE .../extensions/{ext}
```

!!! note "After deploying a group-map change"
    `file_group` is projected onto each search document at index time. After
    changing the extension map (or on first rollout), run a **rebuild-index**
    (`POST /api/v1/system/rebuild-index`) so existing documents pick up the new
    `file_group` value. Newly scanned/updated items get it automatically.
"""


def render_reference_markdown() -> str:
    """Render the full ``file-extension-groups.md`` reference page from the
    registry (pure). The committed doc must equal this byte-for-byte."""
    lines: list[str] = [_DOC_INTRO.rstrip(), ""]

    # Category overview.
    lines.append("## Categories")
    lines.append("")
    lines.append(
        "The coarse parent layer. Each category rolls up one or more groups and "
        "declares the extraction `extractor` it routes to (or none)."
    )
    lines.append("")
    lines.append("| Category | Label | Extractor | Groups |")
    lines.append("| --- | --- | --- | --- |")
    for c in FILE_CATEGORIES.values():
        ex = f"`{c.extractor}`" if c.extractor is not None else "—"
        members = [gid for gid in FILE_GROUPS if category_for_group(gid) == c.key]
        member_list = ", ".join(f"`{gid}`" for gid in members) if members else "—"
        lines.append(f"| `{c.key}` | {c.label} | {ex} | {member_list} |")
    lines.append("")

    # Per-group sections.
    lines.append("## Groups")
    lines.append("")
    for g in FILE_GROUPS.values():
        exts = extensions_for_group(g.id)
        cat = category_for_group(g.id)
        lines.append(f"### `{g.id}` — {g.label}")
        lines.append("")
        lines.append(f"*Parent `file_category`:* `{cat}`  ")
        lines.append(f"*Extensions ({len(exts)}):* {_fmt_ext_list(exts)}")
        lines.append("")
        lines.append(g.description)
        if g.notes:
            lines.append("")
            lines.append(f"!!! info \"Notes\"\n    {g.notes}")
        lines.append("")

    # Summary table.
    lines.append("## Summary")
    lines.append("")
    lines.append("| Group | Label | Parent `file_category` | # ext |")
    lines.append("| --- | --- | --- | --: |")
    for g in FILE_GROUPS.values():
        cat = category_for_group(g.id)
        n = len(extensions_for_group(g.id))
        lines.append(f"| `{g.id}` | {g.label} | `{cat}` | {n} |")
    lines.append("")

    # Flat alphabetical ext -> group index.
    lines.append("## Extension index")
    lines.append("")
    lines.append("Every mapped extension, alphabetically, with its group.")
    lines.append("")
    lines.append("| Extension | Group |")
    lines.append("| --- | --- |")
    for ext in sorted(EXT_GROUP_MAP):
        lines.append(f"| `.{ext}` | `{EXT_GROUP_MAP[ext]}` |")
    lines.append("")

    total = len(EXT_GROUP_MAP)
    lines.append(
        f"_{total} extensions across {len(FILE_GROUPS)} groups. "
        "Generated from `backend/filearr/file_groups.py`._"
    )
    lines.append("")
    return "\n".join(lines)


def _fmt_ext_list(exts: list[str]) -> str:
    """Render a sorted extension list as inline code, or an em dash when empty."""
    if not exts:
        return "—"
    return ", ".join(f"`.{e}`" for e in exts)


#: Committed reference-doc path, relative to the repo root.
REFERENCE_DOC_RELPATH = "docs-site/reference/file-extension-groups.md"


def reference_doc_path() -> PurePath:
    """Absolute path to the committed reference doc (repo-root relative). Kept as a
    helper so the generator and its drift test agree on the location."""
    # file_groups.py lives at backend/filearr/file_groups.py -> repo root is parents[2].
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    return root / REFERENCE_DOC_RELPATH


def write_reference_doc() -> str:
    """Write the rendered reference markdown to the committed path; return the path.
    Convenience for regeneration (``python -c 'from filearr.file_groups import
    write_reference_doc; write_reference_doc()'``). NOT called at runtime."""
    import pathlib

    path = pathlib.Path(reference_doc_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_reference_markdown(), encoding="utf-8", newline="\n")
    return str(path)
