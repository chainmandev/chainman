package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
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

func TestContainerBoundaryRemovesHostDescriptorAdvertisements(t *testing.T) {
	names := []string{"TOOLCHAIN_LOCK_FD", "TOOLCHAIN_GATE_FD", "TOOLCHAIN_COMPAT_FD", "TOOLCHAIN_ANCESTOR_FDS", "TOOLCHAIN_OPERATION_ID", "CHAINMAN_COMPILER_OWNER", "CHAINMAN_SERVICE_LEASE_FDS", "CHAINMAN_SERVICE_CONTEXT_FD", "CHAINMAN_UPDATE_LEASE_FD", "CHAINMAN_STORAGE_FDS", "CHAINMAN_OPERATION_FIXTURE"}
	env := map[string]string{
		"CHAINMAN_CONTAINER_OWNER": "literal-owner", "CHAINMAN_CONTAINER_NAME": "literal-name",
		"CHAINMAN_SETUP_CHANNEL": "literal-consent", "CHAINMAN_UPDATE_TRANSACTION": "literal-transaction",
		"CHAINMAN_DEV_CHANNEL": "literal-display", "LITERAL": "spaces $()",
	}
	for _, name := range names {
		t.Setenv(name, "stale-inherited")
		env[name] = "stale-override"
	}
	cmd, err := child(Command{Argv: []string{"/bin/sh", "-c", ":"}, Environment: env})
	if err != nil {
		t.Fatal(err)
	}
	if err = isolateContainerDescriptors(cmd); err != nil {
		t.Fatal(err)
	}
	if len(cmd.ExtraFiles) != 0 {
		t.Fatal("container bootstrap received host descriptors")
	}
	for _, name := range names {
		for _, value := range cmd.Environ() {
			if strings.HasPrefix(value, name+"=") {
				t.Fatalf("stale host descriptor context: %s", value)
			}
		}
		delete(env, name)
	}
	for name, value := range env {
		found := false
		for _, entry := range cmd.Environ() {
			found = found || entry == name+"="+value
		}
		if !found {
			t.Fatalf("container recovery/consent environment lost: %s", name)
		}
	}
}
