package main

import (
	"bytes"
	"context"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
	"time"
)

func TestMain(m *testing.M) {
	if len(os.Args) == 4 && os.Args[1] == "pending-signal-fixture" {
		number, err := strconv.Atoi(os.Args[2])
		if err != nil {
			os.Exit(2)
		}
		os.Exit(pendingSignalFixture(syscall.Signal(number), os.Args[3]))
	}
	if len(os.Args) > 1 && (os.Args[1] == "update-cache" || os.Args[1] == "hook-exec" || os.Args[1] == "build") {
		os.Exit(mainAction(os.Args[1:]))
	}
	// Exercise the actual admission entrypoint in a subprocess of the test
	// binary. No production delay, environment switch, or signal stub is needed.
	if len(os.Args) > 1 && os.Args[1] == "admitted" {
		os.Exit(admittedCommand(os.Args[2:]))
	}
	os.Exit(m.Run())
}

func TestQueuedCancellationDoesNotSpawn(t *testing.T) {
	signals := make(chan os.Signal, 1)
	signals <- syscall.SIGTERM
	cmd := exec.Command("/bin/sh", "-c", "exit 99")
	if err := startAdmitted(cmd, signals); exitCode(err) != 143 || cmd.Process != nil {
		t.Fatalf("cancelled command started: process=%v error=%v", cmd.Process, err)
	}
}

func gatedFixture(t *testing.T, script string, arguments ...string) (*exec.Cmd, *os.File) {
	t.Helper()
	read, permit, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	args := append([]string{"admitted", "3", "/bin/sh", "sh", "-c", script, "fixture"}, arguments...)
	cmd := exec.Command(os.Args[0], args...)
	cmd.ExtraFiles = []*os.File{read}
	t.Cleanup(func() {
		read.Close()
		permit.Close()
		if cmd.Process != nil && cmd.ProcessState == nil {
			cmd.Process.Kill()
			cmd.Wait()
		}
	})
	return cmd, permit
}

func pendingSignalFixture(number syscall.Signal, marker string) int {
	read, permit, err := os.Pipe()
	if err != nil {
		return 1
	}
	defer read.Close()
	defer permit.Close()
	cmd := exec.Command(os.Args[0], "admitted", "3", "/bin/sh", "sh", "-c", `printf ran > "$1"`, "fixture", marker)
	cmd.ExtraFiles = []*os.File{read}
	// Keep the captured output pipe alive until the gated child also exits.
	// The parent must not inspect the marker while a child can still create it.
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	signals := make(chan os.Signal, 8)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer signal.Stop(signals)
	if err := cmd.Start(); err != nil {
		return 1
	}
	read.Close()
	// Default termination during admission's notification reset is also a
	// valid fail-closed outcome: it closes the permit without granting it.
	if err := syscall.Kill(os.Getpid(), number); err != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return 1
	}
	// Kill returning does not establish delivery to Go's asynchronous signal
	// channel. Queue the actual notification before exercising pending admission
	// so this fixture tests received cancellation, not scheduler timing.
	received := <-signals
	if received != number {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return 1
	}
	signals <- received
	if err := admitStarted(cmd, permit, signals); err != nil {
		return exitCode(err)
	}
	return exitCode(cmd.Wait())
}

func TestPendingSignalVetoesSpawnedHelper(t *testing.T) {
	for _, number := range []syscall.Signal{syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP} {
		t.Run(number.String(), func(t *testing.T) {
			marker := filepath.Join(physicalTempDir(t), "unexpected")
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "pending-signal-fixture", strconv.Itoa(int(number)), marker)
			read, write, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			defer read.Close()
			defer write.Close()
			cmd.Stdout, cmd.Stderr = write, write
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			write.Close()
			err = cmd.Wait()
			if e := read.SetReadDeadline(time.Now().Add(2 * time.Second)); e != nil {
				t.Fatal(e)
			}
			output, drained := io.ReadAll(read)
			if ctx.Err() != nil || drained != nil || exitCode(err) != 128+int(number) {
				t.Fatalf("expected cancellation and child exit, got %v (context %v, output %v): %s", err, ctx.Err(), drained, output)
			}
			if _, err := os.Stat(marker); !os.IsNotExist(err) {
				t.Fatalf("unadmitted workload ran: %v", err)
			}
		})
	}
}

func TestAdmissionRequiresPermitAndExecPreservesPID(t *testing.T) {
	for _, test := range []struct {
		name  string
		token []byte
	}{
		{"closed", nil}, {"invalid", []byte{0}}, {"granted", []byte{1}},
	} {
		t.Run(test.name, func(t *testing.T) {
			token := test.token
			cmd, permit := gatedFixture(t, `printf '%s' "$$"`)
			var output bytes.Buffer
			cmd.Stdout = &output
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			if len(token) > 0 {
				if _, err := permit.Write(token); err != nil {
					t.Fatal(err)
				}
			}
			permit.Close()
			err := cmd.Wait()
			if len(token) > 0 && token[0] == 1 {
				if err != nil || output.String() != strconv.Itoa(cmd.Process.Pid) {
					t.Fatalf("exec changed PID: %q, %v", output.String(), err)
				}
			} else if exitCode(err) != 125 || output.Len() != 0 {
				t.Fatalf("missing/invalid permission ran workload: %q, %v", output.String(), err)
			}
		})
	}
}

func TestGatedHelperRetainsDefaultInterruptHandling(t *testing.T) {
	signals := make(chan os.Signal, 8)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer signal.Stop(signals)
	cmd, _ := gatedFixture(t, `printf unexpected`)
	var output bytes.Buffer
	cmd.Stdout = &output
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	if err := cmd.Process.Signal(syscall.SIGINT); err != nil {
		t.Fatal(err)
	}
	if err := cmd.Wait(); exitCode(err) != 130 || output.Len() != 0 {
		t.Fatalf("unadmitted helper ignored interrupt or ran workload: %q, %v", output.String(), err)
	}
}
