package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func updateFixture(t *testing.T, base string, age time.Duration, bytes int64) (string, *os.File) {
	t.Helper()
	path, lease, r, err := updateStart(base, "-")
	if err != nil {
		t.Fatal(err)
	}
	r.Touched = time.Now().Add(-age)
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	f, err := os.Create(filepath.Join(path, "payload"))
	if err != nil {
		t.Fatal(err)
	}
	if err = f.Truncate(bytes); err != nil {
		t.Fatal(err)
	}
	f.Close()
	t.Cleanup(func() { lease.Close() })
	return path, lease
}

func TestUpdateCacheAgeBudgetAndActive(t *testing.T) {
	base := t.TempDir()
	old, a := updateFixture(t, base, 25*time.Hour, 10)
	a.Close()
	recent, b := updateFixture(t, base, time.Minute, 10)
	b.Close()
	active, _ := updateFixture(t, base, 48*time.Hour, 1<<20)
	_, removed, err := updateCollect(base, true, false, time.Now(), updateLimit)
	if err != nil || len(removed) != 1 || removed[0] != old {
		t.Fatalf("age collection: %v %v", removed, err)
	}
	for _, path := range []string{recent, active} {
		if _, err = os.Stat(path); err != nil {
			t.Fatal(err)
		}
	}
	_, removed, err = updateCollect(base, true, false, time.Now(), 0)
	if err != nil || len(removed) != 1 || removed[0] != recent {
		t.Fatalf("budget: %v %v", removed, err)
	}
	_, removed, err = updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 {
		t.Fatalf("active --all: %v %v", removed, err)
	}
}

func TestUpdateCacheStatusDoesNotRemove(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	entries, removed, err := updateCollect(base, false, false, time.Now(), updateLimit)
	if err != nil || len(entries) != 1 || !entries[0].Eligible || len(removed) != 0 {
		t.Fatalf("status: %+v %v", entries, err)
	}
	if _, err = os.Stat(path); err != nil {
		t.Fatal(err)
	}
}

func TestUpdateCacheNeverAdoptsUnknownOrLegacy(t *testing.T) {
	base := t.TempDir()
	pool, gate, err := updateGate(base)
	if err != nil {
		t.Fatal(err)
	}
	gate.Close()
	outside := t.TempDir()
	for _, path := range []string{filepath.Join(base, "candidate.legacy"), filepath.Join(pool, "candidate.unknown")} {
		if err = os.Mkdir(path, 0700); err != nil {
			t.Fatal(err)
		}
	}
	if err = os.Symlink(outside, filepath.Join(pool, "candidate.link")); err != nil {
		t.Fatal(err)
	}
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 {
		t.Fatalf("unknown removal: %v %v", removed, err)
	}
}

func TestUpdateCacheRejectsSymlinkPoolAndLease(t *testing.T) {
	base := t.TempDir()
	if err := os.Symlink(t.TempDir(), filepath.Join(base, "v1")); err != nil {
		t.Fatal(err)
	}
	if _, _, err := updateGate(base); err == nil {
		t.Fatal("symlink pool accepted")
	}
	base = t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	if err := os.Remove(filepath.Join(path, ".lease")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(path, "payload"), filepath.Join(path, ".lease")); err != nil {
		t.Fatal(err)
	}
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 {
		t.Fatalf("symlink lease: %v %v", removed, err)
	}
}

func TestUpdateCacheReadOnlyDirectoriesAndExternalLinks(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	external := t.TempDir()
	nested := filepath.Join(path, "immutable")
	if err := os.Mkdir(nested, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(external, filepath.Join(nested, "external")); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(nested, 0500); err != nil {
		t.Fatal(err)
	}
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 1 {
		t.Fatalf("immutable: %v %v", removed, err)
	}
	if _, err = os.Stat(external); err != nil {
		t.Fatal(err)
	}
}

func TestUpdateCacheResumeAdmissionAndExpiry(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, time.Minute, 10)
	if _, _, _, err := updateStart(base, path); err == nil {
		t.Fatal("concurrent resume accepted")
	}
	lease.Close()
	_, lease, _, err := updateStart(base, path)
	if err != nil {
		t.Fatal(err)
	}
	lease.Close()
	if _, _, err = updateCollect(base, true, true, time.Now(), 0); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err = updateStart(base, path); err == nil {
		t.Fatal("expired resume accepted")
	}
}

