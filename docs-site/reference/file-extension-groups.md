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

## Categories

The coarse parent layer. Each category rolls up one or more groups and declares the extraction `extractor` it routes to (or none).

| Category | Label | Extractor | Groups |
| --- | --- | --- | --- |
| `image` | Image | `image` | `raster-photo`, `raw-photo`, `vector-image`, `layered-image`, `animated-image` |
| `audio` | Audio | `audio` | `audio-lossy`, `audio-lossless`, `audiobook`, `audio-project`, `playlist` |
| `video` | Video & subtitle | `video` | `video`, `subtitle` |
| `document` | Document | `document` | `document-text`, `document-office`, `pdf`, `presentation`, `spreadsheet`, `ebook`, `comic`, `markup` |
| `three-d-cad` | 3D & CAD | `model3d` | `3d-model`, `cad` |
| `development` | Development & data | — | `source-code`, `script`, `web-asset`, `notebook`, `config-data` |
| `archive` | Archive & image | — | `archive`, `disk-image`, `package-installer` |
| `system` | System & data files | — | `font`, `database`, `executable-binary`, `email`, `certificate-key`, `log` |
| `other` | Other / unknown | — | `other` |

## Groups

### `raster-photo` — Raster / photo

*Parent `file_category`:* `image`  
*Extensions (50):* `.avif`, `.avifs`, `.bmp`, `.bpg`, `.cur`, `.dds`, `.dib`, `.exr`, `.fit`, `.fits`, `.flif`, `.hdr`, `.heic`, `.heics`, `.heif`, `.hif`, `.ico`, `.j2k`, `.jfif`, `.jif`, `.jng`, `.jp2`, `.jpe`, `.jpeg`, `.jpf`, `.jpg`, `.jpm`, `.jpx`, `.jxl`, `.pam`, `.pbm`, `.pct`, `.pcx`, `.pgm`, `.pict`, `.png`, `.pnm`, `.ppm`, `.qoi`, `.ras`, `.rgb`, `.sgi`, `.targa`, `.tga`, `.tif`, `.tiff`, `.wbmp`, `.webp`, `.xbm`, `.xpm`

Pixel (bitmap) images — photographs, screenshots, web graphics and their HDR/high-bit-depth cousins. The everyday image formats.

### `raw-photo` — Camera RAW

*Parent `file_category`:* `image`  
*Extensions (39):* `.3fr`, `.ari`, `.arw`, `.bay`, `.cap`, `.cr2`, `.cr3`, `.crw`, `.cs1`, `.dcr`, `.dcs`, `.dng`, `.drf`, `.eip`, `.erf`, `.fff`, `.gpr`, `.iiq`, `.k25`, `.kc2`, `.kdc`, `.mdc`, `.mef`, `.mos`, `.mrw`, `.nef`, `.nksc`, `.nrw`, `.orf`, `.pef`, `.raf`, `.raw`, `.rw2`, `.rwl`, `.rwz`, `.sr2`, `.srf`, `.srw`, `.x3f`

Unprocessed camera sensor data (mostly one proprietary format per manufacturer) plus the open Adobe DNG. Needs a RAW developer, not a plain image viewer.

### `vector-image` — Vector image

*Parent `file_category`:* `image`  
*Extensions (24):* `.ai`, `.cdr`, `.cgm`, `.cmx`, `.dia`, `.drawio`, `.drw`, `.emf`, `.emz`, `.eps`, `.epsf`, `.epsi`, `.fig`, `.fodg`, `.hpgl`, `.odg`, `.plt`, `.svg`, `.svgz`, `.vsd`, `.vsdx`, `.vss`, `.wmf`, `.wmz`

Resolution-independent geometry (paths/shapes) rather than pixels — illustration, logos, diagrams.

!!! info "Notes"
    ``cdr`` is CorelDRAW here (not a macOS disc image); ``eps`` is the vector Encapsulated PostScript (raster print PostScript ``ps`` is grouped under ``pdf``).

### `layered-image` — Layered / authoring image

*Parent `file_category`:* `image`  
*Extensions (20):* `.afdesign`, `.afphoto`, `.clip`, `.cpt`, `.csp`, `.idml`, `.indd`, `.kra`, `.ora`, `.pdd`, `.pdn`, `.procreate`, `.psb`, `.psd`, `.pxm`, `.sai`, `.sai2`, `.sketch`, `.tvpp`, `.xcf`

Editable multi-layer image project documents from raster editors (Photoshop, GIMP, Krita, Affinity, …) — not a flattened export.

### `animated-image` — Animated image

*Parent `file_category`:* `image`  
*Extensions (7):* `.ani`, `.apng`, `.flc`, `.fli`, `.gif`, `.gifv`, `.mng`

Short looping animations delivered as an image rather than a video container.

!!! info "Notes"
    ``gif`` is classified here (its canonical animated use) rather than under raster-photo; APNG/animated-WebP share the ``png``/``webp`` extensions and so cannot be split out by extension alone.

### `video` — Video

*Parent `file_category`:* `video`  
*Extensions (52):* `.3g2`, `.3gp`, `.3gpp`, `.amv`, `.asf`, `.av1`, `.avi`, `.braw`, `.dav`, `.divx`, `.dv`, `.dvr-ms`, `.f4v`, `.flv`, `.gxf`, `.h264`, `.h265`, `.hevc`, `.ifo`, `.m1v`, `.m2t`, `.m2ts`, `.m2v`, `.m4s`, `.m4v`, `.mkv`, `.mod`, `.mov`, `.mp4`, `.mpe`, `.mpeg`, `.mpg`, `.mpv`, `.mts`, `.mxf`, `.nsv`, `.ogm`, `.ogv`, `.qt`, `.r3d`, `.rm`, `.rmvb`, `.roq`, `.swf`, `.tod`, `.ts`, `.tts`, `.vob`, `.webm`, `.wmv`, `.wtv`, `.y4m`

Moving-image containers and streams (movies, clips, recordings).

!!! info "Notes"
    ``ts``/``mts`` are MPEG transport streams here, not TypeScript — ``tsx``/``cts`` still classify as source code.

### `audio-lossy` — Lossy audio

*Parent `file_category`:* `audio`  
*Extensions (26):* `.3ga`, `.aac`, `.ac3`, `.amr`, `.awb`, `.dts`, `.eac3`, `.gsm`, `.m4a`, `.m4r`, `.mka`, `.mp1`, `.mp2`, `.mp3`, `.mpa`, `.mpc`, `.oga`, `.ogg`, `.opus`, `.qcp`, `.ra`, `.ram`, `.spx`, `.vqf`, `.weba`, `.wma`

Perceptually compressed audio (MP3/AAC/Ogg/Opus/…) — smaller files, irreversible quality loss.

### `audio-lossless` — Lossless / PCM audio

*Parent `file_category`:* `audio`  
*Extensions (28):* `.aif`, `.aifc`, `.aiff`, `.alac`, `.ape`, `.au`, `.bwf`, `.caf`, `.dff`, `.dsd`, `.dsf`, `.flac`, `.l16`, `.la`, `.mlp`, `.ofr`, `.ofs`, `.pcm`, `.rf64`, `.shn`, `.snd`, `.tak`, `.tta`, `.w64`, `.wav`, `.wave`, `.wv`, `.wvc`

Losslessly compressed or uncompressed PCM/DSD audio (FLAC/ALAC/WAV/AIFF/APE/…) — bit-exact reconstruction.

!!! info "Notes"
    Raw PCM sample files (``wav``/``aiff``) group here as lossless audio (file_category ``audio``), alongside their sampler cousins in ``audio-project``.

### `audiobook` — Audiobook

*Parent `file_category`:* `audio`  
*Extensions (4):* `.aa`, `.aax`, `.aaxc`, `.m4b`

Chapterised spoken-word audiobook containers (M4B and Audible formats).

### `audio-project` — Audio project, sampler & instrument

*Parent `file_category`:* `audio`  
*Extensions (54):* `.adg`, `.adv`, `.agr`, `.akp`, `.alp`, `.als`, `.aup`, `.aup3`, `.aupreset`, `.bwproject`, `.cpr`, `.cwb`, `.cwp`, `.dawproject`, `.dls`, `.exs`, `.ffp`, `.flp`, `.fxb`, `.fxp`, `.gig`, `.h2song`, `.kit`, `.logic`, `.logicx`, `.mmp`, `.mmpz`, `.nkc`, `.nki`, `.nkm`, `.nksf`, `.nksn`, `.nkx`, `.npr`, `.pat`, `.ptf`, `.pts`, `.ptx`, `.rcy`, `.reapeaks`, `.rex`, `.rns`, `.rpp`, `.rx2`, `.ses`, `.sesx`, `.sf2`, `.sf3`, `.sfark`, `.sfz`, `.sng`, `.sxt`, `.syx`, `.vstpreset`

Digital-audio-workstation project/session files and sampler / synth instrument, patch, sound-font and loop formats — production assets, not finished audio.

!!! info "Notes"
    ``ptx`` is a Pro Tools session here (not Pentax RAW).

### `playlist` — Playlist

*Parent `file_category`:* `audio`  
*Extensions (15):* `.aimppl`, `.asx`, `.b4s`, `.cue`, `.fpl`, `.kpl`, `.m3u`, `.m3u8`, `.pla`, `.pls`, `.wax`, `.wpl`, `.wvx`, `.xspf`, `.zpl`

Ordered references to media tracks/clips (and cue sheets) — the playlist itself carries no audio/video payload.

### `subtitle` — Subtitle / caption

*Parent `file_category`:* `video`  
*Extensions (21):* `.aqt`, `.ass`, `.dfxp`, `.idx`, `.jss`, `.lrc`, `.mcc`, `.mpsub`, `.pjs`, `.rt`, `.sami`, `.sbv`, `.scc`, `.smi`, `.srt`, `.ssa`, `.sub`, `.sup`, `.ttml`, `.usf`, `.vtt`

Timed-text subtitle, caption and synced-lyric sidecar formats.

