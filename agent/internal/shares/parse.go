package shares

import (
	"encoding/csv"
	"strings"
)

// This file holds the PURE, platform-neutral parsers for each OS's share-listing
// format. They take raw command/file output text and return the discovered
// exports; they never touch the filesystem or shell out (that is enumerate.go),
// so they are fully fixture-testable on any host. Every parser is tolerant:
// unrecognised / malformed lines are skipped, never fatal (R1 best-effort).

// specialSMBShare reports whether an SMB share name is a hidden/administrative
// default (C$, ADMIN$, IPC$, print$, ...) or a Samba meta-section we must not turn
// into a user-openable hint: an admin `$` share reaches the whole drive but only
// an administrator can open it, so advertising \\host\C$ would fabricate a link
// that fails for ordinary users. Case-insensitive.
func specialSMBShare(name string) bool {
	if name == "" {
		return true
	}
	if strings.HasSuffix(name, "$") { // C$, ADMIN$, IPC$, print$, <drive>$
		return true
	}
	switch strings.ToLower(name) {
	case "global", "printers", "homes":
		return true
	}
	return false
}

// parseSmbShareCSV parses the CSV produced by
// `Get-SmbShare | Select-Object Name,Path | ConvertTo-Csv -NoTypeInformation`:
//
//	"Name","Path"
//	"media","D:\media"
//	"Public","C:\Users\Public"
//	"C$","C:\"
//
// Quoting (CSV) makes paths-with-spaces unambiguous, unlike `net share`'s columns.
// The header row and any admin/hidden `$` share are skipped.
func parseSmbShareCSV(out string) []export {
	r := csv.NewReader(strings.NewReader(strings.TrimSpace(out)))
	r.FieldsPerRecord = -1 // tolerate ragged rows
	records, err := r.ReadAll()
	if err != nil || len(records) == 0 {
		return nil
	}
	var exports []export
	for i, rec := range records {
		if len(rec) < 2 {
			continue
		}
		name, path := strings.TrimSpace(rec[0]), strings.TrimSpace(rec[1])
		if i == 0 && strings.EqualFold(name, "Name") {
			continue // header
		}
		if name == "" || path == "" || specialSMBShare(name) {
			continue
		}
		exports = append(exports, export{name: name, path: path, kind: "smb"})
	}
	return exports
}

// parseNetShare parses classic `net share` list output (Windows fallback):
//
//	Share name   Resource                        Remark
//	-------------------------------------------------------------
//	C$           C:\                             Default share
//	media        D:\media
//	IPC$                                         Remote IPC
//	The command completed successfully.
//
// Column-based, so a path containing spaces is best-effort only (the Resource
// column is taken from the name column's end to the first run of 2+ spaces). This
// is why CSV Get-SmbShare is preferred; net share is the locked-down fallback.
func parseNetShare(out string) []export {
	var exports []export
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimRight(line, "\r")
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "-") {
			continue
		}
		low := strings.ToLower(trimmed)
		if strings.HasPrefix(low, "share name") ||
			strings.HasPrefix(low, "the command completed") {
			continue
		}
		// Name is the first whitespace-delimited token; the resource is the next
		// field, split on a run of 2+ spaces so a single-spaced path survives.
		fields := strings.Fields(trimmed)
		if len(fields) < 2 {
			continue
		}
		name := fields[0]
		if specialSMBShare(name) {
			continue
		}
		rest := strings.TrimSpace(trimmed[len(name):])
		// Resource = rest up to the first 2+-space gap (the Remark column).
		resource := rest
		if idx := indexMultiSpace(rest); idx >= 0 {
			resource = strings.TrimSpace(rest[:idx])
		}
		if resource == "" || !looksLikeLocalPath(resource) {
			continue
		}
		exports = append(exports, export{name: name, path: resource, kind: "smb"})
	}
	return exports
}

// indexMultiSpace returns the index of the first run of 2+ spaces, or -1.
func indexMultiSpace(s string) int {
	return strings.Index(s, "  ")
}

