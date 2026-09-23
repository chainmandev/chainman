package main

import (
	"os"
	"testing"
	"time"
)

func TestHookWaitsForCallbacksAndRejectsLateAdmission(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "callbacks-")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	t.Setenv("CHAINMAN_HOOK_CALLBACK_BARRIER", f.Name())
	lease, err := hookCallbackLease()
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	done := make(chan error, 1)
	go func() { done <- hookDrainCallbacks(f) }()
	select {
	case err := <-done:
		t.Fatalf("returned with a live callback: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	lease.Close()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("did not observe callback completion")
	}
	f.Close()
	if late, err := hookCallbackLease(); err == nil {
		late.Close()
		t.Fatal("admitted a callback after completion")
	}
}