!!! info "Notes"
    ``stl`` is claimed by ``3d-model`` (stereolithography); the EBU STL subtitle format is therefore not represented by that extension here.

### `document-text` — Plain text document

*Parent `file_category`:* `document`  
*Extensions (10):* `.1st`, `.ans`, `.diz`, `.etx`, `.me`, `.nfo`, `.readme`, `.text`, `.txt`, `.wtx`

Human-readable plain-text documents with no rich formatting model.

### `document-office` — Word-processor / office document

*Parent `file_category`:* `document`  
*Extensions (30):* `.602`, `.abw`, `.cwk`, `.doc`, `.docm`, `.docx`, `.dot`, `.dotm`, `.dotx`, `.fodt`, `.gdoc`, `.hwp`, `.hwpx`, `.kwd`, `.lwp`, `.mcw`, `.odt`, `.ott`, `.pages`, `.rtf`, `.sdw`, `.stw`, `.sxw`, `.uof`, `.uot`, `.wpd`, `.wps`, `.wpt`, `.wri`, `.zabw`

Rich word-processor documents (Word/OpenDocument/Pages/WordPerfect/…) with styles, layout and embedded objects.

### `pdf` — PDF & page description

*Parent `file_category`:* `document`  
*Extensions (8):* `.fdf`, `.oxps`, `.pdf`, `.prn`, `.ps`, `.xdp`, `.xfdf`, `.xps`

Fixed-layout page-description documents — PDF and the PostScript / XPS print family.

### `presentation` — Presentation

*Parent `file_category`:* `document`  
*Extensions (21):* `.fodp`, `.gslides`, `.odp`, `.otp`, `.pot`, `.potm`, `.potx`, `.pps`, `.ppsm`, `.ppsx`, `.ppt`, `.pptm`, `.pptx`, `.prz`, `.sdd`, `.shw`, `.sldm`, `.sldx`, `.sti`, `.sxi`, `.uop`

Slide decks (PowerPoint / Keynote / Impress / Google Slides).

!!! info "Notes"
    Slide decks group here under file_category ``document``.

### `spreadsheet` — Spreadsheet / tabular

*Parent `file_category`:* `document`  
*Extensions (34):* `.123`, `.csv`, `.dif`, `.et`, `.fods`, `.gnumeric`, `.gsheet`, `.numbers`, `.ods`, `.ots`, `.qpw`, `.slk`, `.stc`, `.sxc`, `.sylk`, `.tab`, `.tsv`, `.uos`, `.wb2`, `.wk1`, `.wk3`, `.wk4`, `.wks`, `.wq1`, `.xla`, `.xlam`, `.xls`, `.xlsb`, `.xlsm`, `.xlsx`, `.xlt`, `.xltm`, `.xltx`, `.xlw`

Spreadsheet workbooks and delimited tabular data (CSV/TSV).

### `ebook` — E-book

*Parent `file_category`:* `document`  
*Extensions (29):* `.acsm`, `.azw`, `.azw3`, `.azw4`, `.ceb`, `.cebx`, `.chm`, `.djv`, `.djvu`, `.epub`, `.fb2`, `.fb2z`, `.fbz`, `.ibooks`, `.kf8`, `.kfx`, `.kpf`, `.lit`, `.lrf`, `.lrx`, `.mobi`, `.ncx`, `.oeb`, `.opf`, `.pdb`, `.prc`, `.snb`, `.tcr`, `.tpz`

Reflowable and fixed e-book formats (EPUB, Kindle, FictionBook, DjVu, …).

### `comic` — Comic archive

*Parent `file_category`:* `document`  
*Extensions (7):* `.acbf`, `.cb7`, `.cba`, `.cbr`, `.cbt`, `.cbw`, `.cbz`

Comic-book archives (a page-image bundle in a ZIP/RAR/7z/tar wrapper).

### `markup` — Markup & typesetting source

*Parent `file_category`:* `document`  
*Extensions (64):* `.adoc`, `.asciidoc`, `.atom`, `.bib`, `.bst`, `.cls`, `.creole`, `.dita`, `.docbook`, `.dtd`, `.dtx`, `.ejs`, `.erb`, `.haml`, `.handlebars`, `.hbs`, `.htm`, `.html`, `.j2`, `.jade`, `.jinja`, `.jinja2`, `.latex`, `.liquid`, `.ltx`, `.man`, `.markdown`, `.md`, `.mdown`, `.mdwn`, `.mdx`, `.mediawiki`, `.mkd`, `.mkdn`, `.mustache`, `.njk`, `.nroff`, `.nunjucks`, `.org`, `.pod`, `.pug`, `.rdoc`, `.rng`, `.roff`, `.rss`, `.rst`, `.sgml`, `.shtml`, `.slim`, `.sty`, `.tex`, `.texi`, `.texinfo`, `.textile`, `.troff`, `.twig`, `.typ`, `.wiki`, `.xht`, `.xhtml`, `.xml`, `.xsd`, `.xsl`, `.xslt`

Human-authored markup, template and typesetting SOURCE — Markdown, HTML/XML, reStructuredText, AsciiDoc, LaTeX, and friends.

!!! info "Notes"
    ``xml`` is grouped here as a markup language rather than under ``config-data``.

### `3d-model` — 3D model / mesh

*Parent `file_category`:* `three-d-cad`  
*Extensions (52):* `.3ds`, `.3mf`, `.abc`, `.amf`, `.blend`, `.blend1`, `.c4d`, `.collada`, `.dae`, `.e57`, `.fbx`, `.gco`, `.gcode`, `.glb`, `.gltf`, `.gltf2`, `.ksplat`, `.las`, `.laz`, `.lwo`, `.lws`, `.lxl`, `.lxo`, `.ma`, `.max`, `.mb`, `.mesh`, `.mmd`, `.mqo`, `.mtl`, `.obj`, `.off`, `.pcd`, `.ply`, `.pmd`, `.pmx`, `.qb`, `.splat`, `.stl`, `.usd`, `.usda`, `.usdc`, `.usdz`, `.vox`, `.vrml`, `.wrl`, `.x3d`, `.x3db`, `.x3dv`, `.xyz`, `.zpr`, `.ztl`

3D meshes, scenes and printable models (STL/OBJ/glTF/FBX/3MF/…).

!!! info "Notes"
    ``obj`` is the Wavefront 3D mesh here (not a compiled object file); ``stl`` is stereolithography (not EBU subtitle).

### `cad` — CAD & engineering

*Parent `file_category`:* `three-d-cad`  
*Extensions (58):* `.3dm`, `.3dxml`, `.brd`, `.catdrawing`, `.catpart`, `.catproduct`, `.cnc`, `.dgn`, `.drl`, `.dwf`, `.dwfx`, `.dwg`, `.dwt`, `.dxf`, `.emn`, `.f3d`, `.f3z`, `.fcstd`, `.gbr`, `.iam`, `.idw`, `.ifc`, `.ifcxml`, `.ifczip`, `.iges`, `.igs`, `.ipn`, `.ipt`, `.jt`, `.kicad_pcb`, `.kicad_sch`, `.mcd`, `.model`, `.nc`, `.neu`, `.par`, `.pln`, `.prt`, `.psm`, `.rfa`, `.rte`, `.rvt`, `.sab`, `.sat`, `.scad`, `.scdoc`, `.sch`, `.skp`, `.sldasm`, `.slddrw`, `.sldprt`, `.step`, `.stp`, `.stpz`, `.tap`, `.vwx`, `.x_b`, `.x_t`

Computer-aided-design drawings and engineering interchange formats (DWG/DXF/STEP/IGES/native part & assembly files).

### `font` — Font

*Parent `file_category`:* `system`  
*Extensions (31):* `.afm`, `.bdf`, `.cff`, `.dfont`, `.eot`, `.fnt`, `.fon`, `.fond`, `.fot`, `.gf`, `.glyphs`, `.glyphspackage`, `.otc`, `.otf`, `.pcf`, `.pf2`, `.pfa`, `.pfb`, `.pfm`, `.pk`, `.sfd`, `.snf`, `.suit`, `.t1`, `.tfm`, `.ttc`, `.ttf`, `.ufo`, `.vfb`, `.woff`, `.woff2`

Digital typefaces and font-editor sources (TrueType/OpenType/WOFF/…).

### `source-code` — Source code

*Parent `file_category`:* `development`  
*Extensions (147):* `.ada`, `.adb`, `.ads`, `.asm`, `.au3`, `.bas`, `.c`, `.c++`, `.cairo`, `.cbl`, `.cc`, `.cjs`, `.cl`, `.clj`, `.cljc`, `.cljs`, `.cob`, `.cobol`, `.coffee`, `.comp`, `.cpp`, `.cpy`, `.cr`, `.cs`, `.csx`, `.cts`, `.cu`, `.cuh`, `.cxx`, `.d`, `.dart`, `.dpr`, `.edn`, `.eex`, `.el`, `.elm`, `.erl`, `.ex`, `.f`, `.f03`, `.f08`, `.f90`, `.f95`, `.for`, `.frag`, `.fs`, `.fsi`, `.fsscript`, `.fsx`, `.ftn`, `.gd`, `.gemspec`, `.geom`, `.glsl`, `.go`, `.groovy`, `.gvy`, `.gy`, `.h`, `.h++`, `.heex`, `.hh`, `.hlsl`, `.hpp`, `.hrl`, `.hs`, `.hx`, `.hxml`, `.hxx`, `.i`, `.inc`, `.inl`, `.ino`, `.ipp`, `.jav`, `.java`, `.jl`, `.js`, `.jsx`, `.kt`, `.ktm`, `.kts`, `.leex`, `.lhs`, `.lisp`, `.litcoffee`, `.lsp`, `.lua`, `.m`, `.metal`, `.mjs`, `.ml`, `.mli`, `.mm`, `.move`, `.nasm`, `.nim`, `.nims`, `.pas`, `.php`, `.php3`, `.php4`, `.php5`, `.phps`, `.phtml`, `.pl`, `.pm`, `.pp`, `.purs`, `.pxd`, `.pxi`, `.py`, `.pyi`, `.pyw`, `.pyx`, `.r`, `.rake`, `.rb`, `.rbw`, `.re`, `.rei`, `.rkt`, `.rpy`, `.rs`, `.s`, `.sc`, `.scala`, `.scm`, `.sol`, `.ss`, `.sv`, `.svh`, `.swift`, `.t`, `.tcc`, `.tcl`, `.tpp`, `.tsx`, `.v`, `.vala`, `.vapi`, `.vb`, `.vert`, `.vhdl`, `.wat`, `.wgsl`, `.zig`

