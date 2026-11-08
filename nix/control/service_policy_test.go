package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestServiceStartupPolicyKeepsDefaultFatalAndBoundsOptionalReadiness(t *testing.T) {
	for _, policy := range []string{"", "stop", "continue"} {
		for _, status := range []string{"Error", "Completed", "Skipped", "Running"} {
			t.Run(policy+"/"+status, func(t *testing.T) {
				p := Plan{State: t.TempDir(), Services: map[string]Service{
					"core": {}, "reporting": {FailurePolicy: policy, Readiness: &Probe{Period: 1, Timeout: 1, Failures: 2}},
				}}
				ps := []Process{{Name: "core", Running: true, Status: "Running"}, {Name: "reporting", Status: status, Running: status == "Running", Ready: "NotReady"}}
				pending, degraded, err := startupServiceSample(p, []string{"core", "reporting"}, ps, 0)
				if status == "Running" {
					if err != nil || !pending || len(degraded) != 0 {
						t.Fatalf("premature readiness result: %v %v %v", pending, degraded, err)
					}
				} else if policy == "continue" {
					if err != nil || pending || !reflect.DeepEqual(degraded, []string{"reporting"}) {
						t.Fatalf("optional startup cancelled core: %v %v %v", pending, degraded, err)
					}
				} else if err == nil || !strings.Contains(err.Error(), "exited before readiness") {
					t.Fatalf("default/fatal startup failure weakened: %v", err)
				}
				if status == "Running" && policy == "continue" {
					bound := serviceStartupBound(p.Services["reporting"])
					pending, _, err = startupServiceSample(p, []string{"core", "reporting"}, ps, bound-time.Nanosecond)
					if !pending || err != nil {
						t.Fatalf("readiness bound shortened: %v %v", pending, err)
					}
					pending, degraded, err = startupServiceSample(p, []string{"core", "reporting"}, ps, bound)
					if pending || err != nil || !reflect.DeepEqual(degraded, []string{"reporting"}) {
						t.Fatalf("unavailable readiness did not degrade: %v %v %v", pending, degraded, err)
					}
					ps[1].Ready = "Ready"
					pending, degraded, err = startupServiceSample(p, []string{"core", "reporting"}, ps, bound)
					if pending || err != nil || len(degraded) != 0 {
						t.Fatalf("recovery was hidden: %v %v %v", pending, degraded, err)
					}
				}
			})
		}
	}
}

func TestOptionalServiceDoesNotMaskFatalDependencyOrLostBackendRow(t *testing.T) {
	p := Plan{State: t.TempDir(), Services: map[string]Service{"core": {}, "reporting": {FailurePolicy: "continue"}}}
	for _, ps := range [][]Process{
		{{Name: "reporting", Status: "Error"}},
		{{Name: "core", Status: "Skipped"}, {Name: "reporting", Status: "Error"}},
	} {
		_, _, err := startupServiceSample(p, []string{"core", "reporting"}, ps, time.Hour)
		if err == nil {
			t.Fatal("optional policy masked a core/controller failure")
		}
	}
}

func TestOptionalRuntimeFailureAndRecoveryRemainObservableWithoutCancellation(t *testing.T) {
	dir := t.TempDir()
	backend := filepath.Join(dir, "backend")
	p := Plan{Root: dir, State: dir, Backend: backend, Services: map[string]Service{"core": {}, "reporting": {FailurePolicy: "continue", Readiness: &Probe{}}}, Requested: []string{"core", "reporting"}}
	if _, err := socketPath(p); err != nil {
		t.Fatal(err)
	}
	for _, status := range []string{"Error", "Completed", "Skipped", "Running"} {
		body := `[{"name":"core","status":"Running","is_running":true},{"name":"reporting","status":"` + status + `","is_running":` + map[bool]string{true: "true", false: "false"}[status == "Running"] + `,"is_ready":"Ready"}]`
		if err := os.WriteFile(backend, []byte("#!/bin/sh\nprintf '%s' "+quote(body)+"\n"), 0700); err != nil {
			t.Fatal(err)
		}
		if err := serviceOutcome(p, nil); err != nil {
			t.Fatalf("optional %s cancelled core: %v", status, err)
		}
		ps, err := states(p)
		if err != nil {
			t.Fatal(err)
		}
		degraded := degradedServices(p, ps, map[string]bool{"core": true, "reporting": true})
		if (status == "Running") != (len(degraded) == 0) {
			t.Fatalf("incorrect degradation/recovery: %s %v", status, degraded)
		}
		if status != "Running" {
			service := p.Services["reporting"]
			service.FailurePolicy = "stop"
			p.Services["reporting"] = service
			if err := serviceOutcome(p, nil); err == nil {
				t.Fatal("fatal runtime policy ignored")
			}
			service.FailurePolicy = "continue"
			p.Services["reporting"] = service
		}
	}
	if err := os.WriteFile(backend, []byte("#!/bin/sh\nexit 7\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := serviceOutcome(p, nil); err == nil {
		t.Fatal("optional policy masked lost controller")
	}
}

func TestServiceFailurePolicyRejectsUnknownValuesAndPreservesRestart(t *testing.T) {
	root := physicalTempDir(t)
	command := Command{Argv: []string{"/bin/true"}, Directory: root}
	p := Plan{Schema: 1, Root: root, State: physicalTempDir(t), Backend: "/bin/true", Fingerprint: "fixture", Services: map[string]Service{}, Requested: []string{"reporting"}}
	for _, policy := range []string{"", "stop", "continue", "ignore", "CONTINUE"} {
		p.Services["reporting"] = Service{Command: command, Shutdown: 1, Restart: "always", FailurePolicy: policy}
		err := validate(p)
		if (err == nil) != (policy == "" || policy == "stop" || policy == "continue") {
			t.Fatalf("policy %q: %v", policy, err)
		}
	}
}
