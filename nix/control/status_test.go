package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestStatusDuringMutationDoesNotWaitOrReap(t *testing.T) {
	state := physicalTempDir(t)
	os.Chmod(state, 0700)
	if err := private(state); err != nil {
		t.Fatal(err)
	}
	gate, err := locked(filepath.Join(state, "gate"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	p := Plan{State: state, Services: map[string]Service{}}
	if err := atomic(filepath.Join(state, "plan.json"), p); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(state, "abandoned.lease")
	if err := atomic(path, Lease{Services: []string{"abandoned"}}); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(path)
	started := time.Now()
	ctx, cancel := context.WithTimeout(context.Background(), statusTimeout)
	defer cancel()
	value, _, err := scopeStatus(ctx, state, false)
	if err != nil {
		t.Fatal(err)
	}
	if time.Since(started) > time.Second || value["busy"] != true || value["complete"] != false || value["recovery_required"] != nil {
		t.Fatal(value)
	}
	after, err := os.ReadFile(path)
	if err != nil || string(after) != string(before) {
		t.Fatal("inspection reaped or changed a lease", err)
	}
	gate.Close()
	value, _, err = scopeStatus(ctx, state, false)
	if err != nil || value["busy"] != false || value["complete"] != true {
		t.Fatal(value, err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("unlocked inspection reaped lease", err)
	}
}

func TestStatusBudgetSharedAcrossScopesAndUnknownOnTimeout(t *testing.T) {
	base := physicalTempDir(t)
	backend := filepath.Join(base, "backend")
	// exec prevents an intentionally stalled fixture child outliving its owner.
	if err := os.WriteFile(backend, []byte("#!/bin/sh\nexec sleep 30\n"), 0700); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	started := time.Now()
	for _, name := range []string{"one", "two"} {
		state := filepath.Join(base, name)
		if err := private(state); err != nil {
			t.Fatal(err)
		}
		gate, err := locked(filepath.Join(state, "gate"), false)
		if err != nil {
			t.Fatal(err)
		}
		defer gate.Close()
		p := Plan{State: state, Backend: backend, Services: map[string]Service{"service": {}}}
		if err := atomic(filepath.Join(state, "plan.json"), p); err != nil {
			t.Fatal(err)
		}
		value, _, err := scopeStatus(ctx, state, false)
		if err != nil || value["complete"] != false || value["services"] != nil || value["recovery_required"] != nil {
			t.Fatal(value, err)
		}
	}
	if time.Since(started) > time.Second {
		t.Fatal("status did not share one deadline")
	}
}

func TestStatusOfUnusedScopeDoesNotCreateState(t *testing.T) {
	state := filepath.Join(physicalTempDir(t), "unused")
	value, _, err := scopeStatus(context.Background(), state, false)
	if err != nil || value["running"] != false || value["complete"] != true {
		t.Fatal(value, err)
	}
	if _, err := os.Stat(state); !os.IsNotExist(err) {
		t.Fatal("inspection created state", err)
	}
}
