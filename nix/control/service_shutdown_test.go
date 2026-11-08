package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A separate owned process group keeps fixture cleanup away from the test runner.
func shutdownFixtureOwner(t *testing.T, p Plan, name string) Identity {
	t.Helper()
	cmd := exec.Command("sleep", "60")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	id, err := identify(cmd.Process.Pid)
	if err != nil {
		_ = cmd.Process.Kill()
		<-done
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if id.alive() {
			_ = syscall.Kill(-id.PID, syscall.SIGKILL)
		}
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("owned shutdown fixture did not exit")
		}
	})
	if err := atomic(filepath.Join(p.State, name+".owner.json"), Owner{Identity: id}); err != nil {
		t.Fatal(err)
	}
	return id
}

func shutdownFixturePlan(t *testing.T) Plan {
	t.Helper()
	// exec/sequence reexec this race-instrumented test binary. Its default
	// one-second successful-exit sleep is sanitizer teardown, not hook work.
	// Keep race checks and caller options while excluding that fixture delay.
	t.Setenv("GORACE", strings.TrimSpace(os.Getenv("GORACE")+" atexit_sleep_ms=0"))
	root := physicalTempDir(t)
	p := Plan{Schema: 1, Root: root, State: filepath.Join(root, "state"), Backend: "/bin/false", Fingerprint: "shutdown-fixture", Services: map[string]Service{}}
	if err := private(p.State); err != nil {
		t.Fatal(err)
	}
	return p
}

func assertShutdownOwnerReleased(t *testing.T, p Plan, name string, id Identity) {
	t.Helper()
	if id.alive() {
		t.Fatalf("shutdown retained owned service %s", name)
	}
	if _, err := os.Stat(filepath.Join(p.State, name+".owner.json")); !os.IsNotExist(err) {
		t.Fatalf("shutdown retained owner receipt for %s: %v", name, err)
	}
}

func TestShutdownHooksPersistInReverseDependencyOrderBeforeOwnersExit(t *testing.T) {
	p := shutdownFixturePlan(t)
	order := filepath.Join(p.Root, "checkpoint-order")
	owners := map[string]Identity{}
	for index, name := range []string{"database", "api", "website"} {
		snapshot := filepath.Join(p.Root, name+".snapshot")
		hook := Command{Argv: []string{"/bin/sh", "-eu", "-c", `kill -0 "$1"; printf '%s\n' "$2" >> "$3"; printf '%s' "$2" > "$4"`, "fixture", "pending", name, order, snapshot}, Directory: p.Root}
		s := Service{Command: Command{Argv: []string{"sleep", "60"}, Directory: p.Root}, ShutdownCommand: &hook, Restart: "no", Shutdown: 1}
		if index > 0 {
			s.Dependencies = []string{[]string{"database", "api"}[index-1]}
		}
		p.Services[name] = s
		owners[name] = shutdownFixtureOwner(t, p, name)
		hook.Argv[5] = strconv.Itoa(owners[name].PID)
	}
	if err := releaseUnused(p, true); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(order)
	if err != nil || string(body) != "website\napi\ndatabase\n" {
		t.Fatalf("checkpoint order %q: %v", body, err)
	}
	for name, owner := range owners {
		body, err := os.ReadFile(filepath.Join(p.Root, name+".snapshot"))
		if err != nil || string(body) != name {
			t.Fatalf("missing persisted checkpoint for %s: %q %v", name, body, err)
		}
		assertShutdownOwnerReleased(t, p, name, owner)
	}
}

func TestShutdownHookFailurePreservesSnapshotAndSurvivesBridgeCleanup(t *testing.T) {
	p := shutdownFixturePlan(t)
	bridge, bridgeState, _ := bridgeFixture(t)
	if err := ensureBridge(bridge, false); err != nil {
		t.Fatal(err)
	}
	p.Bridge = bridge
	temporary := physicalTempDir(t)
	t.Setenv("TMPDIR", temporary)
	good := filepath.Join(p.Root, "last-good.snapshot")
	if err := os.WriteFile(good, []byte("previous complete generation"), 0600); err != nil {
		t.Fatal(err)
	}
	pending := filepath.Join(p.Root, "snapshot.pending")
	hook := Command{Argv: []string{"/bin/sh", "-c", `printf incomplete > "$1"; exit 7`, "fixture", pending}, Directory: p.Root}
	p.Services["database"] = Service{Command: Command{Argv: []string{"sleep", "60"}, Directory: p.Root}, ShutdownCommand: &hook, Restart: "no", Shutdown: 1}
	owner := shutdownFixtureOwner(t, p, "database")
	err := releaseUnused(p, true)
	if err == nil || !strings.Contains(err.Error(), "shutdown hook database failed") {
		t.Fatalf("bridge cleanup discarded shutdown failure: %v", err)
	}
	assertShutdownOwnerReleased(t, p, "database", owner)
	for _, pattern := range []string{"chainman-shutdown-*", "chainman-command-*"} {
		if paths, err := filepath.Glob(filepath.Join(temporary, pattern)); err != nil || len(paths) != 0 {
			t.Fatalf("failed shutdown retained temporary command state: %v %v", paths, err)
		}
	}
	body, readErr := os.ReadFile(good)
	if readErr != nil || string(body) != "previous complete generation" {
		t.Fatalf("failed checkpoint replaced last successful snapshot: %q %v", body, readErr)
	}
	if _, err := os.Stat(bridgeState); !os.IsNotExist(err) {
		t.Fatalf("hook failure prevented owned bridge cleanup: %v", err)
	}
}