func TestUpdateCacheForeignOwnership(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("foreign ownership needs root; ordinary runs validate owned paths")
	}
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	if err := os.Chown(path, 12345, 12345); err != nil {
		t.Fatal(err)
	}
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 {
		t.Fatalf("foreign: %v %v", removed, err)
	}
}

func TestUpdateCacheContainerWitness(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	engine := filepath.Join(t.TempDir(), "engine")
	if err := os.WriteFile(engine, []byte("#!/bin/sh\necho running-container\n"), 0700); err != nil {
		t.Fatal(err)
	}
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Engines = []string{engine}
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	entries, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 || !entries[0].Active {
		t.Fatalf("container: %v %v", removed, err)
	}
	if err = os.WriteFile(engine, []byte("#!/bin/sh\nexit 1\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if _, _, err = updateCollect(base, true, true, time.Now(), 0); err == nil {
		t.Fatal("unavailable engine treated as idle")
	}
}

func updateTestCommand(t *testing.T, base, script string) *exec.Cmd {
	t.Helper()
	cmd := exec.Command(os.Args[0], "update-cache", "run", base, "-", "deps-update", "/bin/sh", "-c", script)
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(entry, "CHAINMAN_") && !strings.HasPrefix(entry, "TOOLCHAIN_") {
			cmd.Env = append(cmd.Env, entry)
		}
	}
	return cmd
}

func TestUpdateCacheCommandSuccessFailureAndOversize(t *testing.T) {
	for _, test := range []struct {
		name, script string
		want         int
		retained     bool
	}{
		{"success", "test -n \"$CHAINMAN_UPDATE_LEASE_FD\"; mkdir \"$CHAINMAN_UPDATE_TRANSACTION/candidate\"", 0, false},
		{"failure", "mkdir \"$CHAINMAN_UPDATE_TRANSACTION/candidate\"; exit 17", 17, true},
		{"oversize", "truncate -s 13958643712 \"$CHAINMAN_UPDATE_TRANSACTION/large\"; exit 19", 19, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			base := t.TempDir()
			out, err := updateTestCommand(t, base, test.script).CombinedOutput()
			got := 0
			if err != nil {
				if e, ok := err.(*exec.ExitError); ok {
					got = e.ExitCode()
				} else {
					t.Fatal(err)
				}
			}
			if got != test.want {
				t.Fatalf("exit %d: %s", got, out)
			}
			paths, _ := filepath.Glob(filepath.Join(base, "v1/candidate.*"))
			if (len(paths) > 0) != test.retained {
				t.Fatalf("retention %v: %s", paths, out)
			}
			if test.retained && !strings.Contains(string(out), "not a backup") {
				t.Fatal("missing warning")
			}
			if test.want != 0 && !test.retained && strings.Contains(string(out), "resume=") {
				t.Fatal("printed expired resume")
			}
		})
	}
}

func TestUpdateCacheInheritedLeaseSurvivesOwner(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	// A different process retains the same kernel open-file description; no PID
	// lookup is involved in pruning, including after the allocating owner closes.
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	cmd := exec.Command("/bin/sh", "-c", "read ignored <&4")
	cmd.ExtraFiles = []*os.File{lease, reader}
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	reader.Close()
	lease.Close()
	defer func() { writer.Close(); cmd.Wait() }()
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 {
		t.Fatalf("inherited lease lost: %v %v", removed, err)
	}
	writer.Close()
	cmd.Wait()
	_, removed, err = updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 1 || removed[0] != path {
		t.Fatalf("released child: %v %v", removed, err)
	}
}

func TestUpdateCacheConcurrentPruners(t *testing.T) {
	base := t.TempDir()
	_, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	done := make(chan error, 2)
	for range 2 {
		go func() { _, _, err := updateCollect(base, true, true, time.Now(), 0); done <- err }()
	}
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
}

func TestUpdateCacheCompletedChildLease(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 0, 10)
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Complete = true
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	entries, _, err := updateCollect(base, false, false, time.Now(), updateLimit)
	if err != nil || entries[0].Eligible {
		t.Fatal("active success is collectible")
	}
	lease.Close()
	_, removed, err := updateCollect(base, true, false, time.Now(), updateLimit)
	if err != nil || len(removed) != 1 {
		t.Fatalf("completed: %v %v", removed, err)
	}
}

func TestUpdateCacheReceiptHardlinkRejected(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	if err := os.Link(filepath.Join(path, ".transaction.json"), filepath.Join(path, "duplicate")); err != nil {
		t.Fatal(err)
	}
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 {
		t.Fatalf("hardlink: %v %v", removed, err)
	}
}

