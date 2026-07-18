package clicore

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"text/tabwriter"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/lipgloss/table"
	"github.com/muesli/termenv"
	"golang.org/x/term"

	"github.com/filearr/filearr/agent/internal/localapi"
)

// RenderOptions controls how a QueryResponse is presented.
type RenderOptions struct {
	// Color enables the ANSI-colored lipgloss table. When false, Render uses a
	// plain text/tabwriter grid that emits NO ANSI escape codes — the guarantee
	// that piped output is clean even without --plain. The caller sets this only
	// after checking IsTerminal, NO_COLOR, and --plain.
	Color bool
	// Verbose lists the full scope predicate set in the R3 restricted-view footer.
	Verbose bool
	// Offset/Limit are echoed into the "showing first N — use --offset" hint.
	Offset int
	Limit  int
	// Now is a clock seam for humanized mtime; defaults to time.Now.
	Now time.Time
}

// tableHeaders — the R1 wire row carries no lifecycle status (only active items
// cross the P7-T2 boundary), so the STATUS column reflects MATCH quality:
// "exact" or "fuzzy" (the R5 typo-tolerant re-rank), which also satisfies the
// requirement that fuzzy_matched rows are visibly marked.
var tableHeaders = []string{"REL PATH", "SIZE", "MODIFIED", "STATUS"}

// RenderJSON writes one ResultRow per line as NDJSON: jq-parseable, one JSON
// object per line, snake_case keys straight off the wire row. ONLY the rows go to
// w — every advisory (scope/notice/truncated) is written by RenderFooter to a
// separate stream so stdout stays pure, machine-parseable data.
func RenderJSON(w io.Writer, resp localapi.QueryResponse) error {
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false) // keep rel_paths with &,<,> readable; still valid JSON
	for i := range resp.Rows {
		if err := enc.Encode(resp.Rows[i]); err != nil {
			return err
		}
	}
	return nil
}

// Render writes the human-facing results table to w. With opts.Color it renders a
// lipgloss table (TTY only); otherwise a text/tabwriter grid guaranteed free of
// ANSI codes. Rows only — call RenderFooter for the advisories.
func Render(w io.Writer, resp localapi.QueryResponse, opts RenderOptions) error {
	if len(resp.Rows) == 0 {
		return nil
	}
	now := opts.Now
	if now.IsZero() {
		now = time.Now()
	}
	if opts.Color {
		return renderColorTable(w, resp, now)
	}
	return renderPlainTable(w, resp, now)
}

// renderPlainTable uses stdlib text/tabwriter — no color, no ANSI, pipe-safe.
func renderPlainTable(w io.Writer, resp localapi.QueryResponse, now time.Time) error {
	tw := tabwriter.NewWriter(w, 0, 2, 2, ' ', 0)
	fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", tableHeaders[0], tableHeaders[1], tableHeaders[2], tableHeaders[3])
	for i := range resp.Rows {
		r := resp.Rows[i]
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", r.RelPath, formatSize(r.Size), humanizeMtime(r.Mtime, now), matchStatus(r))
	}
	return tw.Flush()
}

// renderColorTable uses lipgloss/table with tty-gated color; fuzzy rows are tinted
// so the approximate matches stand out from exact hits.
func renderColorTable(w io.Writer, resp localapi.QueryResponse, now time.Time) error {
	// Bind a renderer to w with an explicit color profile so the caller's Color
	// decision is authoritative: lipgloss's default renderer would otherwise strip
	// color when w is not a detected TTY. The command only takes this path after
	// gating on IsTerminal + NO_COLOR + --plain, so forcing color here is correct.
	r := lipgloss.NewRenderer(w)
	r.SetColorProfile(termenv.ANSI256)
	header := r.NewStyle().Bold(true).Padding(0, 1)
	cell := r.NewStyle().Padding(0, 1)
	fuzzy := r.NewStyle().Padding(0, 1).Foreground(lipgloss.Color("3")) // yellow

	t := table.New().
		Border(lipgloss.NormalBorder()).
		BorderStyle(r.NewStyle().Foreground(lipgloss.Color("8"))).
		Headers(tableHeaders...).
		StyleFunc(func(row, col int) lipgloss.Style {
			if row == table.HeaderRow {
				return header
			}
			if row >= 0 && row < len(resp.Rows) && resp.Rows[row].FuzzyMatched {
				return fuzzy
			}
			return cell
		})
	for i := range resp.Rows {
		r := resp.Rows[i]
		t.Row(r.RelPath, formatSize(r.Size), humanizeMtime(r.Mtime, now), matchStatus(r))
	}
	_, err := fmt.Fprintln(w, t.Render())
	return err
}

