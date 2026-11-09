package main

import (
	"bufio"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestFinishGroupConfirmsForcedKernelExit(t *testing.T) {
	const fixture = "CHAINMAN_TEST_FINISH_GROUP_MARKER"
	if marker := os.Getenv(fixture); marker != "" {
		if syscall.Getpgrp() != os.Getpid() {
			t.Fatal("fixture is not its process-group anchor")
		}
		cmd := exec.Command("/bin/sh", "-c", `trap '' TERM; printf 'ready\n'; exec sleep 60`)
		output, err := cmd.StdoutPipe()
		if err != nil {
			t.Fatal(err)
		}
		defer output.Close()
		if err = cmd.Start(); err != nil {
			t.Fatal(err)
		}
		defer func() {
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
		}()
		ready, err := bufio.NewReader(output).ReadString('\n')
		if err != nil || ready != "ready\n" {
			t.Fatalf("TERM-resistant child readiness: %q, %v", ready, err)
		}
		child, err := identify(cmd.Process.Pid)
		if err != nil {
			t.Fatal(err)
		}
		if err = atomic(marker, child); err != nil {
			t.Fatal(err)
		}
		started := time.Now()
		if err = finishGroup(os.Getpid(), started.Add(100*time.Millisecond)); err != nil {
			t.Fatal(err)
		}
		// Observe before cmd.Wait: waiting for the child here would hide the gap.
		if child.alive() {
			t.Fatal("cleanup returned while its forced child was still live")
		}
		if elapsed := time.Since(started); elapsed > 2*time.Second {
			t.Fatalf("bounded graceful and forced cleanup took %s", elapsed)
		}
		return
	}

	t.Setenv("GORACE", strings.TrimSpace(os.Getenv("GORACE")+" atexit_sleep_ms=0"))
	root := physicalTempDir(t)
	marker := filepath.Join(root, "child.json")
	logPath := filepath.Join(root, "fixture.log")
	log, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	cmd := exec.Command(os.Args[0], "-test.run=^TestFinishGroupConfirmsForcedKernelExit$")
	cmd.Env = append(os.Environ(), fixture+"="+marker)
	cmd.Stdout, cmd.Stderr = log, log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	owner, identityErr := identify(cmd.Process.Pid)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	waited := false
	t.Cleanup(func() {
		var child Identity
		if readJSON(marker, &child) == nil && child.alive() {
			_ = syscall.Kill(child.PID, syscall.SIGKILL)
		}
		if identityErr == nil && owner.alive() {
			_ = syscall.Kill(-owner.PID, syscall.SIGKILL)
		}
		if !waited {
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Error("cleanup fixture did not exit after forced teardown")
			}
		}
	})
	if identityErr != nil {
		t.Fatal(identityErr)
	}
	select {
	case err = <-done:
		waited = true
	case <-time.After(6 * time.Second):
		t.Fatal("kernel cleanup fixture exceeded its outer deadline")
	}
	if err != nil {
		output, _ := os.ReadFile(logPath)
		t.Fatalf("kernel cleanup fixture: %v\n%s", err, output)
	}
}
