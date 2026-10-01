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

func TestUpdateCacheLegacySupervisorWithNewWatchHelper(t *testing.T) {
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
			engine := filepath.Join(t.TempDir(), "docker")
			if err := os.WriteFile(engine, []byte("#!/bin/sh\ncase \"$1\" in info) echo fixture-daemon;; ps) exit 0;; *) exit 1;; esac\n"), 0700); err != nil {
				t.Fatal(err)
			}
			// The bootstrap deliberately keeps CHAINMAN_UPDATE_HELPER pointing at
			// its original supervisor, even while executing the candidate runtime.
			registration := `"$CHAINMAN_UPDATE_HELPER" update-cache engine ` + quote(base) + ` "$CHAINMAN_UPDATE_TRANSACTION" ` + quote(engine) + " >/dev/null\n"
			worker := "set -eu\n" + quote(os.Args[0]) + " build " + quote(state) + " fixture\n" + registration
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
			if err != nil || r.Schema != 1 || r.Complete || len(r.Engines) != 1 || r.Engines[0] != engine {
				t.Fatalf("legacy receipt not preserved: %+v %v", r, err)
			}
			if scenario == "resume" {
				// Resume and another registration must still work through the
				// original supervisor after the newer helper has finished.
				cmd = exec.Command(legacy, "update-cache", "run", base, paths[0], "deps-update", "/bin/sh", "-eu", "-c", registration)
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
	if migrated.Schema != updateSchema || migrated.Token != r.Token || !migrated.Touched.Equal(r.Touched) {
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
