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
	root := t.TempDir()
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
