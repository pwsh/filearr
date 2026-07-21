package permissions

import "testing"

func TestClassifySID(t *testing.T) {
	tests := map[string]string{
		"S-1-1-0":                   WKEveryone,
		"S-1-3-0":                   WKCreatorOwner,
		"S-1-5-18":                  WKSystem,
		"S-1-5-32-544":              WKAdministrators,
		"S-1-5-32-545":              WKUsers,
		"S-1-5-11":                  WKAuthenticatedUsers,
		"  S-1-5-18  ":              WKSystem, // trimmed
		"S-1-5-21-111-222-333-1001": "",       // ordinary domain user
		"S-1-5-32-544-extra":        "",       // not an exact match
	}
	for sid, want := range tests {
		if got := ClassifySID(sid); got != want {
			t.Errorf("ClassifySID(%q) = %q, want %q", sid, got, want)
		}
	}
}

func TestClassifyPOSIXID(t *testing.T) {
	if got := ClassifyPOSIXID(0, false); got != WKRoot {
		t.Errorf("uid 0 = %q, want root", got)
	}
	if got := ClassifyPOSIXID(0, true); got != WKWheel {
		t.Errorf("gid 0 = %q, want wheel", got)
	}
	if got := ClassifyPOSIXID(1, false); got != WKDaemon {
		t.Errorf("uid 1 = %q, want daemon", got)
	}
	if got := ClassifyPOSIXID(1000, false); got != "" {
		t.Errorf("uid 1000 = %q, want empty", got)
	}
}

func TestClassifyPOSIXName(t *testing.T) {
	tests := map[string]string{
		"root":       WKRoot,
		"ROOT":       WKRoot,
		"wheel":      WKWheel,
		"daemon":     WKDaemon,
		"CORP\\root": WKRoot, // DOMAIN\user form
		"root@corp":  WKRoot, // NFSv4 user@domain form
		"jsmith":     "",
	}
	for name, want := range tests {
		if got := ClassifyPOSIXName(name); got != want {
			t.Errorf("ClassifyPOSIXName(%q) = %q, want %q", name, got, want)
		}
	}
}
