package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestServiceContextDescriptorRemapping(t *testing.T) {
	for _, name := range []string{"TOOLCHAIN_LOCK_FD", "TOOLCHAIN_GATE_FD", "TOOLCHAIN_COMPAT_FD", "TOOLCHAIN_OPERATION_ID", "TOOLCHAIN_ANCESTOR_FDS", "CHAINMAN_SERVICE_LEASE_FDS", "CHAINMAN_SERVICE_CONTEXT_FD"} {
		t.Setenv(name, "")
	}
	file, err := os.Create(filepath.Join(t.TempDir(), "context"))
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err = file.WriteString("independent fixture context\n"); err != nil {
		t.Fatal(err)
	}
	if _, err = file.Seek(0, 0); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CHAINMAN_SERVICE_CONTEXT_FD", fmt.Sprint(file.Fd()))
	t.Setenv("TOOLCHAIN_ANCESTOR_FDS", fmt.Sprintf("[%d]", file.Fd()))
	cmd := exec.Command("sh", "-c", `eval "cat <&$CHAINMAN_SERVICE_CONTEXT_FD"`)
	cmd.Env = os.Environ()
	if err = forwardLeases(cmd); err != nil {
		t.Fatal(err)
	}
	defer closeForwarded(cmd)
	output, err := cmd.CombinedOutput()
	if err != nil || string(output) != "independent fixture context\n" {
		t.Fatalf("context was not forwarded: %s, %v", output, err)
	}
}
