package main

import (
	"os"
	"os/exec"
	"syscall"
	"testing"
	"time"
)

func TestTaskWaitDoesNotDependOnChildNotifications(t *testing.T) {
	// Project a stopped child whose owner receives no child-change wakeup.
	// The task status waiter remains the only process-status consumer.
	cmd := exec.Command("/bin/sh", "-c", `kill -STOP "$$"; exit 7`)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	changed := make(chan os.Signal, 1)
	done := make(chan int, 1)
	completed := false
	defer func() {
		if completed {
			return
		}
		_ = cmd.Process.Kill()
		// Unblock even the original notification-only waiter before failing.
		changed <- syscall.SIGCHLD
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Error("fixture status waiter did not finish during cleanup")
		}
	}()
	go func() {
		done <- waitTask(cmd, &taskTTY{fd: -1}, make(chan os.Signal), changed, make(chan os.Signal))
	}()
	// Resume only this disposable child. No real signal notification is
	// connected to changed, including the subsequent exit notification.
	time.Sleep(200 * time.Millisecond)
	if err := cmd.Process.Signal(syscall.SIGCONT); err != nil {
		t.Fatal(err)
	}
	select {
	case code := <-done:
		completed = true
		if code != 7 {
			t.Fatalf("actual child status lost: got %d, want 7", code)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("task owner stalled without child-change notification")
	}
}
