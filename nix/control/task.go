package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

type TaskCommands struct {
	Commands []Command `json:"commands"`
	Timeout  int       `json:"timeout_seconds"`
	Shutdown int       `json:"shutdown_seconds"`
}

// Forward checked Chainman operation/setup leases into the identity anchor and
// its command sequence, remapping descriptor numbers allocated by os/exec.
func forwardLeases(cmd *exec.Cmd) error {
	ancestors := []int{}
	serviceLeases := []int{}
	if value := os.Getenv("CHAINMAN_SERVICE_LEASE_FDS"); value != "" {
		if e := json.Unmarshal([]byte(value), &serviceLeases); e != nil {
			return e
		}
	}
	if value := os.Getenv("TOOLCHAIN_ANCESTOR_FDS"); value != "" {
		if e := json.Unmarshal([]byte(value), &ancestors); e != nil {
			return e
		}
	}
	vars := map[string]int{}
	for _, name := range []string{"TOOLCHAIN_LOCK_FD", "TOOLCHAIN_GATE_FD", "TOOLCHAIN_COMPAT_FD"} {
		if value := os.Getenv(name); value != "" {
			fd, e := strconv.Atoi(value)
			if e != nil {
				return e
			}
			vars[name] = fd
		}
	}
	selected := map[int]bool{}
	for _, fd := range ancestors {
		selected[fd] = true
	}
	for _, fd := range serviceLeases {
		selected[fd] = true
	}
	for _, fd := range vars {
		selected[fd] = true
	}
	if len(selected) > 128 {
		return fmt.Errorf("too many inherited leases")
	}
	ordered := []int{}
	for fd := range selected {
		var stat syscall.Stat_t
		if fd < 3 || syscall.Fstat(fd, &stat) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFREG {
			return fmt.Errorf("invalid inherited lease descriptor")
		}
		ordered = append(ordered, fd)
	}
	sort.Ints(ordered)
	remap := map[int]int{}
	for _, fd := range ordered {
		remap[fd] = 3 + len(cmd.ExtraFiles)
		copy, e := syscall.Dup(fd)
		if e != nil {
			closeForwarded(cmd)
			return e
		}
		cmd.ExtraFiles = append(cmd.ExtraFiles, os.NewFile(uintptr(copy), "inherited-lease"))
	}
	env := cmd.Env[:0]
	for _, value := range cmd.Env {
		name, _, _ := strings.Cut(value, "=")
		_, remapped := vars[name]
		if !remapped && name != "TOOLCHAIN_ANCESTOR_FDS" && name != "TOOLCHAIN_OPERATION_ID" && name != "CHAINMAN_SERVICE_LEASE_FDS" {
			env = append(env, value)
		}
	}
	cmd.Env = env
	for name, fd := range vars {
		cmd.Env = append(cmd.Env, name+"="+strconv.Itoa(remap[fd]))
	}
	for i, fd := range ancestors {
		ancestors[i] = remap[fd]
	}
	for i, fd := range serviceLeases {
		serviceLeases[i] = remap[fd]
	}
	if len(serviceLeases) > 0 {
		data, e := json.Marshal(serviceLeases)
		if e != nil {
			return e
		}
		cmd.Env = append(cmd.Env, "CHAINMAN_SERVICE_LEASE_FDS="+string(data))
	}
	data, e := json.Marshal(ancestors)
	if e != nil {
		return e
	}
	if len(vars)+len(ancestors) > 0 {
		cmd.Env = append(cmd.Env, "TOOLCHAIN_ANCESTOR_FDS="+string(data), "TOOLCHAIN_OPERATION_ID="+os.Getenv("TOOLCHAIN_OPERATION_ID"))
		if owner := os.Getenv("CHAINMAN_COMPILER_OWNER"); owner != "" {
			cmd.Env = append(cmd.Env, "CHAINMAN_COMPILER_OWNER="+owner)
		}
	}
	return nil
}

func closeForwarded(cmd *exec.Cmd) {
	for _, file := range cmd.ExtraFiles {
		file.Close()
	}
}

func taskCommand(action, path string) int {
	var task TaskCommands
	if e := readJSON(path, &task); e != nil {
		return exitCode(e)
	}
	if len(task.Commands) == 0 || task.Timeout < 0 || task.Timeout > 86400 || task.Shutdown < 1 || task.Shutdown > 300 {
		return exitCode(fmt.Errorf("invalid task lifetime declaration"))
	}
	for _, command := range task.Commands {
		if _, e := child(command); e != nil {
			return exitCode(e)
		}
	}
	if action == "sequence" {
		for _, command := range task.Commands {
			cmd, e := child(command)
			if e != nil {
				return exitCode(e)
			}
			if e = forwardLeases(cmd); e != nil {
				return exitCode(e)
			}
			// The native anchor owns service leases through the whole command.
			// Do not advertise host control descriptors through Nix or an engine
			// daemon, which may close them before entering project code.
			env := cmd.Env[:0]
			for _, value := range cmd.Env {
				if !strings.HasPrefix(value, "CHAINMAN_SERVICE_LEASE_FDS=") {
					env = append(env, value)
				}
			}
			cmd.Env = env
			e = cmd.Run()
			closeForwarded(cmd)
			if e != nil {
				return exitCode(e)
			}
		}
		return 0
	}
	state, e := os.MkdirTemp("", "chainman-command-")
	if e != nil {
		return exitCode(e)
	}
	defer os.RemoveAll(state)
	self, e := os.Executable()
	if e != nil {
		return exitCode(e)
	}
	if e = atomic(filepath.Join(state, "sequence.json"), task); e != nil {
		return exitCode(e)
	}
	spec := Service{Command: Command{Argv: []string{self, "sequence", filepath.Join(state, "sequence.json")}, Directory: task.Commands[0].Directory}, Shutdown: task.Shutdown, Timeout: task.Timeout, ForwardLeases: true}
	if e = atomic(filepath.Join(state, "task.command.json"), spec); e != nil {
		return exitCode(e)
	}
	cmd, e := child(Command{Argv: []string{self, "exec", state, "task"}, Directory: task.Commands[0].Directory})
	if e != nil {
		return exitCode(e)
	}
	if e = forwardLeases(cmd); e != nil {
		return exitCode(e)
	}
	defer closeForwarded(cmd)
	signals := make(chan os.Signal, 8)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer signal.Stop(signals)
	if e = cmd.Start(); e != nil {
		return exitCode(e)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case e = <-done:
	case sig := <-signals:
		_ = cmd.Process.Signal(sig)
		e = <-done
	}
	return exitCode(e)
}
