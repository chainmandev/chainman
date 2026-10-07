package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"syscall"
	"testing"
	"time"
)

func TestStoppedCancellationSignalHelper(t *testing.T) {
	if os.Getenv("CHAINMAN_TEST_STOPPED_SIGNAL_HELPER") != "1" {
		return
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM)
	fmt.Fprintln(os.Stdout, "ready")
	<-signals
	os.Exit(23)
}

func TestStoppedOwnerReceivesTaskCancellation(t *testing.T) {
	for iteration := range 24 {
		t.Run(strconv.Itoa(iteration), func(t *testing.T) {
			// The subprocess acknowledges only signal.Notify's actual delivery.
			// Confirm its native stop before exercising the production route.
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestStoppedCancellationSignalHelper$")
			cmd.Env = append(os.Environ(), "CHAINMAN_TEST_STOPPED_SIGNAL_HELPER=1")
			output, err := cmd.StdoutPipe()
			if err != nil {
				t.Fatal(err)
			}
			defer output.Close()
			if err = cmd.Start(); err != nil {
				t.Fatal(err)
			}
			defer func() {
				if cmd.ProcessState == nil {
					_ = cmd.Process.Kill()
					_ = cmd.Wait()
				}
			}()
			ready, err := bufio.NewReader(output).ReadString('\n')
			if err != nil || ready != "ready\n" {
				t.Fatalf("signal receiver readiness: %q, %v", ready, err)
			}
			if err = cmd.Process.Signal(syscall.SIGSTOP); err != nil {
				t.Fatal(err)
			}
			for {
				var status syscall.WaitStatus
				_, err = syscall.Wait4(cmd.Process.Pid, &status, syscall.WUNTRACED, nil)
				if errors.Is(err, syscall.EINTR) {
					continue
				}
				if err != nil || !taskStopped(status) {
					t.Fatalf("signal receiver stop: status=%#x, error=%v", status, err)
				}
				break
			}
			interruptTask(cmd, syscall.SIGTERM)
			if err = cmd.Wait(); exitCode(err) != 23 {
				t.Fatalf("stopped owner did not acknowledge cancellation: %v", err)
			}
		})
	}
}
