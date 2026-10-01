package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Freeze the complete receipt manager from 61268b9 rather than testing old JSON
// through today's reader. The overlay shares unrelated native plumbing but
// replaces allocation, registration, finalization, resume and collection with
// the original implementation. No Git history or network is needed at test time.
func legacyUpdateControl(t *testing.T) string {
	t.Helper()
	source, err := filepath.Abs("update_cache.go")
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile("testdata/update-cache-v1.go")
	if err != nil {
		t.Fatal(err)
	}
	// Other commands in the current package reference this newer function. This
	// fixture only executes update-cache, whose frozen code never calls it.
	data = append(data, []byte("\nfunc forwardUpdateLease(cmd *exec.Cmd) error { panic(\"legacy fixture only supports update-cache\") }\n")...)
	directory := t.TempDir()
	replacement := filepath.Join(directory, "update_cache.go")
	if err = os.WriteFile(replacement, data, 0600); err != nil {
		t.Fatal(err)
	}
	overlay := filepath.Join(directory, "overlay.json")
	if err = atomic(overlay, map[string]any{"Replace": map[string]string{source: replacement}}); err != nil {
		t.Fatal(err)
	}
	binary := filepath.Join(directory, "control-v1")
	cmd := exec.Command("go", "build", "-mod=readonly", "-buildvcs=false", "-overlay", overlay, "-o", binary, ".")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build frozen receipt manager: %s %v", out, err)
	}
	return binary
}

func TestUpdateCacheLegacyHostSupervisorWithNewWatchHelper(t *testing.T) {
	legacy := legacyUpdateControl(t)
	for _, scenario := range []string{"success", "resume", "prune"} {
		t.Run(scenario, func(t *testing.T) {
			base, state := t.TempDir(), t.TempDir()
			plan := Plan{Schema: 1, Root: state, State: state, Services: map[string]Service{
				"fixture": {Watch: &Watch{Build: Command{Argv: []string{"/bin/true"}, Directory: state}}},
			}}
			if err := atomic(filepath.Join(state, "plan.json"), plan); err != nil {
				t.Fatal(err)
			}
			// The bootstrap deliberately keeps CHAINMAN_UPDATE_HELPER pointing at
			// its original supervisor, even while executing the candidate runtime.
			readReceipt := `"$CHAINMAN_UPDATE_HELPER" update-cache status ` + quote(base) + " >/dev/null\n"
			worker := "set -eu\n" + quote(os.Args[0]) + " build " + quote(state) + " fixture\n" + readReceipt
			want := 0
			if scenario != "success" {
				want = 23
				worker += "exit 23\n"
			}
			cmd := updateTestCommand(t, base, worker)
			cmd.Path, cmd.Args[0] = legacy, legacy
			cmd.Env = append(cmd.Env, "CHAINMAN_UPDATE_HELPER="+legacy)
			out, err := cmd.CombinedOutput()
			if got := exitCode(err); got != want || strings.Contains(string(out), "unknown update receipt") {
				t.Fatalf("legacy supervisor exit %d, want %d: %s", got, want, out)
			}
			paths, err := filepath.Glob(filepath.Join(base, "v1/candidate.*"))
			if err != nil {
				t.Fatal(err)
			}
			if scenario == "success" {
				if len(paths) != 0 {
					t.Fatalf("successful legacy update leaked its candidate: %v\n%s", paths, out)
				}
				return
			}
			if len(paths) != 1 {
				t.Fatalf("failed candidate not retained: %v\n%s", paths, out)
			}
			r, err := updateRead(paths[0])
			if err != nil || r.Schema != 1 || r.Complete || len(r.Engines) != 1 || r.Engines[0] != updateLegacyGuard(paths[0]) {
				t.Fatalf("legacy receipt not preserved: %+v %v", r, err)
			}
			if scenario == "resume" {
				// Resume and further reads must still work through the
				// original supervisor after the newer helper has finished.
				cmd = exec.Command(legacy, "update-cache", "run", base, paths[0], "deps-update", "/bin/sh", "-eu", "-c", readReceipt)
			} else {
				cmd = exec.Command(legacy, "update-cache", "prune", base, "--all")
			}
			cmd.Env = append(updateTestCommand(t, base, "").Env, "CHAINMAN_UPDATE_HELPER="+legacy)
			if out, err = cmd.CombinedOutput(); err != nil {
				t.Fatalf("legacy %s failed: %s %v", scenario, out, err)
			}
			if scenario == "prune" {
				var report struct{ Removed []string }
				if err = json.Unmarshal(out, &report); err != nil || len(report.Removed) != 1 || report.Removed[0] != paths[0] {
					t.Fatalf("legacy prune skipped candidate: %s %v", out, err)
				}
			}
			if _, err = os.Stat(paths[0]); !os.IsNotExist(err) {
				t.Fatalf("legacy %s retained candidate: %v", scenario, err)
			}
		})
	}
}

