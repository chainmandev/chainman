package main

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestHTTPAdmissionPreservesFrozenAuthority(t *testing.T) {
	for _, c := range []struct {
		name                    string
		used, stale, unresolved bool
	}{
		{name: "unused-resolves"},
		{name: "active-stays-frozen", used: true},
		{name: "wrong-generation-refused", stale: true},
		{name: "unresolved-refused", unresolved: true},
	} {
		t.Run(c.name, func(t *testing.T) {
			dir := t.TempDir()
			original := Service{Generation: "live", Command: Command{Argv: []string{"original"}, Directory: dir}, Container: &Container{Name: "owned", Token: "original-owner"}, Readiness: &Probe{HTTPGet: &HTTPProbe{Port: 1234, Path: "/", StatusCode: 200, PendingEnvironment: true}, Period: 1, Timeout: 2, Failures: 3}}
			if c.used {
				original.Readiness.HTTPGet.PendingEnvironment = false
				original.Readiness.HTTPGet.Headers = map[string]string{"Authorization": "Bearer original"}
			}
			if c.stale {
				original.Generation = "old"
			}
			saved := Plan{State: dir, Generation: "live", Services: map[string]Service{"api": original}}
			incoming := Plan{Services: map[string]Service{"api": {Command: Command{Argv: []string{"replacement-must-not-run"}}, Readiness: &Probe{HTTPGet: &HTTPProbe{Port: 1234, Path: "/", StatusCode: 200, Headers: map[string]string{"Authorization": "Bearer fixture"}, PendingEnvironment: c.unresolved}, Period: 99, Timeout: 99, Failures: 99}}}}
			path := filepath.Join(dir, "api.command.json")
			if err := atomic(path, original); err != nil {
				t.Fatal(err)
			}
			before, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			err = admitHTTPProbes(&saved, incoming, []string{"api"}, map[string]bool{"api": c.used})
			shouldFail := c.stale || c.unresolved
			if (err != nil) != shouldFail {
				t.Fatalf("unexpected result: %v", err)
			}
			if c.used || shouldFail {
				after, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				if string(before) != string(after) || !reflect.DeepEqual(saved.Services["api"], original) {
					t.Fatal("refusal/reuse modified frozen state")
				}
				return
			}
			want := original
			want.Readiness = &Probe{HTTPGet: incoming.Services["api"].Readiness.HTTPGet, Period: 1, Timeout: 2, Failures: 3}
			var got Service
			if err := readJSON(path, &got); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) || !reflect.DeepEqual(saved.Services["api"], want) {
				t.Fatal("admission changed more than HTTP probe")
			}
		})
	}
}