Programming-language source files across the common language ecosystems.

!!! info "Notes"
    ``m`` is grouped as source (Objective-C / MATLAB share it); ``sql`` is grouped under ``database``.

### `script` — Shell / automation script

*Parent `file_category`:* `development`  
*Extensions (36):* `.ahk`, `.ahk2`, `.applescript`, `.ash`, `.awk`, `.bash`, `.bat`, `.btm`, `.cgi`, `.cmd`, `.command`, `.csh`, `.dash`, `.elv`, `.exp`, `.expect`, `.fish`, `.hta`, `.ksh`, `.nu`, `.ps1`, `.ps1xml`, `.psd1`, `.psm1`, `.scpt`, `.scptd`, `.sed`, `.sh`, `.tcsh`, `.tool`, `.vbe`, `.vbs`, `.wsf`, `.wsh`, `.xonsh`, `.zsh`

Shell, batch and automation scripts (Bash/PowerShell/Batch/AppleScript/…).

### `web-asset` — Web asset

*Parent `file_category`:* `development`  
*Extensions (14):* `.astro`, `.css`, `.importmap`, `.less`, `.pcss`, `.postcss`, `.sass`, `.scss`, `.styl`, `.stylus`, `.svelte`, `.vue`, `.wasm`, `.webmanifest`

Front-end web build assets — stylesheets and WebAssembly.

!!! info "Notes"
    HTML is grouped under ``markup``; web fonts under ``font``.

### `notebook` — Computational notebook

*Parent `file_category`:* `development`  
*Extensions (8):* `.ipynb`, `.livemd`, `.nb`, `.qmd`, `.rmarkdown`, `.rmd`, `.rnw`, `.zpln`

Literate computational notebooks (Jupyter, R Markdown, Quarto).

### `config-data` — Config & structured data

*Parent `file_category`:* `development`  
*Extensions (67):* `.avsc`, `.babelrc`, `.bazel`, `.bzl`, `.capnp`, `.cfg`, `.cmake`, `.cnf`, `.conf`, `.config`, `.containerfile`, `.csproj`, `.desktop`, `.dhall`, `.dockerfile`, `.dotenv`, `.editorconfig`, `.env`, `.eslintrc`, `.fbs`, `.geojson`, `.gql`, `.gradle`, `.graphql`, `.hcl`, `.ini`, `.json`, `.json5`, `.jsonc`, `.jsonl`, `.jsonnet`, `.kdl`, `.libsonnet`, `.lock`, `.mak`, `.mk`, `.ndjson`, `.ninja`, `.nix`, `.npmrc`, `.pbxproj`, `.plist`, `.prefs`, `.prettierrc`, `.pri`, `.prop`, `.properties`, `.proto`, `.rc`, `.reg`, `.resx`, `.ron`, `.sbt`, `.service`, `.sln`, `.tf`, `.tfstate`, `.tfvars`, `.thrift`, `.toml`, `.topojson`, `.unit`, `.vcxproj`, `.xcconfig`, `.yaml`, `.yarnrc`, `.yml`

Machine-readable configuration and structured-data / serialization files (JSON/YAML/TOML/INI/env/infrastructure-as-code/…).

### `database` — Database & dataset

*Parent `file_category`:* `system`  
*Extensions (54):* `.accdb`, `.accde`, `.arrow`, `.avro`, `.bson`, `.cdf`, `.db`, `.db3`, `.dbf`, `.ddl`, `.dta`, `.dump`, `.fdb`, `.feather`, `.fmp12`, `.fp7`, `.frm`, `.gdb`, `.h5`, `.hdf5`, `.ibd`, `.kdb`, `.kdbx`, `.ldf`, `.mdb`, `.mde`, `.mdf`, `.myd`, `.myi`, `.ndf`, `.npy`, `.npz`, `.nsf`, `.ntf`, `.odb`, `.orc`, `.parquet`, `.pickle`, `.pkl`, `.por`, `.rdata`, `.rds`, `.realm`, `.s3db`, `.sas7bdat`, `.sav`, `.sdf`, `.sl3`, `.sql`, `.sqlite`, `.sqlite3`, `.sqlitedb`, `.wdb`, `.xpt`

On-disk databases, database dumps, and columnar/analytics dataset files (SQLite/Access/SQL dumps/Parquet/Avro/…).

!!! info "Notes"
    ``mdf`` is a SQL Server data file here (not a disc image); ``nsf`` is a Lotus Notes database (not e-mail).

### `archive` — Archive / compressed

*Parent `file_category`:* `archive`  
*Extensions (74):* `.7z`, `.ace`, `.afa`, `.alz`, `.ar`, `.arc`, `.arj`, `.b1`, `.ba`, `.br`, `.bz2`, `.cab`, `.cpio`, `.dar`, `.dgc`, `.ear`, `.gca`, `.gcf`, `.gz`, `.gzip`, `.hqx`, `.jar`, `.kgb`, `.lha`, `.lrz`, `.lz`, `.lz4`, `.lzh`, `.lzma`, `.lzo`, `.lzop`, `.pak`, `.paq`, `.pea`, `.pk3`, `.pk4`, `.r00`, `.r01`, `.rar`, `.rz`, `.s7z`, `.sar`, `.sea`, `.shar`, `.sit`, `.sitx`, `.sz`, `.tar`, `.taz`, `.tb2`, `.tbz`, `.tbz2`, `.tgz`, `.tlz`, `.tlz4`, `.tlzma`, `.tlzo`, `.txz`, `.tzst`, `.uc2`, `.uha`, `.vpk`, `.war`, `.xar`, `.xz`, `.yz1`, `.z`, `.zip`, `.zipx`, `.zoo`, `.zpaq`, `.zst`, `.zstd`, `.zz`

General-purpose archive and compression containers (ZIP/RAR/7z/tar/gz/zst/…), including multi-part ``tar.*`` bundles and generic Java archives.

### `disk-image` — Disk / filesystem image

*Parent `file_category`:* `archive`  
*Extensions (48):* `.adf`, `.adz`, `.aff`, `.b5t`, `.b6t`, `.ccd`, `.chd`, `.cif`, `.cso`, `.d64`, `.daa`, `.dmg`, `.dsk`, `.e01`, `.esd`, `.fdi`, `.ffu`, `.gcm`, `.gho`, `.ghs`, `.gi`, `.hdd`, `.img`, `.iso`, `.isz`, `.mds`, `.nrg`, `.nsp`, `.ova`, `.ovf`, `.qcow`, `.qcow2`, `.qed`, `.sparsebundle`, `.sparseimage`, `.swm`, `.tib`, `.toast`, `.udf`, `.uif`, `.vdi`, `.vfd`, `.vhd`, `.vhdx`, `.vmdk`, `.wbfs`, `.wim`, `.xci`

Whole-disc, filesystem and virtual-machine disk images (ISO/DMG/VHD/VMDK/…).

### `package-installer` — Package / installer

*Parent `file_category`:* `archive`  
*Extensions (38):* `.aab`, `.aar`, `.apk`, `.apkg`, `.apkm`, `.apks`, `.appimage`, `.appx`, `.appxbundle`, `.crx`, `.deb`, `.drpm`, `.egg`, `.eopkg`, `.flatpak`, `.flatpakref`, `.gem`, `.ipa`, `.jmod`, `.mpkg`, `.msi`, `.msix`, `.msixbundle`, `.msp`, `.nupkg`, `.pkg`, `.rpm`, `.run`, `.snap`, `.srpm`, `.tazpkg`, `.tipa`, `.udeb`, `.vsix`, `.whl`, `.xapk`, `.xbps`, `.xpi`

OS, application and language-ecosystem installable packages (deb/rpm/msi/apk/AppImage/wheel/gem/…).

### `executable-binary` — Executable / binary

*Parent `file_category`:* `system`  
*Extensions (37):* `.a`, `.ax`, `.axf`, `.beam`, `.bin`, `.bundle`, `.class`, `.com`, `.cpl`, `.dll`, `.dol`, `.drv`, `.dylib`, `.efi`, `.elf`, `.exe`, `.jsa`, `.ko`, `.lib`, `.node`, `.nro`, `.nso`, `.o`, `.ocx`, `.out`, `.prx`, `.pyc`, `.pyd`, `.pyo`, `.rlib`, `.rmeta`, `.rpx`, `.scr`, `.self`, `.so`, `.sys`, `.xex`

Native executables, shared/static libraries, compiled object code and byte-code.

### `email` — E-mail & mailbox

*Parent `file_category`:* `system`  
*Extensions (17):* `.dbx`, `.eml`, `.emlx`, `.mbox`, `.mbs`, `.mbx`, `.mht`, `.mhtml`, `.mim`, `.mime`, `.msg`, `.nws`, `.oft`, `.ost`, `.p7m`, `.pst`, `.tnef`

Individual messages and mailbox stores (EML/MSG/mbox/PST/…).

### `certificate-key` — Certificate & key

*Parent `file_category`:* `system`  
*Extensions (33):* `.asc`, `.bks`, `.cer`, `.cert`, `.crl`, `.crt`, `.csr`, `.der`, `.gpg`, `.jceks`, `.jks`, `.jwk`, `.jwks`, `.kbx`, `.key`, `.keystore`, `.p12`, `.p7b`, `.p7c`, `.p7r`, `.p7s`, `.p8`, `.pem`, `.pfx`, `.pgp`, `.pk8`, `.pkr`, `.ppk`, `.pub`, `.req`, `.sig`, `.skr`, `.spc`

