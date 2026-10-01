package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
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
	engine := filepath.Join(t.TempDir(), "docker")
	if err := os.WriteFile(engine, []byte("#!/bin/sh\nif [ \"$1\" = info ]; then echo daemon; else echo running-container; fi\n"), 0700); err != nil {
		t.Fatal(err)
	}
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Engines = []string{engine}
	r.EngineIdentities = map[string]string{engine: "daemon"}
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
	identity, err := engineIdentity(engine)
	if err != nil {
		t.Fatal(err)
	}
	r.EngineIdentities = map[string]string{engine: identity}
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
	engine := filepath.Join(t.TempDir(), "docker")
	offline := filepath.Join(t.TempDir(), "offline")
	if err := os.WriteFile(engine, []byte("#!/bin/sh\n[ ! -f "+quote(offline)+" ] || exit 1\necho daemon\n"), 0700); err != nil {
		t.Fatal(err)
	}
	script := quote(os.Args[0]) + " update-cache engine " + quote(base) + " \"$CHAINMAN_UPDATE_TRANSACTION\" " + quote(engine) + " >/dev/null; touch " + quote(offline) + "; exit 23"
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
	engine := filepath.Join(t.TempDir(), "docker")
	receipt, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Engines = []string{engine}
	receipt.EngineIdentities = map[string]string{engine: "daemon"}
	for _, mounted := range []bool{true, false} {
		body := "[]"
		if mounted {
			data, e := json.Marshal([]map[string]any{{"Mounts": []map[string]string{{"Source": path}}}})
			if e != nil {
				t.Fatal(e)
			}
			body = string(data)
		}
		script := "#!/bin/sh\nif [ \"$1\" = info ]; then echo daemon; exit; fi\nif [ \"$1\" = inspect ]; then printf '%s' " + quote(body) + "; exit 1; fi\nif [ \"$#\" = 3 ]; then echo transient; fi\n"
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

func TestUpdateCacheBindsDaemonBeforeRegistrationAndCollection(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	engine := filepath.Join(t.TempDir(), "docker")
	queried := filepath.Join(t.TempDir(), "queried")
	body := "#!/bin/sh\nif [ \"$1\" = info ]; then echo \"$DOCKER_HOST\"; else touch " + quote(queried) + "; fi\n"
	if err := os.WriteFile(engine, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DOCKER_HOST", "original")
	if got := updateCacheAction([]string{"engine", base, path, engine}); got != 0 {
		t.Fatalf("registration: %d", got)
	}
	r, err := updateRead(path)
	if err != nil || r.EngineIdentities[engine] != "original" || r.Schema != 2 {
		t.Fatalf("daemon identity not recorded: %+v %v", r, err)
	}
	t.Setenv("DOCKER_HOST", "different")
	if got := updateCacheAction([]string{"engine", base, path, engine}); got == 0 {
		t.Fatal("registration silently rebound the daemon")
	}
	if _, _, _, err = updateStart(base, path); err == nil {
		t.Fatal("resumed against a different daemon")
	}
	entries, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err == nil || len(removed) != 0 || len(entries) != 1 || entries[0].Error == "" || entries[0].Eligible {
		t.Fatalf("changed daemon authorized removal: %+v %v %v", entries, removed, err)
	}
	if _, err := os.Stat(queried); !os.IsNotExist(err) {
		t.Fatal("queried containers on the wrong daemon")
	}
	t.Setenv("DOCKER_HOST", "original")
	_, removed, err = updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 1 {
		t.Fatalf("restored daemon cannot collect idle candidate: %v %v", removed, err)
	}
}

func TestUpdateCacheMissingIdentityDoesNotAdoptCurrentDaemon(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Engines = []string{"/missing/docker"}
	r.Schema = 1
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	if got := updateCacheAction([]string{"engine", base, path, r.Engines[0]}); got == 0 {
		t.Fatal("adopted an unbound old receipt")
	}
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err == nil || len(removed) != 0 {
		t.Fatalf("unbound receipt collected: %v %v", removed, err)
	}
}

func TestUpdateCacheUnavailableEngineDoesNotBlockIndependentCleanup(t *testing.T) {
	base := t.TempDir()
	blocked, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	r, err := updateRead(blocked)
	if err != nil {
		t.Fatal(err)
	}
	r.Engines = []string{"/missing/docker"}
	r.EngineIdentities = map[string]string{r.Engines[0]: "original"}
	if err = atomic(filepath.Join(blocked, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	oversize, lease := updateFixture(t, base, time.Hour, 13<<30)
	lease.Close()
	expired, lease := updateFixture(t, base, 25*time.Hour, 10)
	lease.Close()
	cmd := exec.Command(os.Args[0], "update-cache", "prune", base)
	out, err := cmd.Output()
	var report struct {
		Transactions []updateEntry `json:"transactions"`
		Removed      []string      `json:"removed"`
	}
	if err == nil {
		t.Fatal("unavailable engine not reported")
	}
	if err = json.Unmarshal(out, &report); err != nil {
		t.Fatalf("partial result missing: %s %v", out, err)
	}
	if len(report.Removed) != 2 || !slices.Contains(report.Removed, oversize) || !slices.Contains(report.Removed, expired) {
		t.Fatalf("independent cleanup blocked: %s", out)
	}
	if _, err = os.Stat(blocked); err != nil {
		t.Fatal("uncertain candidate removed")
	}
	// A subsequent successful update must still be collected immediately.
	if out, err = updateTestCommand(t, base, "exit 0").CombinedOutput(); err != nil {
		t.Fatalf("independent update failed: %s %v", out, err)
	}
	paths, _ := filepath.Glob(filepath.Join(base, "v1/candidate.*"))
	if len(paths) != 1 || paths[0] != blocked {
		t.Fatalf("successful candidate leaked: %v", paths)
	}
}

func TestUpdateCacheIndependentServiceLeaseProtectsCandidate(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	t.Setenv("CHAINMAN_UPDATE_TRANSACTION", path)
	// A detached backend can advertise a descriptor it no longer inherited.
	t.Setenv("CHAINMAN_UPDATE_LEASE_FD", "999")
	cmd, err := child(Command{Argv: []string{"/bin/sh", "-c", "read ignored"}})
	if err != nil {
		t.Fatal(err)
	}
	if err = forwardUpdateLease(cmd); err != nil {
		t.Fatal(err)
	}
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	cmd.Stdin = reader
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	reader.Close()
	defer func() { writer.Close(); cmd.Wait() }()
	lease.Close()
	closeForwarded(cmd)
	if _, _, _, err = updateStart(base, path); err == nil {
		t.Fatal("resumed while a detached child still owns the candidate")
	}
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 {
		t.Fatalf("detached child lost ownership: %v %v", removed, err)
	}
	writer.Close()
	cmd.Wait()
	_, removed, err = updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 1 {
		t.Fatalf("finished child retained candidate: %v %v", removed, err)
	}
	// A stale service plan cannot recreate a collected transaction.
	if err = forwardUpdateLease(cmd); err == nil {
		t.Fatal("recreated a collected candidate")
	}
}

func TestUpdateCacheLeasedTaskRemapsAroundResourceDescriptor(t *testing.T) {
	base := t.TempDir()
	candidate, update := updateFixture(t, base, 0, 10)
	t.Setenv("CHAINMAN_UPDATE_TRANSACTION", candidate)
	t.Setenv("CHAINMAN_UPDATE_LEASE_FD", strconv.Itoa(int(update.Fd())))
	path := filepath.Join(leaseTestState(t), "client.lease")
	if err := atomic(path, Lease{TaskReceipt: true}); err != nil {
		t.Fatal(err)
	}
	resource, err := locked(path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer resource.Close()
	// /bin/sh has only single-digit redirections, so compare file identities in
	// the Go subprocess instead of assuming an inherited descriptor number.
	command := Command{Argv: []string{os.Args[0], "-test.run=^TestUpdateCacheDescriptorHelper$"}}
	if err = atomic(path+".command.json", command); err != nil {
		t.Fatal(err)
	}
	cmd, err := child(Command{Argv: []string{os.Args[0], "-test.run=^TestLeaseEntryHelper$"}, Environment: map[string]string{"CHAINMAN_TEST_LEASE_ENTRY": path}})
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stdout, cmd.Stderr = nil, nil
	cmd.ExtraFiles = []*os.File{resource}
	if err = forwardUpdateLease(cmd); err != nil {
		t.Fatal(err)
	}
	defer closeForwarded(cmd)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("leased task failed: %s %v", out, err)
	}
}

func TestUpdateCacheServicePreservesLegacyReceipt(t *testing.T) {
	legacy := legacyUpdateControl(t)
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 10)
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Schema = 1
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(filepath.Join(path, ".transaction.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err = syscall.Flock(int(lease.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CHAINMAN_UPDATE_TRANSACTION", path)
	t.Setenv("CHAINMAN_UPDATE_LEASE_FD", strconv.Itoa(int(lease.Fd())))
	cmd, err := child(Command{Argv: []string{"/bin/true"}})
	if err != nil {
		t.Fatal(err)
	}
	if err = forwardUpdateLease(cmd); err != nil {
		t.Fatalf("legacy supervisor prevented service ownership: %v", err)
	}
	defer closeForwarded(cmd)
	after, err := os.ReadFile(filepath.Join(path, ".transaction.json"))
	if err != nil || string(after) != string(before) {
		t.Fatalf("service changed its supervisor's receipt: %s %v", after, err)
	}
	lease.Close()
	_, removed, err := updateCollect(base, true, true, time.Now(), 0)
	if err != nil || len(removed) != 0 {
		t.Fatalf("legacy supervisor exit released live service: %v %v", removed, err)
	}
	// The original collector must honor independent shared service ownership
	// too, including after its own supervisor has released the exclusive lease.
	prune := func() []string {
		t.Helper()
		out, err := exec.Command(legacy, "update-cache", "prune", base, "--all").CombinedOutput()
		if err != nil {
			t.Fatalf("legacy prune: %s %v", out, err)
		}
		var report struct{ Removed []string }
		if err = json.Unmarshal(out, &report); err != nil {
			t.Fatal(err)
		}
		return report.Removed
	}
	if removed := prune(); len(removed) != 0 {
		t.Fatalf("old collector ignored surviving service: %v", removed)
	}
	closeForwarded(cmd)
	if removed := prune(); len(removed) != 1 || removed[0] != path {
		t.Fatalf("old collector retained finished service: %v", removed)
	}
}

func TestUpdateCacheDescriptorHelper(t *testing.T) {
	if os.Getenv("CHAINMAN_TEST_LEASE_ENTRY") == "" {
		return
	}
	fd, err := strconv.Atoi(os.Getenv("CHAINMAN_UPDATE_LEASE_FD"))
	if err != nil {
		t.Fatal(err)
	}
	file := os.NewFile(uintptr(fd), "update")
	defer file.Close()
	actual, err := file.Stat()
	if err != nil {
		t.Fatal(err)
	}
	want, err := os.Stat(filepath.Join(os.Getenv("CHAINMAN_UPDATE_TRANSACTION"), ".lease"))
	if err != nil || !os.SameFile(actual, want) {
		t.Fatalf("update descriptor refers to the resource lease: %v", err)
	}
}
