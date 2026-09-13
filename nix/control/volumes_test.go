package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func volumeFixture(t *testing.T) (Volume, string, string) {
	t.Helper()
	root := physicalTempDir(t)
	state := filepath.Join(root, "volume.json")
	mutations := filepath.Join(root, "mutations")
	v := Volume{Engine: filepath.Join(root, "engine"), Name: "chainman-fixture-data", Scope: strings.Repeat("a", 24), Compatibility: strings.Repeat("b", 64), Policy: "preserve"}
	expected, _ := json.Marshal([]VolumeState{{Name: v.Name, Labels: map[string]string{"dev.chainman.scope": v.Scope, "dev.chainman.compatibility": v.Compatibility}}})
	body := "#!/bin/sh\nset -eu\n[ \"$1\" = volume ]; shift\ncase \"$1\" in\ninspect) test -f " + quote(state) + "; cat " + quote(state) + ";;\nls) if test -f " + quote(state) + "; then echo " + quote(v.Name) + "; fi;;\nrm) echo rm >> " + quote(mutations) + "; test ! -f " + quote(filepath.Join(root, "in-use")) + "; rm " + quote(state) + ";;\ncreate) echo create >> " + quote(mutations) + "; printf '%s' " + quote(string(expected)) + " > " + quote(state) + ";;\n*) exit 2;;\nesac\n"
	if e := os.WriteFile(v.Engine, []byte(body), 0700); e != nil {
		t.Fatal(e)
	}
	return v, state, mutations
}

func TestVolumeOwnershipAndPreservationRefuseDestructiveChanges(t *testing.T) {
	for _, kind := range []string{"foreign", "preserve", "active"} {
		t.Run(kind, func(t *testing.T) {
			v, state, mutations := volumeFixture(t)
			owner := v.Scope
			if kind == "foreign" {
				owner = "foreign"
				v.Policy = "disposable"
			}
			if kind == "active" {
				v.Policy = "disposable"
			}
			body, _ := json.Marshal([]VolumeState{{Name: v.Name, Labels: map[string]string{"dev.chainman.scope": owner, "dev.chainman.compatibility": "old"}}})
			if e := os.WriteFile(state, body, 0600); e != nil {
				t.Fatal(e)
			}
			if e := ensureVolumes([]Volume{v}, kind == "active"); e == nil {
				t.Fatal("unsafe volume reuse accepted")
			}
			if _, e := os.Stat(mutations); !os.IsNotExist(e) {
				t.Fatal("refusal mutated a volume")
			}
		})
	}
}

func TestDisposableVolumeResetIsExplicitAndUnforced(t *testing.T) {
	v, state, mutations := volumeFixture(t)
	v.Policy = "disposable"
	body, _ := json.Marshal([]VolumeState{{Name: v.Name, Labels: map[string]string{"dev.chainman.scope": v.Scope, "dev.chainman.compatibility": "old"}}})
	if e := os.WriteFile(state, body, 0600); e != nil {
		t.Fatal(e)
	}
	if e := ensureVolumes([]Volume{v}, false); e != nil {
		t.Fatal(e)
	}
	if e := ensureVolumes([]Volume{v}, false); e != nil {
		t.Fatal(e)
	}
	log, e := os.ReadFile(mutations)
	if e != nil || string(log) != "rm\ncreate\n" {
		t.Fatalf("unexpected reset: %q %v", log, e)
	}
	if e = os.WriteFile(state, body, 0600); e != nil {
		t.Fatal(e)
	}
	if e = os.WriteFile(filepath.Join(filepath.Dir(state), "in-use"), nil, 0600); e != nil {
		t.Fatal(e)
	}
	if e = ensureVolumes([]Volume{v}, false); e == nil {
		t.Fatal("external volume use did not block recreation")
	}
	log, e = os.ReadFile(mutations)
	if e != nil || string(log) != "rm\ncreate\nrm\n" {
		t.Fatalf("recreated a busy volume: %q %v", log, e)
	}
}

func TestVolumeAbsenceMustBeConfirmedByHealthyEngine(t *testing.T) {
	v, _, _ := volumeFixture(t)
	if e := os.WriteFile(v.Engine, []byte("#!/bin/sh\nexit 1\n"), 0700); e != nil {
		t.Fatal(e)
	}
	if e := ensureVolumes([]Volume{v}, false); e == nil {
		t.Fatal("engine outage treated as absence")
	}
}