X.509 certificates, cryptographic keys, keystores and signatures.

!!! info "Notes"
    ``key`` is a cryptographic private key here (not an Apple Keynote deck); ``asc`` is treated as PGP-armored key/signature material.

### `log` — Log & diagnostic

*Parent `file_category`:* `system`  
*Extensions (14):* `.dmp`, `.err`, `.etl`, `.evt`, `.evtx`, `.hprof`, `.journal`, `.log`, `.log1`, `.log2`, `.logs`, `.ltsv`, `.mdmp`, `.trace`

Application/system logs and diagnostic event traces.

### `other` — Other / unknown

*Parent `file_category`:* `other`  
*Extensions (0):* —

No matching group — an unrecognised or absent extension. The bucket ``file_group`` is designed to shrink.

## Summary

| Group | Label | Parent `file_category` | # ext |
| --- | --- | --- | --: |
| `raster-photo` | Raster / photo | `image` | 50 |
| `raw-photo` | Camera RAW | `image` | 39 |
| `vector-image` | Vector image | `image` | 24 |
| `layered-image` | Layered / authoring image | `image` | 20 |
| `animated-image` | Animated image | `image` | 7 |
| `video` | Video | `video` | 52 |
| `audio-lossy` | Lossy audio | `audio` | 26 |
| `audio-lossless` | Lossless / PCM audio | `audio` | 28 |
| `audiobook` | Audiobook | `audio` | 4 |
| `audio-project` | Audio project, sampler & instrument | `audio` | 54 |
| `playlist` | Playlist | `audio` | 15 |
| `subtitle` | Subtitle / caption | `video` | 21 |
| `document-text` | Plain text document | `document` | 10 |
| `document-office` | Word-processor / office document | `document` | 30 |
| `pdf` | PDF & page description | `document` | 8 |
| `presentation` | Presentation | `document` | 21 |
| `spreadsheet` | Spreadsheet / tabular | `document` | 34 |
| `ebook` | E-book | `document` | 29 |
| `comic` | Comic archive | `document` | 7 |
| `markup` | Markup & typesetting source | `document` | 64 |
| `3d-model` | 3D model / mesh | `three-d-cad` | 52 |
| `cad` | CAD & engineering | `three-d-cad` | 58 |
| `font` | Font | `system` | 31 |
| `source-code` | Source code | `development` | 147 |
| `script` | Shell / automation script | `development` | 36 |
| `web-asset` | Web asset | `development` | 14 |
| `notebook` | Computational notebook | `development` | 8 |
| `config-data` | Config & structured data | `development` | 67 |
| `database` | Database & dataset | `system` | 54 |
| `archive` | Archive / compressed | `archive` | 74 |
| `disk-image` | Disk / filesystem image | `archive` | 48 |
| `package-installer` | Package / installer | `archive` | 38 |
| `executable-binary` | Executable / binary | `system` | 37 |
| `email` | E-mail & mailbox | `system` | 17 |
| `certificate-key` | Certificate & key | `system` | 33 |
| `log` | Log & diagnostic | `system` | 14 |
| `other` | Other / unknown | `other` | 0 |

## Extension index

Every mapped extension, alphabetically, with its group.

