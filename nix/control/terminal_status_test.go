package main

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
	"testing"
	"time"
)

func TestTaskStoppedReports(t *testing.T) {
	for _, test := range []struct {
		name   string
		status syscall.WaitStatus
		stop   bool
	}{
		{"Darwin SIGSTOP", 0x147f, true},
		{"Linux SIGSTOP", 0x137f, true},
		{"terminal stop", syscall.WaitStatus(uint32(syscall.SIGTSTP)<<8 | 0x7f), true},
		{"terminal input stop", syscall.WaitStatus(uint32(syscall.SIGTTIN)<<8 | 0x7f), true},
		{"terminal output stop", syscall.WaitStatus(uint32(syscall.SIGTTOU)<<8 | 0x7f), true},
		{"exit zero", 0, false},
		{"exit seven", 7 << 8, false},
		{"terminated", syscall.WaitStatus(syscall.SIGTERM), false},
		{"core dump", syscall.WaitStatus(syscall.SIGQUIT) | 0x80, false},
		{"Linux continued", 0xffff, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := taskStopped(test.status); got != test.stop {
				t.Fatalf("status %#x: stopped=%t, want %t", test.status, got, test.stop)
			}
		})
	}
}

func TestTaskStoppedRecognizesNativeSIGSTOP(t *testing.T) {
	cmd := exec.Command("/bin/sh", "-c", `kill -STOP "$$"; exit 7`)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		var status syscall.WaitStatus
		pid, err := syscall.Wait4(cmd.Process.Pid, &status, syscall.WNOHANG|syscall.WUNTRACED, nil)
		if errors.Is(err, syscall.EINTR) {
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		if pid != 0 {
			t.Logf("native SIGSTOP status=%#x, syscall.Stopped=%t", status, status.Stopped())
			if !taskStopped(status) {
				t.Fatalf("native SIGSTOP was not recognized: %#x", status)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("native child did not report its stop")
}

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
