package main

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Exercises the actual ten-second read-only deadline, preserving its context
// instead of exposing the query subprocess SIGKILL as the task outcome.
func TestQueryDeadlineContextAndExitClassification(t *testing.T) {
	dir := t.TempDir()
	backend := filepath.Join(dir, "backend")
	receipt := filepath.Join(dir, "query.txt")
	script := "#!/bin/sh\nprintf 'pid=%s ppid=%s argv=%s\\n' \"$$\" \"$PPID\" \"$*\" > '" + receipt + "'\nexec sleep 60\n"
	if err := os.WriteFile(backend, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	plan := Plan{Root: dir, State: dir, Backend: backend, Services: map[string]Service{"healthy": {}}, Requested: []string{"healthy"}}
	if err := atomic(filepath.Join(dir, "plan.json"), plan); err != nil {
		t.Fatal(err)
	}
	if _, err := socketPath(plan); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(dir, "task-cancelled")
	task := exec.Command("sh", "-c", "trap 'echo cancelled > \"$1\"; exit 0' TERM; while :; do sleep 0.1; done", "fixture", marker)
	if err := task.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = task.Process.Kill() }()
	done := make(chan error, 1)
	go func() { done <- task.Wait() }()
	started := time.Now()
	var failure error
	select {
	case failure = <-func() <-chan error {
		result := make(chan error, 1)
		go func() { result <- serviceOutcome(plan, nil) }()
		return result
	}():
	case <-time.After(15 * time.Second):
		t.Fatal("monitor did not return its bounded query failure")
	}
	if !errors.Is(failure, context.DeadlineExceeded) || !strings.Contains(failure.Error(), "service controller lost") {
		t.Fatalf("missing contextual monitor error: %v", failure)
	}
	// Mirrors the actual mainAction monitor-failure branch: retain the original
	// query failure while cancelling the still-running task.
	if err := cancelTask(task, done, syscall.SIGTERM, 1); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("healthy task was not cancelled: %v", err)
	}
	if code := exitCode(failure); code != 1 {
		t.Fatalf("expected contextual deadline exit1, got%d (%v)", code, failure)
	}
	detail, err := os.ReadFile(receipt)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("query %s; duration=%s; contextual error=%v; public exit=1; healthy task cancelled", strings.TrimSpace(string(detail)), time.Since(started), failure)
}