| Extension | Group |
| --- | --- |
| `.123` | `spreadsheet` |
| `.1st` | `document-text` |
| `.3dm` | `cad` |
| `.3ds` | `3d-model` |
| `.3dxml` | `cad` |
| `.3fr` | `raw-photo` |
| `.3g2` | `video` |
| `.3ga` | `audio-lossy` |
| `.3gp` | `video` |
| `.3gpp` | `video` |
| `.3mf` | `3d-model` |
| `.602` | `document-office` |
| `.7z` | `archive` |
| `.a` | `executable-binary` |
| `.aa` | `audiobook` |
| `.aab` | `package-installer` |
| `.aac` | `audio-lossy` |
| `.aar` | `package-installer` |
| `.aax` | `audiobook` |
| `.aaxc` | `audiobook` |
| `.abc` | `3d-model` |
| `.abw` | `document-office` |
| `.ac3` | `audio-lossy` |
| `.acbf` | `comic` |
| `.accdb` | `database` |
| `.accde` | `database` |
| `.ace` | `archive` |
| `.acsm` | `ebook` |
| `.ada` | `source-code` |
| `.adb` | `source-code` |
| `.adf` | `disk-image` |
| `.adg` | `audio-project` |
| `.adoc` | `markup` |
| `.ads` | `source-code` |
| `.adv` | `audio-project` |
| `.adz` | `disk-image` |
| `.afa` | `archive` |
| `.afdesign` | `layered-image` |
| `.aff` | `disk-image` |
| `.afm` | `font` |
| `.afphoto` | `layered-image` |
| `.agr` | `audio-project` |
| `.ahk` | `script` |
| `.ahk2` | `script` |
| `.ai` | `vector-image` |
| `.aif` | `audio-lossless` |
| `.aifc` | `audio-lossless` |
| `.aiff` | `audio-lossless` |
| `.aimppl` | `playlist` |
| `.akp` | `audio-project` |
| `.alac` | `audio-lossless` |
| `.alp` | `audio-project` |
| `.als` | `audio-project` |
| `.alz` | `archive` |
| `.amf` | `3d-model` |
| `.amr` | `audio-lossy` |
| `.amv` | `video` |
| `.ani` | `animated-image` |
| `.ans` | `document-text` |
| `.ape` | `audio-lossless` |
| `.apk` | `package-installer` |
| `.apkg` | `package-installer` |
| `.apkm` | `package-installer` |
| `.apks` | `package-installer` |
| `.apng` | `animated-image` |
| `.appimage` | `package-installer` |
| `.applescript` | `script` |
| `.appx` | `package-installer` |
| `.appxbundle` | `package-installer` |
| `.aqt` | `subtitle` |
| `.ar` | `archive` |
| `.arc` | `archive` |
| `.ari` | `raw-photo` |
| `.arj` | `archive` |
| `.arrow` | `database` |
| `.arw` | `raw-photo` |
| `.asc` | `certificate-key` |
| `.asciidoc` | `markup` |
| `.asf` | `video` |
| `.ash` | `script` |
| `.asm` | `source-code` |
| `.ass` | `subtitle` |
| `.astro` | `web-asset` |
| `.asx` | `playlist` |
| `.atom` | `markup` |
| `.au` | `audio-lossless` |
| `.au3` | `source-code` |
| `.aup` | `audio-project` |
| `.aup3` | `audio-project` |
| `.aupreset` | `audio-project` |
| `.av1` | `video` |
| `.avi` | `video` |
| `.avif` | `raster-photo` |
| `.avifs` | `raster-photo` |
| `.avro` | `database` |
| `.avsc` | `config-data` |
| `.awb` | `audio-lossy` |
| `.awk` | `script` |
| `.ax` | `executable-binary` |
| `.axf` | `executable-binary` |
| `.azw` | `ebook` |
| `.azw3` | `ebook` |
| `.azw4` | `ebook` |
| `.b1` | `archive` |
| `.b4s` | `playlist` |
| `.b5t` | `disk-image` |
| `.b6t` | `disk-image` |
| `.ba` | `archive` |
| `.babelrc` | `config-data` |
| `.bas` | `source-code` |
| `.bash` | `script` |
| `.bat` | `script` |
| `.bay` | `raw-photo` |
| `.bazel` | `config-data` |
| `.bdf` | `font` |
| `.beam` | `executable-binary` |
| `.bib` | `markup` |
| `.bin` | `executable-binary` |
| `.bks` | `certificate-key` |
| `.blend` | `3d-model` |
| `.blend1` | `3d-model` |
| `.bmp` | `raster-photo` |
| `.bpg` | `raster-photo` |
| `.br` | `archive` |
| `.braw` | `video` |
| `.brd` | `cad` |
| `.bson` | `database` |
| `.bst` | `markup` |
| `.btm` | `script` |
| `.bundle` | `executable-binary` |
| `.bwf` | `audio-lossless` |
| `.bwproject` | `audio-project` |
| `.bz2` | `archive` |
| `.bzl` | `config-data` |
| `.c` | `source-code` |
| `.c++` | `source-code` |
| `.c4d` | `3d-model` |
| `.cab` | `archive` |
| `.caf` | `audio-lossless` |
| `.cairo` | `source-code` |
| `.cap` | `raw-photo` |
| `.capnp` | `config-data` |
| `.catdrawing` | `cad` |
| `.catpart` | `cad` |
| `.catproduct` | `cad` |
| `.cb7` | `comic` |
| `.cba` | `comic` |
| `.cbl` | `source-code` |
| `.cbr` | `comic` |
| `.cbt` | `comic` |
| `.cbw` | `comic` |
| `.cbz` | `comic` |
| `.cc` | `source-code` |
| `.ccd` | `disk-image` |
| `.cdf` | `database` |
| `.cdr` | `vector-image` |
| `.ceb` | `ebook` |
| `.cebx` | `ebook` |
| `.cer` | `certificate-key` |
| `.cert` | `certificate-key` |
| `.cff` | `font` |
| `.cfg` | `config-data` |
| `.cgi` | `script` |
| `.cgm` | `vector-image` |
| `.chd` | `disk-image` |
| `.chm` | `ebook` |
| `.cif` | `disk-image` |
| `.cjs` | `source-code` |
| `.cl` | `source-code` |
| `.class` | `executable-binary` |
| `.clip` | `layered-image` |
| `.clj` | `source-code` |
| `.cljc` | `source-code` |
| `.cljs` | `source-code` |
| `.cls` | `markup` |
| `.cmake` | `config-data` |
| `.cmd` | `script` |
| `.cmx` | `vector-image` |
| `.cnc` | `cad` |
| `.cnf` | `config-data` |
| `.cob` | `source-code` |
| `.cobol` | `source-code` |
| `.coffee` | `source-code` |
| `.collada` | `3d-model` |
| `.com` | `executable-binary` |
| `.command` | `script` |
| `.comp` | `source-code` |
| `.conf` | `config-data` |
| `.config` | `config-data` |
| `.containerfile` | `config-data` |
| `.cpio` | `archive` |
| `.cpl` | `executable-binary` |
| `.cpp` | `source-code` |
| `.cpr` | `audio-project` |
| `.cpt` | `layered-image` |
| `.cpy` | `source-code` |
| `.cr` | `source-code` |
| `.cr2` | `raw-photo` |
| `.cr3` | `raw-photo` |
| `.creole` | `markup` |
| `.crl` | `certificate-key` |
| `.crt` | `certificate-key` |
| `.crw` | `raw-photo` |
| `.crx` | `package-installer` |
| `.cs` | `source-code` |
| `.cs1` | `raw-photo` |
| `.csh` | `script` |
| `.cso` | `disk-image` |
| `.csp` | `layered-image` |
| `.csproj` | `config-data` |
| `.csr` | `certificate-key` |
| `.css` | `web-asset` |
| `.csv` | `spreadsheet` |
| `.csx` | `source-code` |
| `.cts` | `source-code` |
| `.cu` | `source-code` |
| `.cue` | `playlist` |
| `.cuh` | `source-code` |
| `.cur` | `raster-photo` |
| `.cwb` | `audio-project` |
| `.cwk` | `document-office` |
| `.cwp` | `audio-project` |
| `.cxx` | `source-code` |
| `.d` | `source-code` |
| `.d64` | `disk-image` |
| `.daa` | `disk-image` |
| `.dae` | `3d-model` |
| `.dar` | `archive` |
| `.dart` | `source-code` |
| `.dash` | `script` |
| `.dav` | `video` |
| `.dawproject` | `audio-project` |
| `.db` | `database` |
| `.db3` | `database` |
| `.dbf` | `database` |
| `.dbx` | `email` |
| `.dcr` | `raw-photo` |
| `.dcs` | `raw-photo` |
| `.ddl` | `database` |
| `.dds` | `raster-photo` |
| `.deb` | `package-installer` |
| `.der` | `certificate-key` |
| `.desktop` | `config-data` |
| `.dff` | `audio-lossless` |
| `.dfont` | `font` |
| `.dfxp` | `subtitle` |
| `.dgc` | `archive` |
| `.dgn` | `cad` |
| `.dhall` | `config-data` |
| `.dia` | `vector-image` |
| `.dib` | `raster-photo` |
| `.dif` | `spreadsheet` |
| `.dita` | `markup` |
| `.divx` | `video` |
| `.diz` | `document-text` |
| `.djv` | `ebook` |
| `.djvu` | `ebook` |
| `.dll` | `executable-binary` |
| `.dls` | `audio-project` |
| `.dmg` | `disk-image` |
| `.dmp` | `log` |
| `.dng` | `raw-photo` |
| `.doc` | `document-office` |
| `.docbook` | `markup` |
| `.dockerfile` | `config-data` |
| `.docm` | `document-office` |
| `.docx` | `document-office` |
| `.dol` | `executable-binary` |
| `.dot` | `document-office` |
| `.dotenv` | `config-data` |
| `.dotm` | `document-office` |
| `.dotx` | `document-office` |
| `.dpr` | `source-code` |
| `.drawio` | `vector-image` |
| `.drf` | `raw-photo` |
| `.drl` | `cad` |
| `.drpm` | `package-installer` |
| `.drv` | `executable-binary` |
| `.drw` | `vector-image` |
| `.dsd` | `audio-lossless` |
| `.dsf` | `audio-lossless` |
| `.dsk` | `disk-image` |
| `.dta` | `database` |
| `.dtd` | `markup` |
| `.dts` | `audio-lossy` |
| `.dtx` | `markup` |
| `.dump` | `database` |
| `.dv` | `video` |
| `.dvr-ms` | `video` |
| `.dwf` | `cad` |
| `.dwfx` | `cad` |
| `.dwg` | `cad` |
| `.dwt` | `cad` |
| `.dxf` | `cad` |
| `.dylib` | `executable-binary` |
| `.e01` | `disk-image` |
| `.e57` | `3d-model` |
| `.eac3` | `audio-lossy` |
| `.ear` | `archive` |
| `.editorconfig` | `config-data` |
| `.edn` | `source-code` |
| `.eex` | `source-code` |
| `.efi` | `executable-binary` |
| `.egg` | `package-installer` |
| `.eip` | `raw-photo` |
| `.ejs` | `markup` |
| `.el` | `source-code` |
| `.elf` | `executable-binary` |
| `.elm` | `source-code` |
| `.elv` | `script` |
| `.emf` | `vector-image` |
| `.eml` | `email` |
| `.emlx` | `email` |
| `.emn` | `cad` |
| `.emz` | `vector-image` |
| `.env` | `config-data` |
| `.eopkg` | `package-installer` |
| `.eot` | `font` |
| `.eps` | `vector-image` |
| `.epsf` | `vector-image` |
| `.epsi` | `vector-image` |
| `.epub` | `ebook` |
| `.erb` | `markup` |
| `.erf` | `raw-photo` |
| `.erl` | `source-code` |
| `.err` | `log` |
| `.esd` | `disk-image` |
| `.eslintrc` | `config-data` |
| `.et` | `spreadsheet` |
| `.etl` | `log` |
| `.etx` | `document-text` |
| `.evt` | `log` |
| `.evtx` | `log` |
| `.ex` | `source-code` |
| `.exe` | `executable-binary` |
| `.exp` | `script` |
| `.expect` | `script` |
| `.exr` | `raster-photo` |
| `.exs` | `audio-project` |
| `.f` | `source-code` |
| `.f03` | `source-code` |
| `.f08` | `source-code` |
| `.f3d` | `cad` |
| `.f3z` | `cad` |
| `.f4v` | `video` |
| `.f90` | `source-code` |
| `.f95` | `source-code` |
| `.fb2` | `ebook` |
| `.fb2z` | `ebook` |
| `.fbs` | `config-data` |
| `.fbx` | `3d-model` |
| `.fbz` | `ebook` |
| `.fcstd` | `cad` |
| `.fdb` | `database` |
| `.fdf` | `pdf` |
| `.fdi` | `disk-image` |
| `.feather` | `database` |
| `.fff` | `raw-photo` |
| `.ffp` | `audio-project` |
| `.ffu` | `disk-image` |
| `.fig` | `vector-image` |
| `.fish` | `script` |
| `.fit` | `raster-photo` |
| `.fits` | `raster-photo` |
| `.flac` | `audio-lossless` |
| `.flatpak` | `package-installer` |
| `.flatpakref` | `package-installer` |
| `.flc` | `animated-image` |
| `.fli` | `animated-image` |
| `.flif` | `raster-photo` |
| `.flp` | `audio-project` |
| `.flv` | `video` |
| `.fmp12` | `database` |
| `.fnt` | `font` |
| `.fodg` | `vector-image` |
| `.fodp` | `presentation` |
| `.fods` | `spreadsheet` |
| `.fodt` | `document-office` |
| `.fon` | `font` |
| `.fond` | `font` |
| `.for` | `source-code` |
| `.fot` | `font` |
| `.fp7` | `database` |
| `.fpl` | `playlist` |
| `.frag` | `source-code` |
| `.frm` | `database` |
| `.fs` | `source-code` |
| `.fsi` | `source-code` |
| `.fsscript` | `source-code` |
| `.fsx` | `source-code` |
| `.ftn` | `source-code` |
| `.fxb` | `audio-project` |
| `.fxp` | `audio-project` |
| `.gbr` | `cad` |
| `.gca` | `archive` |
| `.gcf` | `archive` |
| `.gcm` | `disk-image` |
| `.gco` | `3d-model` |
| `.gcode` | `3d-model` |
| `.gd` | `source-code` |
| `.gdb` | `database` |
| `.gdoc` | `document-office` |
| `.gem` | `package-installer` |
| `.gemspec` | `source-code` |
| `.geojson` | `config-data` |
| `.geom` | `source-code` |
| `.gf` | `font` |
| `.gho` | `disk-image` |
| `.ghs` | `disk-image` |
| `.gi` | `disk-image` |
| `.gif` | `animated-image` |
| `.gifv` | `animated-image` |
| `.gig` | `audio-project` |
| `.glb` | `3d-model` |
| `.glsl` | `source-code` |
| `.gltf` | `3d-model` |
| `.gltf2` | `3d-model` |
| `.glyphs` | `font` |
| `.glyphspackage` | `font` |
| `.gnumeric` | `spreadsheet` |
| `.go` | `source-code` |
| `.gpg` | `certificate-key` |
| `.gpr` | `raw-photo` |
| `.gql` | `config-data` |
| `.gradle` | `config-data` |
| `.graphql` | `config-data` |
| `.groovy` | `source-code` |
| `.gsheet` | `spreadsheet` |
| `.gslides` | `presentation` |
| `.gsm` | `audio-lossy` |
| `.gvy` | `source-code` |
| `.gxf` | `video` |
| `.gy` | `source-code` |
| `.gz` | `archive` |
| `.gzip` | `archive` |
| `.h` | `source-code` |
| `.h++` | `source-code` |
| `.h264` | `video` |
| `.h265` | `video` |
| `.h2song` | `audio-project` |
| `.h5` | `database` |
| `.haml` | `markup` |
| `.handlebars` | `markup` |
| `.hbs` | `markup` |
| `.hcl` | `config-data` |
| `.hdd` | `disk-image` |
| `.hdf5` | `database` |
| `.hdr` | `raster-photo` |
| `.heex` | `source-code` |
| `.heic` | `raster-photo` |
| `.heics` | `raster-photo` |
| `.heif` | `raster-photo` |
| `.hevc` | `video` |
| `.hh` | `source-code` |
| `.hif` | `raster-photo` |
| `.hlsl` | `source-code` |
| `.hpgl` | `vector-image` |
| `.hpp` | `source-code` |
| `.hprof` | `log` |
| `.hqx` | `archive` |
| `.hrl` | `source-code` |
| `.hs` | `source-code` |
| `.hta` | `script` |
| `.htm` | `markup` |
| `.html` | `markup` |
| `.hwp` | `document-office` |
| `.hwpx` | `document-office` |
| `.hx` | `source-code` |
| `.hxml` | `source-code` |
| `.hxx` | `source-code` |
| `.i` | `source-code` |
| `.iam` | `cad` |
| `.ibd` | `database` |
| `.ibooks` | `ebook` |
| `.ico` | `raster-photo` |
| `.idml` | `layered-image` |
| `.idw` | `cad` |
| `.idx` | `subtitle` |
| `.ifc` | `cad` |
| `.ifcxml` | `cad` |
| `.ifczip` | `cad` |
| `.ifo` | `video` |
| `.iges` | `cad` |
| `.igs` | `cad` |
| `.iiq` | `raw-photo` |
| `.img` | `disk-image` |
| `.importmap` | `web-asset` |
| `.inc` | `source-code` |
| `.indd` | `layered-image` |
| `.ini` | `config-data` |
| `.inl` | `source-code` |
| `.ino` | `source-code` |
| `.ipa` | `package-installer` |
| `.ipn` | `cad` |
| `.ipp` | `source-code` |
| `.ipt` | `cad` |
| `.ipynb` | `notebook` |
| `.iso` | `disk-image` |
| `.isz` | `disk-image` |
| `.j2` | `markup` |
| `.j2k` | `raster-photo` |
| `.jade` | `markup` |
| `.jar` | `archive` |
| `.jav` | `source-code` |
| `.java` | `source-code` |
| `.jceks` | `certificate-key` |
| `.jfif` | `raster-photo` |
| `.jif` | `raster-photo` |
| `.jinja` | `markup` |
| `.jinja2` | `markup` |
| `.jks` | `certificate-key` |
| `.jl` | `source-code` |
| `.jmod` | `package-installer` |
| `.jng` | `raster-photo` |
| `.journal` | `log` |
| `.jp2` | `raster-photo` |
| `.jpe` | `raster-photo` |
| `.jpeg` | `raster-photo` |
| `.jpf` | `raster-photo` |
| `.jpg` | `raster-photo` |
| `.jpm` | `raster-photo` |
| `.jpx` | `raster-photo` |
| `.js` | `source-code` |
| `.jsa` | `executable-binary` |
| `.json` | `config-data` |
| `.json5` | `config-data` |
| `.jsonc` | `config-data` |
| `.jsonl` | `config-data` |
| `.jsonnet` | `config-data` |
| `.jss` | `subtitle` |
| `.jsx` | `source-code` |
| `.jt` | `cad` |
| `.jwk` | `certificate-key` |
| `.jwks` | `certificate-key` |
| `.jxl` | `raster-photo` |
| `.k25` | `raw-photo` |
| `.kbx` | `certificate-key` |
| `.kc2` | `raw-photo` |
| `.kdb` | `database` |
| `.kdbx` | `database` |
| `.kdc` | `raw-photo` |
| `.kdl` | `config-data` |
| `.key` | `certificate-key` |
| `.keystore` | `certificate-key` |
| `.kf8` | `ebook` |
| `.kfx` | `ebook` |
| `.kgb` | `archive` |
| `.kicad_pcb` | `cad` |
| `.kicad_sch` | `cad` |
| `.kit` | `audio-project` |
| `.ko` | `executable-binary` |
| `.kpf` | `ebook` |
| `.kpl` | `playlist` |
| `.kra` | `layered-image` |
| `.ksh` | `script` |
| `.ksplat` | `3d-model` |
| `.kt` | `source-code` |
| `.ktm` | `source-code` |
| `.kts` | `source-code` |
| `.kwd` | `document-office` |
| `.l16` | `audio-lossless` |
| `.la` | `audio-lossless` |
| `.las` | `3d-model` |
| `.latex` | `markup` |
| `.laz` | `3d-model` |
| `.ldf` | `database` |
| `.leex` | `source-code` |
| `.less` | `web-asset` |
| `.lha` | `archive` |
| `.lhs` | `source-code` |
| `.lib` | `executable-binary` |
| `.libsonnet` | `config-data` |
| `.liquid` | `markup` |
| `.lisp` | `source-code` |
| `.lit` | `ebook` |
| `.litcoffee` | `source-code` |
| `.livemd` | `notebook` |
| `.lock` | `config-data` |
| `.log` | `log` |
| `.log1` | `log` |
| `.log2` | `log` |
| `.logic` | `audio-project` |
| `.logicx` | `audio-project` |
| `.logs` | `log` |
| `.lrc` | `subtitle` |
| `.lrf` | `ebook` |
| `.lrx` | `ebook` |
| `.lrz` | `archive` |
| `.lsp` | `source-code` |
| `.ltsv` | `log` |
| `.ltx` | `markup` |
| `.lua` | `source-code` |
| `.lwo` | `3d-model` |
| `.lwp` | `document-office` |
| `.lws` | `3d-model` |
| `.lxl` | `3d-model` |
| `.lxo` | `3d-model` |
| `.lz` | `archive` |
| `.lz4` | `archive` |
| `.lzh` | `archive` |
| `.lzma` | `archive` |
| `.lzo` | `archive` |
| `.lzop` | `archive` |
| `.m` | `source-code` |
| `.m1v` | `video` |
| `.m2t` | `video` |
| `.m2ts` | `video` |
| `.m2v` | `video` |
| `.m3u` | `playlist` |
| `.m3u8` | `playlist` |
| `.m4a` | `audio-lossy` |
| `.m4b` | `audiobook` |
| `.m4r` | `audio-lossy` |
| `.m4s` | `video` |
| `.m4v` | `video` |
| `.ma` | `3d-model` |
| `.mak` | `config-data` |
| `.man` | `markup` |
| `.markdown` | `markup` |
| `.max` | `3d-model` |
| `.mb` | `3d-model` |
| `.mbox` | `email` |
| `.mbs` | `email` |
| `.mbx` | `email` |
| `.mcc` | `subtitle` |
| `.mcd` | `cad` |
| `.mcw` | `document-office` |
| `.md` | `markup` |
| `.mdb` | `database` |
| `.mdc` | `raw-photo` |
| `.mde` | `database` |
| `.mdf` | `database` |
| `.mdmp` | `log` |
| `.mdown` | `markup` |
| `.mds` | `disk-image` |
| `.mdwn` | `markup` |
| `.mdx` | `markup` |
| `.me` | `document-text` |
| `.mediawiki` | `markup` |
| `.mef` | `raw-photo` |
| `.mesh` | `3d-model` |
| `.metal` | `source-code` |
| `.mht` | `email` |
| `.mhtml` | `email` |
| `.mim` | `email` |
| `.mime` | `email` |
| `.mjs` | `source-code` |
| `.mk` | `config-data` |
| `.mka` | `audio-lossy` |
| `.mkd` | `markup` |
| `.mkdn` | `markup` |
| `.mkv` | `video` |
| `.ml` | `source-code` |
| `.mli` | `source-code` |
| `.mlp` | `audio-lossless` |
| `.mm` | `source-code` |
| `.mmd` | `3d-model` |
| `.mmp` | `audio-project` |
| `.mmpz` | `audio-project` |
| `.mng` | `animated-image` |
| `.mobi` | `ebook` |
| `.mod` | `video` |
| `.model` | `cad` |
| `.mos` | `raw-photo` |
| `.mov` | `video` |
| `.move` | `source-code` |
| `.mp1` | `audio-lossy` |
| `.mp2` | `audio-lossy` |
| `.mp3` | `audio-lossy` |
| `.mp4` | `video` |
| `.mpa` | `audio-lossy` |
| `.mpc` | `audio-lossy` |
| `.mpe` | `video` |
| `.mpeg` | `video` |
| `.mpg` | `video` |
| `.mpkg` | `package-installer` |
| `.mpsub` | `subtitle` |
| `.mpv` | `video` |
| `.mqo` | `3d-model` |
| `.mrw` | `raw-photo` |
| `.msg` | `email` |
| `.msi` | `package-installer` |
| `.msix` | `package-installer` |
| `.msixbundle` | `package-installer` |
| `.msp` | `package-installer` |
| `.mtl` | `3d-model` |
| `.mts` | `video` |
| `.mustache` | `markup` |
| `.mxf` | `video` |
| `.myd` | `database` |
| `.myi` | `database` |
| `.nasm` | `source-code` |
| `.nb` | `notebook` |
| `.nc` | `cad` |
| `.ncx` | `ebook` |
| `.ndf` | `database` |
| `.ndjson` | `config-data` |
| `.nef` | `raw-photo` |
| `.neu` | `cad` |
| `.nfo` | `document-text` |
| `.nim` | `source-code` |
| `.nims` | `source-code` |
| `.ninja` | `config-data` |
| `.nix` | `config-data` |
| `.njk` | `markup` |
| `.nkc` | `audio-project` |
| `.nki` | `audio-project` |
| `.nkm` | `audio-project` |
| `.nksc` | `raw-photo` |
| `.nksf` | `audio-project` |
| `.nksn` | `audio-project` |
| `.nkx` | `audio-project` |
| `.node` | `executable-binary` |
| `.npmrc` | `config-data` |
| `.npr` | `audio-project` |
| `.npy` | `database` |
| `.npz` | `database` |
| `.nrg` | `disk-image` |
| `.nro` | `executable-binary` |
| `.nroff` | `markup` |
| `.nrw` | `raw-photo` |
| `.nsf` | `database` |
| `.nso` | `executable-binary` |
| `.nsp` | `disk-image` |
| `.nsv` | `video` |
| `.ntf` | `database` |
| `.nu` | `script` |
| `.numbers` | `spreadsheet` |
| `.nunjucks` | `markup` |
| `.nupkg` | `package-installer` |
| `.nws` | `email` |
| `.o` | `executable-binary` |
| `.obj` | `3d-model` |
| `.ocx` | `executable-binary` |
| `.odb` | `database` |
| `.odg` | `vector-image` |
| `.odp` | `presentation` |
| `.ods` | `spreadsheet` |
| `.odt` | `document-office` |
| `.oeb` | `ebook` |
| `.off` | `3d-model` |
| `.ofr` | `audio-lossless` |
| `.ofs` | `audio-lossless` |
| `.oft` | `email` |
| `.oga` | `audio-lossy` |
| `.ogg` | `audio-lossy` |
| `.ogm` | `video` |
| `.ogv` | `video` |
| `.opf` | `ebook` |
| `.opus` | `audio-lossy` |
| `.ora` | `layered-image` |
| `.orc` | `database` |
| `.orf` | `raw-photo` |
| `.org` | `markup` |
| `.ost` | `email` |
| `.otc` | `font` |
| `.otf` | `font` |
| `.otp` | `presentation` |
| `.ots` | `spreadsheet` |
| `.ott` | `document-office` |
| `.out` | `executable-binary` |
| `.ova` | `disk-image` |
| `.ovf` | `disk-image` |
| `.oxps` | `pdf` |
| `.p12` | `certificate-key` |
| `.p7b` | `certificate-key` |
| `.p7c` | `certificate-key` |
| `.p7m` | `email` |
| `.p7r` | `certificate-key` |
| `.p7s` | `certificate-key` |
| `.p8` | `certificate-key` |
| `.pages` | `document-office` |
| `.pak` | `archive` |
| `.pam` | `raster-photo` |
| `.paq` | `archive` |
| `.par` | `cad` |
| `.parquet` | `database` |
| `.pas` | `source-code` |
| `.pat` | `audio-project` |
| `.pbm` | `raster-photo` |
| `.pbxproj` | `config-data` |
| `.pcd` | `3d-model` |
| `.pcf` | `font` |
| `.pcm` | `audio-lossless` |
| `.pcss` | `web-asset` |
| `.pct` | `raster-photo` |
| `.pcx` | `raster-photo` |
| `.pdb` | `ebook` |
| `.pdd` | `layered-image` |
| `.pdf` | `pdf` |
| `.pdn` | `layered-image` |
| `.pea` | `archive` |
| `.pef` | `raw-photo` |
| `.pem` | `certificate-key` |
| `.pf2` | `font` |
| `.pfa` | `font` |
| `.pfb` | `font` |
| `.pfm` | `font` |
| `.pfx` | `certificate-key` |
| `.pgm` | `raster-photo` |
| `.pgp` | `certificate-key` |
| `.php` | `source-code` |
| `.php3` | `source-code` |
| `.php4` | `source-code` |
| `.php5` | `source-code` |
| `.phps` | `source-code` |
| `.phtml` | `source-code` |
| `.pickle` | `database` |
| `.pict` | `raster-photo` |
| `.pjs` | `subtitle` |
| `.pk` | `font` |
| `.pk3` | `archive` |
| `.pk4` | `archive` |
| `.pk8` | `certificate-key` |
| `.pkg` | `package-installer` |
| `.pkl` | `database` |
| `.pkr` | `certificate-key` |
| `.pl` | `source-code` |
| `.pla` | `playlist` |
| `.plist` | `config-data` |
| `.pln` | `cad` |
| `.pls` | `playlist` |
| `.plt` | `vector-image` |
| `.ply` | `3d-model` |
| `.pm` | `source-code` |
| `.pmd` | `3d-model` |
| `.pmx` | `3d-model` |
| `.png` | `raster-photo` |
| `.pnm` | `raster-photo` |
| `.pod` | `markup` |
| `.por` | `database` |
| `.postcss` | `web-asset` |
| `.pot` | `presentation` |
| `.potm` | `presentation` |
| `.potx` | `presentation` |
| `.pp` | `source-code` |
| `.ppk` | `certificate-key` |
| `.ppm` | `raster-photo` |
| `.pps` | `presentation` |
| `.ppsm` | `presentation` |
| `.ppsx` | `presentation` |
| `.ppt` | `presentation` |
| `.pptm` | `presentation` |
| `.pptx` | `presentation` |
| `.prc` | `ebook` |
| `.prefs` | `config-data` |
| `.prettierrc` | `config-data` |
| `.pri` | `config-data` |
| `.prn` | `pdf` |
| `.procreate` | `layered-image` |
| `.prop` | `config-data` |
| `.properties` | `config-data` |
| `.proto` | `config-data` |
| `.prt` | `cad` |
| `.prx` | `executable-binary` |
| `.prz` | `presentation` |
| `.ps` | `pdf` |
| `.ps1` | `script` |
| `.ps1xml` | `script` |
| `.psb` | `layered-image` |
| `.psd` | `layered-image` |
| `.psd1` | `script` |
| `.psm` | `cad` |
| `.psm1` | `script` |
| `.pst` | `email` |
| `.ptf` | `audio-project` |
| `.pts` | `audio-project` |
| `.ptx` | `audio-project` |
| `.pub` | `certificate-key` |
| `.pug` | `markup` |
| `.purs` | `source-code` |
| `.pxd` | `source-code` |
| `.pxi` | `source-code` |
| `.pxm` | `layered-image` |
| `.py` | `source-code` |
| `.pyc` | `executable-binary` |
| `.pyd` | `executable-binary` |
| `.pyi` | `source-code` |
| `.pyo` | `executable-binary` |
| `.pyw` | `source-code` |
| `.pyx` | `source-code` |
| `.qb` | `3d-model` |
| `.qcow` | `disk-image` |
| `.qcow2` | `disk-image` |
| `.qcp` | `audio-lossy` |
| `.qed` | `disk-image` |
| `.qmd` | `notebook` |
| `.qoi` | `raster-photo` |
| `.qpw` | `spreadsheet` |
| `.qt` | `video` |
| `.r` | `source-code` |
| `.r00` | `archive` |
| `.r01` | `archive` |
| `.r3d` | `video` |
| `.ra` | `audio-lossy` |
| `.raf` | `raw-photo` |
| `.rake` | `source-code` |
| `.ram` | `audio-lossy` |
| `.rar` | `archive` |
| `.ras` | `raster-photo` |
| `.raw` | `raw-photo` |
| `.rb` | `source-code` |
| `.rbw` | `source-code` |
| `.rc` | `config-data` |
| `.rcy` | `audio-project` |
| `.rdata` | `database` |
| `.rdoc` | `markup` |
| `.rds` | `database` |
| `.re` | `source-code` |
| `.readme` | `document-text` |
| `.realm` | `database` |
| `.reapeaks` | `audio-project` |
| `.reg` | `config-data` |
| `.rei` | `source-code` |
| `.req` | `certificate-key` |
| `.resx` | `config-data` |
| `.rex` | `audio-project` |
| `.rf64` | `audio-lossless` |
| `.rfa` | `cad` |
| `.rgb` | `raster-photo` |
| `.rkt` | `source-code` |
| `.rlib` | `executable-binary` |
| `.rm` | `video` |
| `.rmarkdown` | `notebook` |
| `.rmd` | `notebook` |
| `.rmeta` | `executable-binary` |
| `.rmvb` | `video` |
| `.rng` | `markup` |
| `.rns` | `audio-project` |
| `.rnw` | `notebook` |
| `.roff` | `markup` |
| `.ron` | `config-data` |
| `.roq` | `video` |
| `.rpm` | `package-installer` |
| `.rpp` | `audio-project` |
| `.rpx` | `executable-binary` |
| `.rpy` | `source-code` |
| `.rs` | `source-code` |
| `.rss` | `markup` |
| `.rst` | `markup` |
| `.rt` | `subtitle` |
| `.rte` | `cad` |
| `.rtf` | `document-office` |
| `.run` | `package-installer` |
| `.rvt` | `cad` |
| `.rw2` | `raw-photo` |
| `.rwl` | `raw-photo` |
| `.rwz` | `raw-photo` |
| `.rx2` | `audio-project` |
| `.rz` | `archive` |
| `.s` | `source-code` |
| `.s3db` | `database` |
| `.s7z` | `archive` |
| `.sab` | `cad` |
| `.sai` | `layered-image` |
| `.sai2` | `layered-image` |
| `.sami` | `subtitle` |
| `.sar` | `archive` |
| `.sas7bdat` | `database` |
| `.sass` | `web-asset` |
| `.sat` | `cad` |
| `.sav` | `database` |
| `.sbt` | `config-data` |
| `.sbv` | `subtitle` |
| `.sc` | `source-code` |
| `.scad` | `cad` |
| `.scala` | `source-code` |
| `.scc` | `subtitle` |
| `.scdoc` | `cad` |
| `.sch` | `cad` |
| `.scm` | `source-code` |
| `.scpt` | `script` |
| `.scptd` | `script` |
| `.scr` | `executable-binary` |
| `.scss` | `web-asset` |
| `.sdd` | `presentation` |
| `.sdf` | `database` |
| `.sdw` | `document-office` |
| `.sea` | `archive` |
| `.sed` | `script` |
| `.self` | `executable-binary` |
| `.service` | `config-data` |
| `.ses` | `audio-project` |
| `.sesx` | `audio-project` |
| `.sf2` | `audio-project` |
| `.sf3` | `audio-project` |
| `.sfark` | `audio-project` |
| `.sfd` | `font` |
| `.sfz` | `audio-project` |
| `.sgi` | `raster-photo` |
| `.sgml` | `markup` |
| `.sh` | `script` |
| `.shar` | `archive` |
| `.shn` | `audio-lossless` |
| `.shtml` | `markup` |
| `.shw` | `presentation` |
| `.sig` | `certificate-key` |
| `.sit` | `archive` |
| `.sitx` | `archive` |
| `.sketch` | `layered-image` |
| `.skp` | `cad` |
| `.skr` | `certificate-key` |
| `.sl3` | `database` |
| `.sldasm` | `cad` |
| `.slddrw` | `cad` |
| `.sldm` | `presentation` |
| `.sldprt` | `cad` |
| `.sldx` | `presentation` |
| `.slim` | `markup` |
| `.slk` | `spreadsheet` |
| `.sln` | `config-data` |
| `.smi` | `subtitle` |
| `.snap` | `package-installer` |
| `.snb` | `ebook` |
| `.snd` | `audio-lossless` |
| `.snf` | `font` |
| `.sng` | `audio-project` |
| `.so` | `executable-binary` |
| `.sol` | `source-code` |
| `.sparsebundle` | `disk-image` |
| `.sparseimage` | `disk-image` |
| `.spc` | `certificate-key` |
| `.splat` | `3d-model` |
| `.spx` | `audio-lossy` |
| `.sql` | `database` |
| `.sqlite` | `database` |
| `.sqlite3` | `database` |
| `.sqlitedb` | `database` |
| `.sr2` | `raw-photo` |
| `.srf` | `raw-photo` |
| `.srpm` | `package-installer` |
| `.srt` | `subtitle` |
| `.srw` | `raw-photo` |
| `.ss` | `source-code` |
| `.ssa` | `subtitle` |
| `.stc` | `spreadsheet` |
| `.step` | `cad` |
| `.sti` | `presentation` |
| `.stl` | `3d-model` |
| `.stp` | `cad` |
| `.stpz` | `cad` |
| `.stw` | `document-office` |
| `.sty` | `markup` |
| `.styl` | `web-asset` |
| `.stylus` | `web-asset` |
| `.sub` | `subtitle` |
| `.suit` | `font` |
| `.sup` | `subtitle` |
| `.sv` | `source-code` |
| `.svelte` | `web-asset` |
| `.svg` | `vector-image` |
| `.svgz` | `vector-image` |
| `.svh` | `source-code` |
| `.swf` | `video` |
| `.swift` | `source-code` |
| `.swm` | `disk-image` |
| `.sxc` | `spreadsheet` |
| `.sxi` | `presentation` |
| `.sxt` | `audio-project` |
| `.sxw` | `document-office` |
| `.sylk` | `spreadsheet` |
| `.sys` | `executable-binary` |
| `.syx` | `audio-project` |
| `.sz` | `archive` |
| `.t` | `source-code` |
| `.t1` | `font` |
| `.tab` | `spreadsheet` |
| `.tak` | `audio-lossless` |
| `.tap` | `cad` |
| `.tar` | `archive` |
| `.targa` | `raster-photo` |
| `.taz` | `archive` |
| `.tazpkg` | `package-installer` |
| `.tb2` | `archive` |
| `.tbz` | `archive` |
| `.tbz2` | `archive` |
| `.tcc` | `source-code` |
| `.tcl` | `source-code` |
| `.tcr` | `ebook` |
| `.tcsh` | `script` |
| `.tex` | `markup` |
| `.texi` | `markup` |
| `.texinfo` | `markup` |
| `.text` | `document-text` |
| `.textile` | `markup` |
| `.tf` | `config-data` |
| `.tfm` | `font` |
| `.tfstate` | `config-data` |
| `.tfvars` | `config-data` |
| `.tga` | `raster-photo` |
| `.tgz` | `archive` |
| `.thrift` | `config-data` |
| `.tib` | `disk-image` |
| `.tif` | `raster-photo` |
| `.tiff` | `raster-photo` |
| `.tipa` | `package-installer` |
| `.tlz` | `archive` |
| `.tlz4` | `archive` |
| `.tlzma` | `archive` |
| `.tlzo` | `archive` |
| `.tnef` | `email` |
| `.toast` | `disk-image` |
| `.tod` | `video` |
| `.toml` | `config-data` |
| `.tool` | `script` |
| `.topojson` | `config-data` |
| `.tpp` | `source-code` |
| `.tpz` | `ebook` |
| `.trace` | `log` |
| `.troff` | `markup` |
| `.ts` | `video` |
| `.tsv` | `spreadsheet` |
| `.tsx` | `source-code` |
| `.tta` | `audio-lossless` |
| `.ttc` | `font` |
| `.ttf` | `font` |
| `.ttml` | `subtitle` |
| `.tts` | `video` |
| `.tvpp` | `layered-image` |
| `.twig` | `markup` |
| `.txt` | `document-text` |
| `.txz` | `archive` |
| `.typ` | `markup` |
| `.tzst` | `archive` |
| `.uc2` | `archive` |
| `.udeb` | `package-installer` |
| `.udf` | `disk-image` |
| `.ufo` | `font` |
| `.uha` | `archive` |
| `.uif` | `disk-image` |
| `.unit` | `config-data` |
| `.uof` | `document-office` |
| `.uop` | `presentation` |
| `.uos` | `spreadsheet` |
| `.uot` | `document-office` |
| `.usd` | `3d-model` |
| `.usda` | `3d-model` |
| `.usdc` | `3d-model` |
| `.usdz` | `3d-model` |
| `.usf` | `subtitle` |
| `.v` | `source-code` |
| `.vala` | `source-code` |
| `.vapi` | `source-code` |
| `.vb` | `source-code` |
| `.vbe` | `script` |
| `.vbs` | `script` |
| `.vcxproj` | `config-data` |
| `.vdi` | `disk-image` |
| `.vert` | `source-code` |
| `.vfb` | `font` |
| `.vfd` | `disk-image` |
| `.vhd` | `disk-image` |
| `.vhdl` | `source-code` |
| `.vhdx` | `disk-image` |
| `.vmdk` | `disk-image` |
| `.vob` | `video` |
| `.vox` | `3d-model` |
| `.vpk` | `archive` |
| `.vqf` | `audio-lossy` |
| `.vrml` | `3d-model` |
| `.vsd` | `vector-image` |
| `.vsdx` | `vector-image` |
| `.vsix` | `package-installer` |
| `.vss` | `vector-image` |
| `.vstpreset` | `audio-project` |
| `.vtt` | `subtitle` |
| `.vue` | `web-asset` |
| `.vwx` | `cad` |
| `.w64` | `audio-lossless` |
| `.war` | `archive` |
| `.wasm` | `web-asset` |
| `.wat` | `source-code` |
| `.wav` | `audio-lossless` |
| `.wave` | `audio-lossless` |
| `.wax` | `playlist` |
| `.wb2` | `spreadsheet` |
| `.wbfs` | `disk-image` |
| `.wbmp` | `raster-photo` |
| `.wdb` | `database` |
| `.weba` | `audio-lossy` |
| `.webm` | `video` |
| `.webmanifest` | `web-asset` |
| `.webp` | `raster-photo` |
| `.wgsl` | `source-code` |
| `.whl` | `package-installer` |
| `.wiki` | `markup` |
| `.wim` | `disk-image` |
| `.wk1` | `spreadsheet` |
| `.wk3` | `spreadsheet` |
| `.wk4` | `spreadsheet` |
| `.wks` | `spreadsheet` |
| `.wma` | `audio-lossy` |
| `.wmf` | `vector-image` |
| `.wmv` | `video` |
| `.wmz` | `vector-image` |
| `.woff` | `font` |
| `.woff2` | `font` |
| `.wpd` | `document-office` |
| `.wpl` | `playlist` |
| `.wps` | `document-office` |
| `.wpt` | `document-office` |
| `.wq1` | `spreadsheet` |
| `.wri` | `document-office` |
| `.wrl` | `3d-model` |
| `.wsf` | `script` |
| `.wsh` | `script` |
| `.wtv` | `video` |
| `.wtx` | `document-text` |
| `.wv` | `audio-lossless` |
| `.wvc` | `audio-lossless` |
| `.wvx` | `playlist` |
| `.x3d` | `3d-model` |
| `.x3db` | `3d-model` |
| `.x3dv` | `3d-model` |
| `.x3f` | `raw-photo` |
| `.x_b` | `cad` |
| `.x_t` | `cad` |
| `.xapk` | `package-installer` |
| `.xar` | `archive` |
| `.xbm` | `raster-photo` |
| `.xbps` | `package-installer` |
| `.xcconfig` | `config-data` |
| `.xcf` | `layered-image` |
| `.xci` | `disk-image` |
| `.xdp` | `pdf` |
| `.xex` | `executable-binary` |
| `.xfdf` | `pdf` |
| `.xht` | `markup` |
| `.xhtml` | `markup` |
| `.xla` | `spreadsheet` |
| `.xlam` | `spreadsheet` |
| `.xls` | `spreadsheet` |
| `.xlsb` | `spreadsheet` |
| `.xlsm` | `spreadsheet` |
| `.xlsx` | `spreadsheet` |
| `.xlt` | `spreadsheet` |
| `.xltm` | `spreadsheet` |
| `.xltx` | `spreadsheet` |
| `.xlw` | `spreadsheet` |
| `.xml` | `markup` |
| `.xonsh` | `script` |
| `.xpi` | `package-installer` |
| `.xpm` | `raster-photo` |
| `.xps` | `pdf` |
| `.xpt` | `database` |
| `.xsd` | `markup` |
| `.xsl` | `markup` |
| `.xslt` | `markup` |
| `.xspf` | `playlist` |
| `.xyz` | `3d-model` |
| `.xz` | `archive` |
| `.y4m` | `video` |
| `.yaml` | `config-data` |
| `.yarnrc` | `config-data` |
| `.yml` | `config-data` |
| `.yz1` | `archive` |
| `.z` | `archive` |
| `.zabw` | `document-office` |
| `.zig` | `source-code` |
| `.zip` | `archive` |
| `.zipx` | `archive` |
| `.zoo` | `archive` |
| `.zpaq` | `archive` |
| `.zpl` | `playlist` |
| `.zpln` | `notebook` |
| `.zpr` | `3d-model` |
| `.zsh` | `script` |
| `.zst` | `archive` |
| `.zstd` | `archive` |
| `.ztl` | `3d-model` |
| `.zz` | `archive` |

_1271 extensions across 37 groups. Generated from `backend/filearr/file_groups.py`._
