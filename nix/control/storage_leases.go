package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
)

const storagePool = ".chainman-storage-v1"

type storageReceipt struct {
	Schema  int     `json:"schema"`
	Kind    string  `json:"kind"`
	Touched float64 `json:"touched"`
	Epoch   string  `json:"epoch"`
}

// Backends can close descriptors before calling the native adapter. Reacquire
// an independent lease under admission and keep it in both owner and child.
func forwardStorageLeases(cmd *exec.Cmd) error {
	paths := []string{}
	for _, value := range cmd.Environ() {
		if raw, ok := strings.CutPrefix(value, "CHAINMAN_STORAGE_PATHS="); ok {
			if err := json.Unmarshal([]byte(raw), &paths); err != nil {
				return err
			}
		}
	}
	if len(paths) > 64 {
		return fmt.Errorf("too many storage leases")
	}
	start := len(cmd.ExtraFiles)
	fds := []int{}
	cleanup := func(err error) error {
		for _, file := range cmd.ExtraFiles[start:] {
			file.Close()
		}
		cmd.ExtraFiles = cmd.ExtraFiles[:start]
		return err
	}
	for _, path := range paths {
		if filepath.Dir(path) == path || filepath.Base(filepath.Dir(path)) != storagePool {
			return cleanup(fmt.Errorf("invalid storage pool"))
		}
		if err := existingPrivate(path); err != nil {
			return cleanup(err)
		}
		if err := existingPrivate(filepath.Dir(path)); err != nil {
			return cleanup(err)
		}
		gate, err := updateFile(filepath.Join(filepath.Dir(path), ".gate"), false)
		if err != nil {
			return cleanup(err)
		}
		if err = syscall.Flock(int(gate.Fd()), syscall.LOCK_EX); err != nil {
			gate.Close()
			return cleanup(err)
		}
		var receipt storageReceipt
		err = readJSON(filepath.Join(path, ".receipt.json"), &receipt)
		if err == nil && (receipt.Schema != 1 || receipt.Touched <= 0 || !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(receipt.Epoch) || (receipt.Kind != "downloads" && receipt.Kind != "runtime") || (receipt.Kind == "downloads" && filepath.Base(path) != "downloads") || (receipt.Kind == "runtime" && !regexp.MustCompile(`^[0-9a-f]{40}$`).MatchString(filepath.Base(path)))) {
			err = fmt.Errorf("unknown storage receipt")
		}
		var lease *os.File
		if err == nil {
			lease, err = updateFile(filepath.Join(path, ".lease"), false)
		}
		if err == nil {
			err = syscall.Flock(int(lease.Fd()), syscall.LOCK_SH|syscall.LOCK_NB)
		}
		gate.Close()
		if err != nil {
			if lease != nil {
				lease.Close()
			}
			return cleanup(err)
		}
		fds = append(fds, 3+len(cmd.ExtraFiles))
		cmd.ExtraFiles = append(cmd.ExtraFiles, lease)
	}
	env := cmd.Environ()
	cmd.Env = nil
	for _, value := range env {
		if !strings.HasPrefix(value, "CHAINMAN_STORAGE_FDS=") {
			cmd.Env = append(cmd.Env, value)
		}
	}
	encoded, _ := json.Marshal(fds)
	cmd.Env = append(cmd.Env, "CHAINMAN_STORAGE_FDS="+string(encoded))
	return nil
}
