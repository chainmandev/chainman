package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestInternalProbeEntryAllowanceRejectsOtherCommands(t *testing.T) {
	root := physicalTempDir(t)
	command := Command{Argv: []string{"/fixture/launch", "_workflow-service", "web", "fixture"}, Directory: root}
	p := Plan{Schema: 1, Root: root, State: physicalTempDir(t), Backend: "/bin/true", Fingerprint: "fixture-engine-identity", Services: map[string]Service{}, Requested: []string{"web"}, Task: command}
	probe := Command{Argv: []string{"/fixture/launch", "_workflow-probe", "web", "fixture"}, Directory: root}
	for _, tc := range []struct {
		name  string
		entry int
		argv  []string
		http  *HTTPProbe
		valid bool
	}{
		{"host", 10, probe.Argv, nil, true},
		{"container-nix", 10, []string{"/engine", "exec", "container", "/fixture/launch", "_workflow-probe", "web", "fixture"}, nil, true},
		{"raw", 0, []string{"/bin/true"}, nil, true},
		{"direct-container", 0, []string{"/engine", "exec", "container", "/bin/true"}, nil, true},
		{"http", 0, nil, &HTTPProbe{Port: 8080, Path: "/", StatusCode: 200}, true},
		{"negative", -1, probe.Argv, nil, false},
		{"oversized", 11, probe.Argv, nil, false},
		{"raw-marked", 10, []string{"/bin/true"}, nil, false},
		{"direct-container-marked", 10, []string{"/engine", "exec", "container", "/bin/true"}, nil, false},
		{"wrong-service", 10, []string{"/fixture/launch", "_workflow-probe", "other", "fixture"}, nil, false},
		{"wrong-fingerprint", 10, []string{"/fixture/launch", "_workflow-probe", "web", "changed"}, nil, false},
		{"http-marked", 10, nil, &HTTPProbe{Port: 8080, Path: "/", StatusCode: 200}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			probeCommand := Command{Argv: tc.argv, Directory: root}
			if tc.http != nil {
				probeCommand = Command{}
			}
			p.Services["web"] = Service{Command: command, Restart: "no", Shutdown: 1, Readiness: &Probe{Command: probeCommand, HTTPGet: tc.http, Period: 1, Timeout: 2, Failures: 10, Entry: tc.entry}}
			if err := validate(p); (err == nil) != tc.valid {
				t.Fatalf("entry allowance validity: %v", err)
			}
		})
	}
}

func TestInternalLifecycleCommandsRequireTheirDeclaredFingerprintScope(t *testing.T) {
	root := physicalTempDir(t)
	launch := []string{"/fixture/launch", "_workflow-service", "web", "launch-inputs"}
	probe := []string{"/fixture/launch", "_workflow-probe", "web", "lifecycle-inputs"}
	shutdown := []string{"/fixture/launch", "_workflow-shutdown", "web", "lifecycle-inputs"}
	for _, tc := range []struct {
		name     string
		launch   []string
		probe    []string
		shutdown []string
		valid    bool
	}{
		{"host", launch, probe, shutdown, true},
		{"container-nix", launch, append([]string{"/engine", "exec", "container"}, probe...), append([]string{"/engine", "exec", "container"}, shutdown...), true},
		{"probe-uses-launch-scope", launch, []string{"/fixture/launch", "_workflow-probe", "web", "launch-inputs"}, shutdown, false},
		{"probe-uses-other-scope", launch, []string{"/fixture/launch", "_workflow-probe", "web", "changed"}, shutdown, false},
		{"probe-uses-other-service", launch, []string{"/fixture/launch", "_workflow-probe", "other", "lifecycle-inputs"}, shutdown, false},
		{"empty-launch-scope", []string{"/fixture/launch", "_workflow-service", "web", ""}, probe, shutdown, false},
		{"raw-launch", []string{"/bin/true"}, probe, shutdown, false},
		{"shutdown-uses-launch-scope", launch, probe, []string{"/fixture/launch", "_workflow-shutdown", "web", "launch-inputs"}, false},
		{"shutdown-uses-other-service", launch, probe, []string{"/fixture/launch", "_workflow-shutdown", "other", "lifecycle-inputs"}, false},
		{"shutdown-uses-other-action", launch, probe, []string{"/fixture/launch", "_workflow-probe", "web", "lifecycle-inputs"}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			command := Command{Argv: tc.launch, Directory: root}
			stop := Command{Argv: tc.shutdown, Directory: root}
			p := Plan{Schema: 1, Root: root, State: physicalTempDir(t), Backend: "/bin/true", Fingerprint: "fixture-engine-identity", Requested: []string{"web"}, Task: command,
				Services: map[string]Service{"web": {Command: command, Restart: "no", Shutdown: 1, LifecycleFingerprint: "lifecycle-inputs", ShutdownCommand: &stop,
					Readiness: &Probe{Command: Command{Argv: tc.probe, Directory: root}, Period: 1, Timeout: 2, Failures: 10, Entry: 10}}}}
			if err := validate(p); (err == nil) != tc.valid {
				t.Fatalf("lifecycle scope validity: %v", err)
			}
			if !reflect.DeepEqual(p.Services["web"].Command.Argv, tc.launch) {
				t.Fatal("validation changed the immutable launch scope")
			}
		})
	}
}