func TestUpdateCacheLegacyMigrationRequiresIdleResume(t *testing.T) {
	base := t.TempDir()
	path, lease := updateFixture(t, base, 0, 0)
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Schema = 1
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CHAINMAN_UPDATE_TRANSACTION", path)
	cmd := exec.Command("/bin/true")
	if err = forwardUpdateLease(cmd); err != nil {
		t.Fatal(err)
	}
	closeForwarded(cmd)
	before, err := os.ReadFile(filepath.Join(path, ".transaction.json"))
	if err != nil {
		t.Fatal(err)
	}
	engine := filepath.Join(t.TempDir(), "docker")
	if err = os.WriteFile(engine, []byte("#!/bin/sh\necho fixture-daemon\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if got := updateCacheAction([]string{"engine", base, path, engine}); got == 0 {
		t.Fatal("new helper implicitly migrated a legacy transaction")
	}
	if _, admitted, _, err := updateStart(base, path); err == nil {
		admitted.Close()
		t.Fatal("resumed a legacy transaction while its owner was active")
	}
	after, err := os.ReadFile(filepath.Join(path, ".transaction.json"))
	if err != nil || string(after) != string(before) {
		t.Fatalf("changed active legacy receipt: %s %v", after, err)
	}
	lease.Close()
	_, resumed, migrated, err := updateStart(base, path)
	if err != nil {
		t.Fatal(err)
	}
	defer resumed.Close()
	if migrated.Schema != updateSchema || migrated.Token != r.Token || !migrated.Touched.Equal(r.Touched) || len(migrated.Engines) != 0 {
		t.Fatalf("idle resume did not preserve transaction identity: %+v", migrated)
	}
	if got := updateCacheAction([]string{"engine", base, path, engine}); got != 0 {
		t.Fatalf("resumed supervisor cannot register engine: %d", got)
	}
	r, err = updateRead(path)
	if err != nil || r.EngineIdentities[engine] != "fixture-daemon" {
		t.Fatalf("resumed supervisor lost daemon identity: %+v %v", r, err)
	}
	if _, removed, err := updateCollect(base, true, true, time.Now(), 0); err != nil || len(removed) != 0 {
		t.Fatalf("resumed transaction lost its lease: %v %v", removed, err)
	}
}

func TestUpdateCacheLegacyCollectorCannotFollowChangedDaemon(t *testing.T) {
	legacy := legacyUpdateControl(t)
	for _, order := range []string{"register-helper-switch", "helper-register-switch", "register-switch-helper"} {
		t.Run(order, func(t *testing.T) {
			base, state := t.TempDir(), t.TempDir()
			plan := Plan{Schema: 1, Root: state, State: state, Services: map[string]Service{
				"fixture": {Watch: &Watch{Build: Command{Argv: []string{"/bin/true"}, Directory: state}}},
			}}
			if err := atomic(filepath.Join(state, "plan.json"), plan); err != nil {
				t.Fatal(err)
			}
			switched := filepath.Join(state, "different-context")
			engine := filepath.Join(state, "docker")
			// The original daemon retains a transaction container. Changing the
			// selected context only changes which daemon this executable queries.
			body := "#!/bin/sh\ndaemon=original\n[ ! -f " + quote(switched) + " ] || daemon=different\n" +
				"case \"$1\" in info) echo \"$daemon\";; ps) if [ \"$daemon\" = original ]; then echo original-container; fi;; *) exit 1;; esac\n"
			if err := os.WriteFile(engine, []byte(body), 0700); err != nil {
				t.Fatal(err)
			}
			steps := map[string]string{
				"register": quote(legacy) + " update-cache engine " + quote(base) + ` "$CHAINMAN_UPDATE_TRANSACTION" ` + quote(engine) + " >/dev/null\n",
				"helper":   quote(os.Args[0]) + " build " + quote(state) + " fixture\n",
				"switch":   "touch " + quote(switched) + "\n",
			}
			worker := "set -eu\n"
			for _, step := range strings.Split(order, "-") {
				worker += steps[step]
			}
			cmd := updateTestCommand(t, base, worker)
			cmd.Path, cmd.Args[0] = legacy, legacy
			if out, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("legacy supervisor could not finish: %s %v", out, err)
			}
			paths, err := filepath.Glob(filepath.Join(base, "v1/candidate.*"))
			if err != nil || len(paths) != 1 {
				t.Fatalf("legacy collector deleted a candidate still used on the original daemon: %v %v", paths, err)
			}
			r, err := updateRead(paths[0])
			if err != nil || r.Schema != 1 || !r.Complete {
				t.Fatalf("protection broke legacy finalization: %+v %v", r, err)
			}
			out, err := exec.Command(legacy, "update-cache", "prune", base, "--all").CombinedOutput()
			var report struct {
				Transactions []updateEntry
				Removed      []string
			}
			if err != nil || json.Unmarshal(out, &report) != nil || len(report.Removed) != 0 || len(report.Transactions) != 1 || !report.Transactions[0].Active {
				t.Fatalf("old --all bypassed protection: %s %v", out, err)
			}
			entries, removed, err := updateCollect(base, true, true, time.Now(), 0)
			if err == nil || !strings.Contains(err.Error(), "no recorded daemon identity") || len(removed) != 0 || len(entries) != 1 || entries[0].Eligible {
				t.Fatalf("new collector adopted the current daemon: %+v %v %v", entries, removed, err)
			}
			if out, err := exec.Command(legacy, "update-cache", "run", base, paths[0], "deps-update", "/bin/true").CombinedOutput(); err == nil || !strings.Contains(string(out), "candidate has containers") {
				t.Fatalf("old resume ignored the unbound daemon: %s %v", out, err)
			}
			// The witness must protect just this candidate, not make the old
			// collector abort maintenance for independent host-only transactions.
			cmd = updateTestCommand(t, base, "exit 0")
			cmd.Path, cmd.Args[0] = legacy, legacy
			if out, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("independent legacy update failed: %s %v", out, err)
			}
			remaining, err := filepath.Glob(filepath.Join(base, "v1/candidate.*"))
			if err != nil || len(remaining) != 1 || remaining[0] != paths[0] {
				t.Fatalf("protected legacy candidate blocked independent cleanup: %v %v", remaining, err)
			}
		})
	}
}

