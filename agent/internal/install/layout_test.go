package install

import "testing"

func TestResolveLayoutPerOS(t *testing.T) {
	winEnv := func(k string) string {
		switch k {
		case "ProgramFiles":
			return `C:\Program Files`
		case "ProgramData":
			return `C:\ProgramData`
		}
		return ""
	}
	cases := []struct {
		goos   string
		getenv func(string) string
		want   Layout
	}{
		{
			goos:   "windows",
			getenv: winEnv,
			want: Layout{
				InstallDir: `C:\Program Files\Filearr Agent`,
				BinPath:    `C:\Program Files\Filearr Agent\filearr-agent.exe`,
				DataDir:    `C:\ProgramData\Filearr Agent`,
				ConfigDir:  `C:\ProgramData\Filearr Agent`,
				ConfigPath: `C:\ProgramData\Filearr Agent\filearr-agent.json`,
				LogDir:     `C:\ProgramData\Filearr Agent\logs`,
			},
		},
		{
			goos:   "linux",
			getenv: nil,
			want: Layout{
				InstallDir: "/usr/local/bin",
				BinPath:    "/usr/local/bin/filearr-agent",
				DataDir:    "/var/lib/filearr-agent",
				ConfigDir:  "/etc/filearr-agent",
				ConfigPath: "/etc/filearr-agent/filearr-agent.json",
				LogDir:     "/var/log/filearr-agent",
			},
		},
		{
			goos:   "darwin",
			getenv: nil,
			want: Layout{
				InstallDir: "/usr/local/bin",
				BinPath:    "/usr/local/bin/filearr-agent",
				DataDir:    "/Library/Application Support/FilearrAgent",
				ConfigDir:  "/Library/Application Support/FilearrAgent",
				ConfigPath: "/Library/Application Support/FilearrAgent/filearr-agent.json",
				LogDir:     "/Library/Logs/FilearrAgent",
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.goos, func(t *testing.T) {
			got, err := ResolveLayout(tc.goos, tc.getenv)
			if err != nil {
				t.Fatalf("ResolveLayout(%s): %v", tc.goos, err)
			}
			if got != tc.want {
				t.Fatalf("ResolveLayout(%s)\n got=%+v\nwant=%+v", tc.goos, got, tc.want)
			}
		})
	}
}

func TestResolveLayoutWindowsEnvFallback(t *testing.T) {
	got, err := ResolveLayout("windows", func(string) string { return "" })
	if err != nil {
		t.Fatal(err)
	}
	if got.InstallDir != `C:\Program Files\Filearr Agent` || got.DataDir != `C:\ProgramData\Filearr Agent` {
		t.Fatalf("env-less Windows fallback wrong: %+v", got)
	}
}

func TestResolveLayoutUnsupported(t *testing.T) {
	if _, err := ResolveLayout("plan9", nil); err == nil {
		t.Fatal("expected an error for an unsupported OS")
	}
}
