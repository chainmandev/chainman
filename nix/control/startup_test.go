package main

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestStartupStopTickets(t *testing.T) {
	state := t.TempDir()
	path := filepath.Join(state, "startup-stop.json")
	notice := stopNotice{Token: token()}
	if err := atomic(path, notice); err != nil {
		t.Fatal(err)
	}
	guard := &startupGuard{stops: map[string]string{}}
	if err := guard.observe(state); err != nil {
		t.Fatalf("completed stop blocked a later start: %v", err)
	}
	notice.Token = token()
	if err := atomic(path, notice); err != nil {
		t.Fatal(err)
	}
	if !errors.Is(guard.check(), servicesStopped) {
		t.Fatal("startup missed a stop that completed between observations")
	}
	notice.Pending = true
	if err := atomic(path, notice); err != nil {
		t.Fatal(err)
	}
	if !errors.Is(guard.observe(state), servicesStopped) {
		t.Fatal("new start overtook a pending or interrupted stop")
	}
}

func TestStartupGateWaitCanBeInterruptedWithoutReleasingItsOwner(t *testing.T) {
	path := filepath.Join(t.TempDir(), "gate")
	owner, err := locked(path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer owner.Close()
	signals := make(chan os.Signal, 1)
	guard := &startupGuard{signals: signals, stops: map[string]string{}}
	done := make(chan error, 1)
	go func() {
		file, err := guard.lock(path)
		if file != nil {
			file.Close()
		}
		done <- err
	}()
	signals <- syscall.SIGINT
	select {
	case err := <-done:
		if exitCode(err) != 130 {
			t.Fatalf("expected interruption, got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("scope gate wait ignored interruption")
	}
	if file, err := locked(path, true); !errors.Is(err, syscall.EWOULDBLOCK) {
		if file != nil {
			file.Close()
		}
		t.Fatalf("interrupted waiter released another client's gate: %v", err)
	}
}