func resetFixture(t *testing.T) (Plan, Volume, string, string) {
	t.Helper()
	v, volumeState, mutations := volumeFixture(t)
	v.Services = []string{"database"}
	parent := physicalTempDir(t)
	p := Plan{Schema: 1, Root: filepath.Join(parent, "project"), State: filepath.Join(parent, "state"), Backend: "/bin/false", Fingerprint: "fixture", Services: map[string]Service{"database": {Command: Command{Argv: []string{"true"}}, Restart: "no", Shutdown: 1}}, Requested: []string{"database"}, Volumes: []Volume{v}}
	if e := private(p.State); e != nil {
		t.Fatal(e)
	}
	body, _ := json.Marshal([]VolumeState{{Name: v.Name, Labels: map[string]string{"dev.chainman.scope": v.Scope, "dev.chainman.compatibility": "old"}}})
	if e := os.WriteFile(volumeState, body, 0600); e != nil {
		t.Fatal(e)
	}
	return p, v, volumeState, mutations
}

func TestExplicitResetPreservedDataIsUnforcedAndDoesNotStartServices(t *testing.T) {
	p, _, state, mutations := resetFixture(t)
	if e := resetVolumes(p); e != nil {
		t.Fatal(e)
	}
	if _, e := os.Stat(state); !os.IsNotExist(e) {
		t.Fatal("preserved volume was not removed")
	}
	body, e := os.ReadFile(mutations)
	if e != nil || string(body) != "rm\n" {
		t.Fatalf("unexpected mutations: %q %v", body, e)
	}
	if e := resetVolumes(p); e != nil {
		t.Fatal("reset of absent volume:", e)
	}
}

func TestExplicitResetRefusesForeignVolumeAndLiveUsersBeforeMutation(t *testing.T) {
	for _, kind := range []string{"foreign", "missing-label", "active", "engine-changed"} {
		t.Run(kind, func(t *testing.T) {
			p, v, state, mutations := resetFixture(t)
			switch kind {
			case "foreign", "missing-label":
				labels := map[string]string{"dev.chainman.scope": v.Scope}
				if kind == "foreign" {
					labels["dev.chainman.scope"] = "foreign"
					labels["dev.chainman.compatibility"] = "old"
				}
				body, _ := json.Marshal([]VolumeState{{Name: v.Name, Labels: labels}})
				if e := os.WriteFile(state, body, 0600); e != nil {
					t.Fatal(e)
				}
			case "active":
				if e := atomic(filepath.Join(p.State, "plan.json"), p); e != nil {
					t.Fatal(e)
				}
				if e := atomic(filepath.Join(p.State, "fixture.lease"), Lease{Services: []string{"database"}, Persistent: true}); e != nil {
					t.Fatal(e)
				}
			case "engine-changed":
				old := p
				old.Volumes = append([]Volume(nil), p.Volumes...)
				old.Volumes[0].EngineIdentity = "old-daemon"
				if e := atomic(filepath.Join(p.State, "plan.json"), old); e != nil {
					t.Fatal(e)
				}
			}
			if e := resetVolumes(p); e == nil {
				t.Fatal("unsafe reset accepted")
			}
			if _, e := os.Stat(mutations); !os.IsNotExist(e) {
				t.Fatal("refusal mutated volume")
			}
		})
	}
}

func TestExplicitResetCannotForceExternallyReferencedVolume(t *testing.T) {
	p, _, state, mutations := resetFixture(t)
	if e := os.WriteFile(filepath.Join(filepath.Dir(state), "in-use"), nil, 0600); e != nil {
		t.Fatal(e)
	}
	if e := resetVolumes(p); e == nil {
		t.Fatal("busy engine volume removed")
	}
	if _, e := os.Stat(state); e != nil {
		t.Fatal("referenced volume disappeared")
	}
	body, _ := os.ReadFile(mutations)
	if string(body) != "rm\n" {
		t.Fatalf("unexpected engine mutations: %q", body)
	}
}

func TestExplicitResetValidatesRepositoryClaimsBeforeAnyVolumeRemoval(t *testing.T) {
	p, _, _, mutations := resetFixture(t)
	resource := p
	resource.State = filepath.Join(filepath.Dir(p.State), "repository")
	resource.Resources = nil
	if e := private(resource.State); e != nil {
		t.Fatal(e)
	}
	if e := atomic(filepath.Join(resource.State, "plan.json"), resource); e != nil {
		t.Fatal(e)
	}
	if e := atomic(filepath.Join(resource.State, "other.lease"), Lease{Services: []string{"database"}, Persistent: true}); e != nil {
		t.Fatal(e)
	}
	p.Resources = []Plan{resource}
	if e := resetVolumes(p); e == nil {
		t.Fatal("repository user did not block reset")
	}
	if _, e := os.Stat(mutations); !os.IsNotExist(e) {
		t.Fatal("worktree volume removed before checking repository users")
	}
}
