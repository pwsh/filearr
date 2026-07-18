package install

import (
	"errors"
	"os"
	"testing"
)

// mockController records lifecycle calls and returns a scripted status.
type mockController struct {
	status     Status
	statusErr  error
	calls      []string
	installErr error
	startErr   error
}

func (m *mockController) record(op string) { m.calls = append(m.calls, op) }

func (m *mockController) Install() error   { m.record("install"); return m.installErr }
func (m *mockController) Uninstall() error { m.record("uninstall"); return nil }
func (m *mockController) Start() error     { m.record("start"); return m.startErr }
func (m *mockController) Stop() error      { m.record("stop"); return nil }
func (m *mockController) Restart() error   { m.record("restart"); return nil }
func (m *mockController) Status() (Status, error) {
	m.record("status")
	return m.status, m.statusErr
}

// fakeFS records filesystem effects in memory.
type fakeFS struct {
	dirs     map[string]bool
	copied   map[string]bool
	removed  map[string]bool
	removedA map[string]bool
	same     bool // SameFile return
}

func newFakeFS() *fakeFS {
	return &fakeFS{dirs: map[string]bool{}, copied: map[string]bool{}, removed: map[string]bool{}, removedA: map[string]bool{}}
}

func (f *fakeFS) MkdirAll(path string, _ os.FileMode) error { f.dirs[path] = true; return nil }
func (f *fakeFS) CopyFile(_, dst string, _ os.FileMode) error {
	f.copied[dst] = true
	return nil
}
func (f *fakeFS) Remove(path string) error    { f.removed[path] = true; return nil }
func (f *fakeFS) RemoveAll(path string) error { f.removedA[path] = true; return nil }
func (f *fakeFS) SameFile(_, _ string) (bool, error) {
	return f.same, nil
}

func testLayout() Layout {
	l, _ := ResolveLayout("linux", nil)
	return l
}

func TestInstallFreshRegistersAndStarts(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusNotInstalled}
	in := &Installer{
		Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	if err := in.Install(); err != nil {
		t.Fatalf("Install: %v", err)
	}
	// Dirs created.
	for _, d := range []string{"/usr/local/bin", "/var/lib/filearr-agent", "/etc/filearr-agent", "/var/log/filearr-agent"} {
		if !fs.dirs[d] {
			t.Fatalf("dir %s not created", d)
		}
	}
	// Binary copied.
	if !fs.copied["/usr/local/bin/filearr-agent"] {
		t.Fatal("binary not copied")
	}
	// Fresh install: no stop/uninstall, then install + start.
	assertCalls(t, ctrl.calls, []string{"status", "install", "start"})
}

func TestInstallIdempotentUpgradeStopsFirst(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusRunning} // already installed + running
	in := &Installer{
		Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	if err := in.Install(); err != nil {
		t.Fatalf("Install: %v", err)
	}
	// Existing service: stop + uninstall before re-install + start.
	assertCalls(t, ctrl.calls, []string{"status", "stop", "uninstall", "install", "start"})
}

func TestInstallSkipsCopyWhenSameFile(t *testing.T) {
	fs := newFakeFS()
	fs.same = true
	ctrl := &mockController{status: StatusNotInstalled}
	in := &Installer{
		Layout: testLayout(), SourceExe: "/usr/local/bin/filearr-agent", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	if err := in.Install(); err != nil {
		t.Fatalf("Install: %v", err)
	}
	if fs.copied["/usr/local/bin/filearr-agent"] {
		t.Fatal("binary should not be copied onto itself")
	}
}

func TestInstallEnrollGating(t *testing.T) {
	t.Run("token present + not enrolled => enroll called", func(t *testing.T) {
		fs := newFakeFS()
		ctrl := &mockController{status: StatusNotInstalled}
		enrolled := 0
		in := &Installer{
			Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
			IsAdmin:  func() bool { return true },
			HasToken: true,
			Enrolled: func() bool { return false },
			Enroll:   func() error { enrolled++; return nil },
		}
		if err := in.Install(); err != nil {
			t.Fatal(err)
		}
		if enrolled != 1 {
			t.Fatalf("enroll called %d times, want 1", enrolled)
		}
	})

	t.Run("token present + already enrolled => enroll skipped", func(t *testing.T) {
		fs := newFakeFS()
		ctrl := &mockController{status: StatusNotInstalled}
		enrolled := 0
		in := &Installer{
			Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
			IsAdmin:  func() bool { return true },
			HasToken: true,
			Enrolled: func() bool { return true },
			Enroll:   func() error { enrolled++; return nil },
		}
		if err := in.Install(); err != nil {
			t.Fatal(err)
		}
		if enrolled != 0 {
			t.Fatalf("enroll called %d times, want 0 (already enrolled)", enrolled)
		}
	})

	t.Run("no token => enroll skipped", func(t *testing.T) {
		fs := newFakeFS()
		ctrl := &mockController{status: StatusNotInstalled}
		enrolled := 0
		in := &Installer{
			Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
			IsAdmin:  func() bool { return true },
			HasToken: false,
			Enroll:   func() error { enrolled++; return nil },
		}
		if err := in.Install(); err != nil {
			t.Fatal(err)
		}
		if enrolled != 0 {
			t.Fatalf("enroll called %d times, want 0 (no token)", enrolled)
		}
	})
}

func TestInstallRequiresAdmin(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusNotInstalled}
	in := &Installer{
		Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return false },
	}
	if err := in.Install(); !errors.Is(err, ErrNeedAdmin) {
		t.Fatalf("Install without admin: err=%v, want ErrNeedAdmin", err)
	}
	if len(ctrl.calls) != 0 || len(fs.dirs) != 0 {
		t.Fatal("no side effects should occur without admin")
	}
}

func TestUninstallKeepsDataByDefault(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusRunning}
	in := &Installer{
		Layout: testLayout(), FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	kept, err := in.Uninstall(false)
	if err != nil {
		t.Fatal(err)
	}
	assertCalls(t, ctrl.calls, []string{"status", "stop", "uninstall"})
	if !fs.removed["/usr/local/bin/filearr-agent"] {
		t.Fatal("binary not removed")
	}
	if len(fs.removedA) != 0 {
		t.Fatalf("data/logs/config should be kept, but RemoveAll hit: %v", fs.removedA)
	}
	if len(kept) == 0 {
		t.Fatal("expected kept dirs to be reported")
	}
}

func TestUninstallPurgeRemovesEverything(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusStopped}
	in := &Installer{
		Layout: testLayout(), FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	kept, err := in.Uninstall(true)
	if err != nil {
		t.Fatal(err)
	}
	if len(kept) != 0 {
		t.Fatalf("purge should keep nothing, got %v", kept)
	}
	for _, d := range []string{"/var/lib/filearr-agent", "/var/log/filearr-agent", "/etc/filearr-agent"} {
		if !fs.removedA[d] {
			t.Fatalf("purge did not remove %s", d)
		}
	}
}

func TestUninstallNotInstalledSkipsServiceUninstall(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusNotInstalled}
	in := &Installer{
		Layout: testLayout(), FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	if _, err := in.Uninstall(false); err != nil {
		t.Fatal(err)
	}
	// Only Status queried; no stop/uninstall on an absent service.
	assertCalls(t, ctrl.calls, []string{"status"})
}

func assertCalls(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("calls=%v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("calls=%v, want %v", got, want)
		}
	}
}