func TestUpdateCacheReportsJSON(t *testing.T) {
	base := t.TempDir()
	_, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	cmd := exec.Command(os.Args[0], "update-cache", "status", base)
	out, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	var report struct {
		Transactions []updateEntry `json:"transactions"`
	}
	if err = json.Unmarshal(out, &report); err != nil || len(report.Transactions) != 1 {
		t.Fatalf("%s %v", out, err)
	}
}

func TestUpdateCacheLeaseValidation(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 0, 10)
	lease.Close()
	if err := os.Chmod(filepath.Join(path, ".lease"), 0666); err != nil {
		t.Fatal(err)
	}
	if _, err := updateFile(filepath.Join(path, ".lease"), false); err == nil {
		t.Fatal("public lease accepted")
	}
	if err := os.Remove(filepath.Join(path, ".lease")); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(filepath.Join(path, ".lease"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := updateFile(filepath.Join(path, ".lease"), false); err == nil {
		t.Fatal("FIFO accepted")
	}
}

func TestUpdateCacheRealContainer(t *testing.T) {
	engine := os.Getenv("CHAINMAN_TEST_UPDATE_ENGINE")
	if engine == "" {
		t.Skip("set CHAINMAN_TEST_UPDATE_ENGINE to an absolute Docker/Podman path")
	}
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Engines = []string{engine}
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	// Deliberately omit the label to exercise old runtime mount inspection.
	// Use the already-provisioned native Nix image; a cached Alpine tag can
	// refer to another architecture and die before inspection on an ARM host.
	out, err := exec.Command(engine, "run", "--pull=never", "--rm", "--detach", "--mount", "type=bind,src="+path+",dst=/candidate,readonly", "--entrypoint", "sh", "nixos/nix:2.33.3", "-c", "sleep 60").Output()
	if err != nil {
		t.Fatalf("container start: %s %v", out, err)
	}
	id := strings.TrimSpace(string(out))
	t.Cleanup(func() { exec.Command(engine, "rm", "--force", id).Run() })
	entries, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 || !entries[0].Active {
		detail, _ := exec.Command(engine, "inspect", "--format", "{{json .State}} {{json .Mounts}}", id).CombinedOutput()
		t.Fatalf("live mount: %v %v; id=%s path=%s details=%s", removed, err, id, path, detail)
	}
	if out, err = exec.Command(engine, "rm", "--force", id).CombinedOutput(); err != nil {
		t.Fatalf("remove fixture: %s %v", out, err)
	}
	_, removed, err = updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 1 {
		t.Fatalf("released mount: %v %v", removed, err)
	}
}

func TestUpdateCacheMaintenanceDoesNotMaskFailure(t *testing.T) {
	base := t.TempDir()
	script := quote(os.Args[0]) + " update-cache engine " + quote(base) + " \"$CHAINMAN_UPDATE_TRANSACTION\" /nonexistent/engine >/dev/null; exit 23"
	out, err := updateTestCommand(t, base, script).CombinedOutput()
	if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 23 {
		t.Fatalf("original failure masked: %v %s", err, out)
	}
	if !strings.Contains(string(out), "maintenance:") {
		t.Fatalf("missing cleanup diagnostic: %s", out)
	}
	paths, _ := filepath.Glob(filepath.Join(base, "v1/candidate.*"))
	if len(paths) != 1 {
		t.Fatalf("uninspected transaction deleted: %v", paths)
	}
}

func TestUpdateCachePartialEngineInventory(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	engine := filepath.Join(t.TempDir(), "engine")
	receipt, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Engines = []string{engine}
	for _, mounted := range []bool{true, false} {
		body := "[]"
		if mounted {
			data, e := json.Marshal([]map[string]any{{"Mounts": []map[string]string{{"Source": path}}}})
			if e != nil {
				t.Fatal(e)
			}
			body = string(data)
		}
		script := "#!/bin/sh\nif [ \"$1\" = inspect ]; then printf '%s' " + quote(body) + "; exit 1; fi\nif [ \"$#\" = 3 ]; then echo transient; fi\n"
		if err = os.WriteFile(engine, []byte(script), 0700); err != nil {
			t.Fatal(err)
		}
		active, e := updateContainers(receipt, path)
		if mounted && (!active || e != nil) {
			t.Fatalf("lost positive witness: %v %v", active, e)
		}
		if !mounted && e == nil {
			t.Fatal("partial negative inventory authorized deletion")
		}
	}
}
