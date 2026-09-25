package main

import (
	"bytes"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
)

func TestMain(m *testing.M) {
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

func TestPendingSignalVetoesSpawnedHelper(t *testing.T) {
	for _, number := range []syscall.Signal{syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP} {
		t.Run(number.String(), func(t *testing.T) {
			marker := filepath.Join(physicalTempDir(t), "unexpected")
			cmd, permit := gatedFixture(t, `printf ran > "$1"`, marker)
			signals := make(chan os.Signal, 8)
			signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
			defer signal.Stop(signals)
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			// Leave delivery asynchronous: admission must drain runtime notices,
			// not rely on a test waiting until its channel already contains one.
			if err := syscall.Kill(os.Getpid(), number); err != nil {
				t.Fatal(err)
			}
			if err := admitStarted(cmd, permit, signals); exitCode(err) != 128+int(number) {
				t.Fatalf("expected cancellation, got %v", err)
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