func TestUpdateCacheLegacyGuardOnlyAcceptsHostOnlyReceipt(t *testing.T) {
	// Exercise JSON escaping and shell quoting independently, including a
	// literal newline. The guard must never execute receipt or path contents.
	base := filepath.Join(t.TempDir(), "space ' quote\" backslash\\ newline\n <&>")
	path, _ := updateFixture(t, base, 0, 0)
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Schema = 1
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CHAINMAN_UPDATE_TRANSACTION", path)
	cmd := exec.Command("/bin/true")
	if err = forwardUpdateLease(cmd); err != nil {
		t.Fatal(err)
	}
	defer closeForwarded(cmd)
	r, err = updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	encode := func(engines []string, pretty bool) []byte {
		t.Helper()
		r.Engines = engines
		var data []byte
		var err error
		if pretty {
			data, err = json.MarshalIndent(r, "", "  ")
		} else {
			data, err = json.Marshal(r)
		}
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	guard := updateLegacyGuard(path)
	for _, test := range []struct {
		name   string
		data   []byte
		active bool
	}{
		{"host-only", encode([]string{guard}, false), false},
		{"registered-engine", encode([]string{guard, "/missing/docker"}, false), true},
		{"guard-last", encode([]string{"/missing/docker", guard}, false), true},
		{"embedded-suffix", encode([]string{guard, `/missing/","engines":["` + guard + `"]}`}, false), true},
		{"reformatted", encode([]string{guard}, true), true},
		{"truncated", []byte(`{"schema":1`), true},
		{"missing", nil, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			if test.data == nil {
				err = os.Remove(filepath.Join(path, ".transaction.json"))
			} else {
				err = os.WriteFile(filepath.Join(path, ".transaction.json"), test.data, 0600)
			}
			if err != nil {
				t.Fatal(err)
			}
			cmd := exec.Command(guard, "ps", "--all", "--quiet")
			cmd.Env = []string{"PATH=/missing"}
			out, err := cmd.Output()
			if err != nil || (len(out) != 0) != test.active {
				t.Fatalf("guard active=%t, want %t: %q %v", len(out) != 0, test.active, out, err)
			}
		})
	}
}

func TestUpdateCacheLegacyMaintenanceProtectsOldWriters(t *testing.T) {
	legacy := legacyUpdateControl(t)
	base := t.TempDir()
	path, lease := updateFixture(t, base, 25*time.Hour, 0)
	r, err := updateRead(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Schema, r.Engines = 1, []string{"/missing/docker"}
	if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
		t.Fatal(err)
	}
	lease.Close()
	before, err := os.ReadFile(filepath.Join(path, ".transaction.json"))
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = updateCollect(base, false, false, time.Now(), updateLimit)
	if err == nil {
		t.Fatal("status adopted an unbound engine")
	}
	after, err := os.ReadFile(filepath.Join(path, ".transaction.json"))
	if err != nil || string(after) != string(before) {
		t.Fatalf("status mutated legacy receipt: %s %v", after, err)
	}
	if _, removed, err := updateCollect(base, true, true, time.Now(), 0); err == nil || len(removed) != 0 {
		t.Fatalf("maintenance collected unbound candidate: %v %v", removed, err)
	}
	// Maintenance also guards legacy candidates that have never launched a
	// newer service helper. Future old writers must preserve that protection.
	if out, err := exec.Command(legacy, "update-cache", "engine", base, path, "/missing/podman").CombinedOutput(); err != nil {
		t.Fatalf("guard broke old registration: %s %v", out, err)
	}
	r, err = updateRead(path)
	if err != nil || r.Schema != 1 || len(r.Engines) != 3 || r.Engines[0] != updateLegacyGuard(path) {
		t.Fatalf("old writer dropped protection: %+v %v", r, err)
	}
	out, err := exec.Command(legacy, "update-cache", "prune", base, "--all").CombinedOutput()
	var report struct {
		Transactions []updateEntry
		Removed      []string
	}
	if err != nil || json.Unmarshal(out, &report) != nil || len(report.Removed) != 0 || len(report.Transactions) != 1 || !report.Transactions[0].Active {
		t.Fatalf("legacy collector bypassed maintenance guard: %s %v", out, err)
	}
}