func TestWatchMarkerProbeDoesNotEnterTransientBuildContainer(t *testing.T) {
	owner := &Container{Engine: "/missing/docker", Name: "transient-build", Token: strings.Repeat("a", 32)}
	p := Plan{Root: "/fixture", State: "/fixture/state", Watcher: "/fixture/watcher", Services: map[string]Service{
		"app": {Watch: &Watch{Build: Command{Argv: []string{"/bin/true"}}, Paths: []string{"/fixture"}, Debounce: 100, Startup: 5, Container: owner}},
	}}
	if e := expandWatches(&p, "/fixture/control"); e != nil {
		t.Fatal(e)
	}
	s := p.Services["app"+watchSuffix]
	command, e := serviceProbe("app"+watchSuffix, s)
	if e != nil || !reflect.DeepEqual(command.Argv, []string{"/fixture/control", "built", p.State, "app"}) {
		t.Fatal("host marker probe changed", command, e)
	}
	if s.Container != owner {
		t.Fatal("lost transient build cleanup ownership")
	}
	if _, e = serviceProbe("ordinary", s); e == nil {
		t.Fatal("ordinary container service accepted a host probe")
	}
	if e = expandWatches(&p, "/fixture/control"); e == nil {
		t.Fatal("accepted project declaration with reserved watch suffix")
	}
}

func TestContainerProbeBindsOwnedRunningIdentity(t *testing.T) {
	root := physicalTempDir(t)
	engine := filepath.Join(root, "engine")
	owner := &Container{Engine: engine, Name: "chainman-fixture", Token: strings.Repeat("a", 32)}
	id := strings.Repeat("b", 64)
	original := Command{Argv: []string{engine, "exec", owner.Name, "probe", owner.Name, "$(literal) two words"}}
	for _, kind := range []string{"owned", "replacement", "stopped", "missing", "outage"} {
		t.Run(kind, func(t *testing.T) {
			token := owner.Token
			if kind == "replacement" {
				token = "another-owner"
			}
			data, _ := json.Marshal([]any{map[string]any{"Id": id, "State": map[string]bool{"Running": kind != "stopped"}, "Config": map[string]any{"Labels": map[string]string{"dev.chainman.owner": token}}}})
			body := "#!/bin/sh\n[ \"$1 $2\" = 'container inspect' ] || exit 9\nprintf '%s\\n' " + quote(string(data)) + "\n"
			if kind == "missing" {
				body = "#!/bin/sh\n[ \"$1 $2\" = 'container ls' ]\n"
			} else if kind == "outage" {
				body = "#!/bin/sh\nexit 1\n"
			}
			if e := os.WriteFile(engine, []byte(body), 0700); e != nil {
				t.Fatal(e)
			}
			command, e := containerProbe(owner, original)
			if kind != "owned" {
				if e == nil {
					t.Fatal("accepted uncertain or changed probe ownership")
				}
				return
			}
			expected := []string{engine, "exec", id, "probe", owner.Name, "$(literal) two words"}
			if e != nil || !reflect.DeepEqual(command.Argv, expected) || original.Argv[2] != owner.Name {
				t.Fatalf("probe did not bind immutable identity and retain literal args: %+v %v", command, e)
			}
		})
	}
	for _, argv := range [][]string{{engine, "exec", "different", "probe"}, {engine, "run", owner.Name, "probe"}, {"other", "exec", owner.Name, "probe"}, {engine, "exec", owner.Name}} {
		if _, e := containerProbe(owner, Command{Argv: argv}); e == nil {
			t.Fatal("accepted probe without exact generated prefix")
		}
	}
	host := Command{Argv: []string{"/fixture/host-probe", "literal"}}
	if command, e := containerProbe(nil, host); e != nil || !reflect.DeepEqual(command, host) {
		t.Fatal("host probe changed", command, e)
	}
}
