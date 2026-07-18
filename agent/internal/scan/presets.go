// Package scan ports the central Filearr scanner's filesystem walk, diff,
// move-detection, sidecar-association and tombstone logic (backend/filearr/
// tasks/scan.py, move.py, associate.py, sidecar.py, presets.py, media_types.py)
// to the offline-first Go agent. It is a *behavioural* port: identity is
// (root_id, rel_path), scans tombstone rather than delete, and directory
// pruning always wins over file-level negation (ruling R1).
package scan

// presetBundle is a named, independently-toggleable set of gitignore-style
// exclude patterns. Mirrors backend/filearr/presets.py:PresetBundle.
type presetBundle struct {
	name           string
	exclude        []string
	defaultEnabled bool
}

// presetBundles is the canonical, ordered preset catalog. The pattern strings
// are copied VERBATIM from backend/filearr/presets.py (PRESET_BUNDLES),
// including the bracket-expanded case tolerance ([Tt]humbs.db), the literal
// carriage-return Finder pattern (Icon[\r]), the escaped emacs-autosave glob
// (\#*) and the $RECYCLE.BIN/ system pattern.
//
// KEPT IN SYNC WITH backend/filearr/presets.py — do NOT hand-edit a pattern
// without re-running the P5-T3a vector gate (gitignore_test.go). Order is the
// canonical PRESET_BUNDLES order and is load-bearing for gitignore
// last-match-wins determinism.
var presetBundles = []presetBundle{
	{
		name: "system_files",
		exclude: []string{
			"$RECYCLE.BIN/",
			"System Volume Information/",
			".Trashes/",
			".Trash-*/",
			".fseventsd/",
			".Spotlight-V100/",
			".DocumentRevisions-V100/",
			".TemporaryItems/",
			"lost+found/",
			"[Dd]esktop.ini",
			"[Tt]humbs.db",
			"[Tt]humbs.db:encryptable",
			"ehthumbs.db",
			"ehthumbs_vista.db",
			"*.lnk",
			".directory",
			".fuse_hidden*",
			".nfs*",
			"*~",
			"nohup.out",
		},
	},
	{
		name:           "hidden_dotfiles",
		exclude:        []string{".*"},
		defaultEnabled: true,
	},
	{
		name: "caches_temp",
		exclude: []string{
			"tmp/",
			"temp/",
			".cache/",
			"__pycache__/",
			".pytest_cache/",
			".tox/",
			".direnv/",
			".ccls-cache/",
			".parcel-cache/",
			".nyc_output/",
			"*.tmp",
			"*.cache",
			"*.pyc",
			// emacs autosave; the leading '#' must be escaped or gitignore
			// treats the line as a comment.
			`\#*`,
		},
	},
	{
		name: "node_modules_build",
		exclude: []string{
			"node_modules/",
			"jspm_packages/",
			"bower_components/",
			".pnpm-store/",
			"dist/",
			".next/",
			".nuxt/",
			".svelte-kit/",
			".vite/",
			"target/",
			".venv/",
			"venv/",
			".eslintcache",
			"*.tsbuildinfo",
			"*.pyc",
			"npm-debug.log*",
		},
	},
	{
		name: "os_metadata",
		exclude: []string{
			".DS_Store",
			"._*",
			// Literal Finder "Icon" file ending in a carriage-return byte. The
			// character-class form Icon[\r] is required: a bare "Icon\r" line
			// has its trailing CR stripped by gitignore whitespace rules.
			"Icon[\r]",
			"[Tt]humbs.db",
			"[Dd]esktop.ini",
		},
	},
}

// presetByName indexes presetBundles for resolveEffectivePresets.
var presetByName = func() map[string]presetBundle {
	m := make(map[string]presetBundle, len(presetBundles))
	for _, b := range presetBundles {
		m[b.name] = b
	}
	return m
}()

// resolveEffectivePresets resolves a stored enabledPresets configuration into
// the effective preset set, mirroring presets.resolve_effective_presets: the
// union of every default_enabled bundle (today only hidden_dotfiles) and the
// stored positive entries, MINUS any "-name" sentinel disabling a default.
// Returned in canonical presetBundles order. Unknown names are ignored.
func resolveEffectivePresets(enabledPresets []string) []string {
	active := map[string]bool{}
	for _, b := range presetBundles {
		if b.defaultEnabled {
			active[b.name] = true
		}
	}
	disabled := map[string]bool{}
	for _, e := range enabledPresets {
		if len(e) > 0 && e[0] == '-' {
			disabled[e[1:]] = true
		} else {
			active[e] = true
		}
	}
	var out []string
	for _, b := range presetBundles {
		if active[b.name] && !disabled[b.name] {
			out = append(out, b.name)
		}
	}
	return out
}