// RenderFooter writes the advisory footer to w (typically stderr, so stdout stays
// pure result data — jq-safe in --json mode). Order: no-match note, then the R3
// restricted-view note (ALWAYS printed when a scope is active — silent filtering
// is forbidden; --verbose lists the predicates), then the server notice, then the
// truncation/pagination hint.
func RenderFooter(w io.Writer, resp localapi.QueryResponse, opts RenderOptions) {
	if len(resp.Rows) == 0 {
		fmt.Fprintln(w, "no matches")
	}
	if resp.Scope.Active {
		fmt.Fprintln(w, "restricted view: results are path-scope filtered")
		if resp.Scope.Stale {
			fmt.Fprintln(w, "  (policy stale — applying the most-restrictive last-known scope)")
		}
		switch {
		case opts.Verbose && len(resp.Scope.Predicates) > 0:
			for _, p := range resp.Scope.Predicates {
				fmt.Fprintf(w, "  scope: %s\n", p)
			}
		case opts.Verbose:
			fmt.Fprintln(w, "  scope predicates: (none reported)")
		case len(resp.Scope.Predicates) > 0:
			fmt.Fprintf(w, "  (%d scope predicate(s); re-run with --verbose to list)\n", len(resp.Scope.Predicates))
		}
	}
	if resp.Notice != nil && *resp.Notice != "" {
		fmt.Fprintf(w, "note: %s\n", *resp.Notice)
	}
	if resp.Truncated {
		fmt.Fprintf(w, "showing first %d result(s) — use --offset %d for the next page\n", len(resp.Rows), opts.Offset+opts.Limit)
	}
}

// RenderHistory writes the local frecency suggestions (highest score first) to w.
// With opts.Color it uses a lipgloss table; otherwise a tabwriter grid free of
// ANSI codes. In --json mode the caller uses RenderHistoryJSON instead.
func RenderHistory(w io.Writer, resp localapi.HistoryResponse, opts RenderOptions) error {
	if len(resp.Entries) == 0 {
		return nil
	}
	now := opts.Now
	if now.IsZero() {
		now = time.Now()
	}
	if opts.Color {
		r := lipgloss.NewRenderer(w)
		r.SetColorProfile(termenv.ANSI256)
		header := r.NewStyle().Bold(true).Padding(0, 1)
		cell := r.NewStyle().Padding(0, 1)
		t := table.New().
			Border(lipgloss.NormalBorder()).
			BorderStyle(r.NewStyle().Foreground(lipgloss.Color("8"))).
			Headers("QUERY", "USES", "LAST USED").
			StyleFunc(func(row, col int) lipgloss.Style {
				if row == table.HeaderRow {
					return header
				}
				return cell
			})
		for i := range resp.Entries {
			e := resp.Entries[i]
			t.Row(e.Query, fmt.Sprintf("%.0f", e.Hits), humanizeMtime(e.LastUsed, now))
		}
		_, err := fmt.Fprintln(w, t.Render())
		return err
	}
	tw := tabwriter.NewWriter(w, 0, 2, 2, ' ', 0)
	fmt.Fprintf(tw, "QUERY\tUSES\tLAST USED\n")
	for i := range resp.Entries {
		e := resp.Entries[i]
		fmt.Fprintf(tw, "%s\t%.0f\t%s\n", e.Query, e.Hits, humanizeMtime(e.LastUsed, now))
	}
	return tw.Flush()
}

// RenderHistoryJSON writes one HistoryEntry per line as NDJSON (jq-parseable).
func RenderHistoryJSON(w io.Writer, resp localapi.HistoryResponse) error {
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	for i := range resp.Entries {
		if err := enc.Encode(resp.Entries[i]); err != nil {
			return err
		}
	}
	return nil
}

// IsTerminal reports whether w is a terminal (an *os.File backed by a tty). The
// CLI gates color on this — a *bytes.Buffer or a pipe is never a terminal, so
// piped/redirected output carries no ANSI regardless of --plain.
func IsTerminal(w io.Writer) bool {
	f, ok := w.(*os.File)
	return ok && term.IsTerminal(int(f.Fd()))
}

// ColorEnabled resolves whether to emit ANSI color: only on a real TTY, with
// --plain not set, and honoring the NO_COLOR convention (present and non-empty
// disables color, per no-color.org). Extracted as a pure function so the gate is
// deterministically testable without a controlling terminal.
func ColorEnabled(isTTY, plain bool) bool {
	if plain || !isTTY {
		return false
	}
	if v, ok := os.LookupEnv("NO_COLOR"); ok && v != "" {
		return false
	}
	return true
}

// formatSize renders a byte count with binary (1024ⁿ) units — matching the DSL's
// K/M/G semantics — e.g. "1.4 GiB", "512 B".
func formatSize(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(unit), 0
	for m := n / unit; m >= unit; m /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(n)/float64(div), "KMGTPE"[exp])
}

// humanizeMtime renders an ISO-8601 mtime string relative to now ("3d ago"). An
// unparseable value is passed through verbatim.
func humanizeMtime(iso string, now time.Time) string {
	t, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		return iso
	}
	d := now.Sub(t)
	if d < 0 {
		return t.Format("2006-01-02")
	}
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	case d < 30*24*time.Hour:
		return fmt.Sprintf("%dd ago", int(d.Hours()/24))
	case d < 365*24*time.Hour:
		return fmt.Sprintf("%dmo ago", int(d.Hours()/24/30))
	default:
		return fmt.Sprintf("%dy ago", int(d.Hours()/24/365))
	}
}

// matchStatus labels a row exact vs fuzzy (the R5 typo-tolerant re-rank).
func matchStatus(r localapi.ResultRow) string {
	if r.FuzzyMatched {
		return "fuzzy"
	}
	return "exact"
}