func TestShutdownHookFailureStillRunsDependencyCheckpoint(t *testing.T) {
	p := shutdownFixturePlan(t)
	checkpoint := filepath.Join(p.Root, "dependency-checkpoint")
	good := Command{Argv: []string{"/bin/sh", "-c", `printf saved > "$1"`, "fixture", checkpoint}, Directory: p.Root}
	bad := Command{Argv: []string{"/bin/sh", "-c", "exit 9"}, Directory: p.Root}
	p.Services["database"] = Service{Command: Command{Argv: []string{"sleep", "60"}}, ShutdownCommand: &good, Restart: "no", Shutdown: 1}
	p.Services["website"] = Service{Command: Command{Argv: []string{"sleep", "60"}}, ShutdownCommand: &bad, Dependencies: []string{"database"}, Restart: "no", Shutdown: 1}
	owners := map[string]Identity{}
	for _, name := range []string{"database", "website"} {
		owners[name] = shutdownFixtureOwner(t, p, name)
	}
	if err := releaseUnused(p, true); err == nil || !strings.Contains(err.Error(), "website failed") {
		t.Fatalf("shutdown lost dependent failure: %v", err)
	}
	if body, err := os.ReadFile(checkpoint); err != nil || string(body) != "saved" {
		t.Fatalf("dependent hook failure suppressed database checkpoint: %q %v", body, err)
	}
	for name, owner := range owners {
		assertShutdownOwnerReleased(t, p, name, owner)
	}
}

func TestShutdownHookTimeoutKillsItsWorkloadAndStillReleasesOwner(t *testing.T) {
	p := shutdownFixturePlan(t)
	pidPath := filepath.Join(p.Root, "hook.pid")
	hook := Command{Argv: []string{"/bin/sh", "-c", `trap '' TERM; echo $$ > "$1"; while :; do sleep 1; done`, "fixture", pidPath}, Directory: p.Root}
	p.Services["database"] = Service{Command: Command{Argv: []string{"sleep", "60"}}, ShutdownCommand: &hook, Restart: "no", Shutdown: 1}
	owner := shutdownFixtureOwner(t, p, "database")
	started := time.Now()
	err := releaseUnused(p, true)
	if err == nil || !strings.Contains(err.Error(), "shutdown hook database failed") {
		t.Fatalf("unbounded hook did not report timeout: %v", err)
	}
	if elapsed := time.Since(started); elapsed > 12*time.Second {
		t.Fatalf("hook exceeded timeout and bounded shutdown grace: %v", elapsed)
	}
	assertShutdownOwnerReleased(t, p, "database", owner)
	body, err := os.ReadFile(pidPath)
	if err != nil {
		t.Fatal("timed hook never entered its workload:", err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(body)))
	if err != nil {
		t.Fatal(err)
	}
	if remaining, err := identify(pid); err == nil && remaining.alive() {
		t.Fatalf("timed-out hook workload survived: %+v", remaining)
	}
}

func TestShutdownHookRequiresLiveOwnedReceipt(t *testing.T) {
	p := shutdownFixturePlan(t)
	marker := filepath.Join(p.Root, "unowned-hook")
	hook := Command{Argv: []string{"/bin/sh", "-c", `printf ran > "$1"`, "fixture", marker}, Directory: p.Root}
	p.Services["database"] = Service{ShutdownCommand: &hook, Shutdown: 1}
	if err := shutdownService(p, "database"); err != nil {
		t.Fatal(err)
	}
	if err := atomic(filepath.Join(p.State, "database.owner.json"), Owner{Identity: Identity{PID: os.Getpid(), Birth: "stale"}}); err != nil {
		t.Fatal(err)
	}
	if err := shutdownService(p, "database"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("unowned or stale service executed a shutdown hook")
	}
}
