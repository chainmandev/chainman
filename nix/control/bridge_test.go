package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func bridgeFixture(t *testing.T) (*Bridge, string, string) {
	t.Helper()
	root := t.TempDir()
	state, mutations := filepath.Join(root, "network.json"), filepath.Join(root, "mutations")
	b := &Bridge{Engine: filepath.Join(root, "engine"), Scope: strings.Repeat("a", 24)}
	b.Name = "chainman-" + b.Scope
	expected, _ := json.Marshal([]BridgeState{{ID: strings.Repeat("b", 64), Name: b.Name, Driver: "bridge", Labels: map[string]string{"dev.chainman.scope": b.Scope, "dev.chainman.network": "1"}}})
	body := "#!/bin/sh\nset -eu\n[ \"$1\" = network ]; shift\ncase \"$1\" in\ninspect) test -f " + quote(state) + "; cat " + quote(state) + ";;\nls) if test -f " + quote(state) + "; then echo " + quote(b.Name) + "; fi;;\nrm) [ \"$#\" = 2 ]; [ \"$2\" = " + quote(strings.Repeat("b", 64)) + " ]; echo rm >> " + quote(mutations) + "; test ! -f " + quote(filepath.Join(root, "in-use")) + "; rm " + quote(state) + ";;\ncreate) echo create >> " + quote(mutations) + "; printf '%s' " + quote(string(expected)) + " > " + quote(state) + ";;\n*) exit 2;;\nesac\n"
	if e := os.WriteFile(b.Engine, []byte(body), 0700); e != nil {
		t.Fatal(e)
	}
	return b, state, mutations
}

func TestBridgeReuseAndRemovalUseOwnedImmutableID(t *testing.T) {
	b, state, mutations := bridgeFixture(t)
	if e := ensureBridge(b, false); e != nil {
		t.Fatal(e)
	}
	if e := ensureBridge(b, true); e != nil {
		t.Fatal(e)
	}
	if e := removeBridge(b); e != nil {
		t.Fatal(e)
	}
	if e := removeBridge(b); e != nil {
		t.Fatal(e)
	}
	log, e := os.ReadFile(mutations)
	if e != nil || string(log) != "create\nrm\n" {
		t.Fatalf("unexpected mutations: %q %v", log, e)
	}
	if _, e := os.Stat(state); !os.IsNotExist(e) {
		t.Fatal("network retained")
	}
	if e := ensureBridge(b, true); e == nil {
		t.Fatal("recreated disappeared active network")
	}
}

func TestBridgeRefusesForeignNetworksAndEngineOutages(t *testing.T) {
	for _, kind := range []string{"foreign", "driver", "outage"} {
		t.Run(kind, func(t *testing.T) {
			b, state, mutations := bridgeFixture(t)
			if kind == "outage" {
				if e := os.WriteFile(b.Engine, []byte("#!/bin/sh\nexit 1\n"), 0700); e != nil {
					t.Fatal(e)
				}
			} else {
				driver, owner := "bridge", b.Scope
				if kind == "foreign" {
					owner = "other"
				} else {
					driver = "host"
				}
				body, _ := json.Marshal([]BridgeState{{ID: "foreign-id", Name: b.Name, Driver: driver, Labels: map[string]string{"dev.chainman.scope": owner, "dev.chainman.network": "1"}}})
				if e := os.WriteFile(state, body, 0600); e != nil {
					t.Fatal(e)
				}
			}
			if e := ensureBridge(b, false); e == nil {
				t.Fatal("unsafe adoption accepted")
			}
			if e := removeBridge(b); e == nil {
				t.Fatal("unsafe removal accepted")
			}
			if _, e := os.Stat(mutations); !os.IsNotExist(e) {
				t.Fatal("refusal mutated network")
			}
		})
	}
}

func TestBridgeRemovalNeverDisconnectsExternalEndpoints(t *testing.T) {
	b, state, mutations := bridgeFixture(t)
	if e := ensureBridge(b, false); e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(filepath.Join(filepath.Dir(state), "in-use"), nil, 0600); e != nil {
		t.Fatal(e)
	}
	if e := removeBridge(b); e == nil {
		t.Fatal("removed externally referenced network")
	}
	if _, e := os.Stat(state); e != nil {
		t.Fatal("busy network disappeared")
	}
	log, _ := os.ReadFile(mutations)
	if string(log) != "create\nrm\n" {
		t.Fatalf("unexpected mutations: %q", log)
	}
}

func TestRepositoryOnlyStartAndTaskHaveSameEngineFingerprint(t *testing.T) {
	engine := filepath.Join(t.TempDir(), "docker")
	if e := os.WriteFile(engine, []byte("#!/bin/sh\n[ \"$1\" = info ] || exit 1\necho fixture-engine\n"), 0700); e != nil {
		t.Fatal(e)
	}
	up := Plan{Fingerprint: "fixture", Resources: []Plan{{Bridge: &Bridge{Engine: engine}}, {Services: map[string]Service{"database": {Container: &Container{Engine: engine}}}}}}
	run := up
	run.TaskContainer = &Container{Engine: engine}
	if e := bindEngines(&up); e != nil {
		t.Fatal(e)
	}
	if e := bindEngines(&run); e != nil {
		t.Fatal(e)
	}
	if up.Fingerprint != run.Fingerprint || up.Fingerprint == "fixture" {
		t.Fatal("task existence changed the shared engine fingerprint", up.Fingerprint, run.Fingerprint)
	}
}