// looksLikeLocalPath is a cheap guard so a `net share` Remark that leaked into the
// resource column (e.g. "Remote IPC") is not mistaken for a path.
func looksLikeLocalPath(s string) bool {
	if len(s) >= 2 && s[1] == ':' { // drive-letter path C:\...
		return true
	}
	return strings.HasPrefix(s, `\\`) || strings.HasPrefix(s, "/")
}

// parseSmbConf parses a Samba smb.conf: each `[section]` with a `path =` (or the
// `directory =` synonym) becomes an SMB export named after the section. Special
// sections ([global], [printers], [homes]) and print shares are skipped.
func parseSmbConf(content string) []export {
	var exports []export
	var cur string
	var curPath string
	var printable bool
	flush := func() {
		if cur != "" && curPath != "" && !specialSMBShare(cur) && !printable {
			exports = append(exports, export{name: cur, path: curPath, kind: "smb"})
		}
	}
	for _, raw := range strings.Split(content, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";") {
			continue
		}
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			flush()
			cur = strings.TrimSpace(line[1 : len(line)-1])
			curPath = ""
			printable = false
			continue
		}
		key, val, ok := splitConf(line)
		if !ok {
			continue
		}
		switch strings.ToLower(strings.ReplaceAll(key, " ", "")) {
		case "path", "directory":
			curPath = stripInlineComment(val)
		case "printable", "print":
			printable = isTruthy(val)
		}
	}
	flush()
	return exports
}

// parseExports parses /etc/exports (NFS). The first whitespace-delimited token on
// each non-comment line is the exported directory (optionally double-quoted for a
// path with spaces); the client/option list that follows is ignored.
func parseExports(content string) []export {
	var exports []export
	for _, raw := range strings.Split(content, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		var path string
		if strings.HasPrefix(line, `"`) { // "quoted path with spaces" clients...
			if end := strings.Index(line[1:], `"`); end >= 0 {
				path = line[1 : 1+end]
			}
		} else {
			path = strings.Fields(line)[0]
		}
		if path == "" || !strings.HasPrefix(path, "/") {
			continue
		}
		exports = append(exports, export{path: path, kind: "nfs"})
	}
	return exports
}

// parseSharingL parses macOS `sharing -l` (share points). A `name:` line starts a
// block; the block's `path:` line gives the exported directory. Values may contain
// spaces (share names commonly do).
//
//	name:		Media
//	path:		/Volumes/Data/Media
//	smb:		enabled
//	name:		Public
//	path:		/Users/x/Public
func parseSharingL(out string) []export {
	var exports []export
	var name, path string
	flush := func() {
		if name != "" && path != "" {
			exports = append(exports, export{name: name, path: path, kind: "smb"})
		}
	}
	for _, raw := range strings.Split(out, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		key, val, ok := splitColon(line)
		if !ok {
			continue
		}
		switch strings.ToLower(key) {
		case "name":
			flush()
			name, path = val, ""
		case "path":
			path = val
		}
	}
	flush()
	return exports
}

// --- small tolerant token helpers -------------------------------------------

// splitConf splits an smb.conf `key = value` line.
func splitConf(line string) (key, val string, ok bool) {
	i := strings.Index(line, "=")
	if i < 0 {
		return "", "", false
	}
	return strings.TrimSpace(line[:i]), strings.TrimSpace(line[i+1:]), true
}

// splitColon splits a `key:<ws>value` line (macOS sharing output / generic).
func splitColon(line string) (key, val string, ok bool) {
	i := strings.Index(line, ":")
	if i < 0 {
		return "", "", false
	}
	return strings.TrimSpace(line[:i]), strings.TrimSpace(line[i+1:]), true
}

func stripInlineComment(s string) string {
	// Samba treats a leading # / ; as a comment; inline after a value is uncommon,
	// so only strip a trailing comment that is clearly separated.
	return strings.TrimSpace(s)
}

func isTruthy(s string) bool {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "yes", "true", "1", "on":
		return true
	}
	return false
}
