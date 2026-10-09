package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// Provisioning is outside application readiness. Its roots remain leased until
// acquired services have entered their own Nix environments. The helper uses
// ordinary native command containment, never a detached or unowned installer.
type profilePreparation struct {
	directory string
	alive     *os.File
	command   *exec.Cmd
	done      chan struct{}
	err       error
	shutdown  int
	closed    bool
}

func (p *profilePreparation) outcome() error {
	if p.err != nil {
		return p.err
	}
	return fmt.Errorf("profile helper exited without retaining its roots")
}

func (p *profilePreparation) close() error {
	if p.closed {
		return nil
	}
	p.closed = true
	if err := atomic(filepath.Join(p.directory, "outgoing", "released.json"), struct {
		Schema int `json:"schema"`
	}{1}); err != nil {
		_ = p.command.Process.Signal(syscall.SIGTERM)
	}
	p.alive.Close()
	select {
	case <-p.done:
	case <-time.After(time.Duration(p.shutdown) * time.Second):
		_ = p.command.Process.Signal(syscall.SIGTERM)
		select {
		case <-p.done:
		case <-time.After(time.Duration(p.shutdown+2) * time.Second):
			_ = p.command.Process.Kill()
			<-p.done
		}
	}
	return errors.Join(p.err, os.RemoveAll(p.directory))
}

func startProfilePreparation(command Command, shutdown int, signals chan os.Signal, startup *startupGuard) (result *profilePreparation, err error) {
	if shutdown == 0 {
		shutdown = 10
	}
	if shutdown < 1 || shutdown > 300 {
		return nil, fmt.Errorf("invalid profile preparation shutdown bound")
	}
	temporary, err := os.MkdirTemp("", "chainman-profile-preparation-")
	if err != nil {
		return nil, err
	}
	directory, err := filepath.EvalSymlinks(temporary)
	if err != nil {
		_ = os.RemoveAll(temporary)
		return nil, err
	}
	defer func() {
		if result == nil {
			_ = os.RemoveAll(directory)
		}
	}()
	for _, name := range []string{"incoming", "outgoing"} {
		if err = private(filepath.Join(directory, name)); err != nil {
			return nil, err
		}
	}
	alive, err := locked(filepath.Join(directory, "outgoing", "alive"), false)
	if err != nil {
		return nil, err
	}
	defer func() {
		if result == nil {
			alive.Close()
		}
	}()
	// Do not mutate the frozen plan's command environment.
	env := map[string]string{}
	for key, value := range command.Environment {
		env[key] = value
	}
	env["CHAINMAN_PROFILE_CHANNEL"] = directory
	command.Environment = env
	path := filepath.Join(directory, "command.json")
	if err = atomic(path, TaskCommands{Commands: []Command{command}, Shutdown: shutdown}); err != nil {
		return nil, err
	}
	self, err := os.Executable()
	if err != nil {
		return nil, err
	}
	cmd, err := child(Command{Argv: []string{self, "profile-preparation", path}, Directory: command.Directory})
	if err != nil {
		return nil, err
	}
	cmd.Stdin = nil
	if err = forwardLeases(cmd); err != nil {
		return nil, err
	}
	defer closeForwarded(cmd)
	if err = startAdmitted(cmd, signals); err != nil {
		return nil, err
	}
	p := &profilePreparation{directory: directory, alive: alive, command: cmd, done: make(chan struct{}), shutdown: shutdown}
	go func() { p.err = cmd.Wait(); close(p.done) }()
	defer func() {
		if result == nil {
			// Failed or interrupted admission cancels provisioning now, rather
			// than waiting for the normal root-release grace period first.
			_ = p.command.Process.Signal(syscall.SIGTERM)
			_ = p.close()
		}
	}()
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-p.done:
			return nil, p.outcome()
		case sig := <-signals:
			return nil, &startupInterrupted{sig.(syscall.Signal)}
		case <-ticker.C:
			if err := startup.check(); err != nil {
				return nil, err
			}
			var ready struct {
				Schema int `json:"schema"`
			}
			err := readJSON(filepath.Join(directory, "incoming", "ready.json"), &ready)
			if os.IsNotExist(err) {
				continue
			}
			if err != nil {
				return nil, err
			}
			if ready.Schema != 1 {
				return nil, fmt.Errorf("invalid profile preparation receipt")
			}
			return p, nil
		}
	}
}

// The liveness lease is not inherited by any child. A killed native caller
// releases it in the kernel; this watcher then cancels even a cold Nix build.
func profilePreparationCommand(path string) int {
	directory := filepath.Dir(path)
	if filepath.Base(path) != "command.json" || !strings.HasPrefix(filepath.Base(directory), "chainman-profile-preparation-") {
		return exitCode(fmt.Errorf("invalid profile preparation directory"))
	}
	if err := existingPrivate(directory); err != nil {
		return exitCode(err)
	}
	alive, err := os.OpenFile(filepath.Join(directory, "outgoing", "alive"), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return exitCode(err)
	}
	defer alive.Close()
	info, err := alive.Stat()
	if err != nil {
		return exitCode(err)
	}
	if !info.Mode().IsRegular() || info.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid()) || info.Sys().(*syscall.Stat_t).Nlink != 1 {
		return exitCode(fmt.Errorf("invalid profile preparation owner lease"))
	}
	// The surviving contained helper removes only its validated, private channel,
	// including when the original native caller was killed and cannot clean up.
	defer os.RemoveAll(directory)
	stop := make(chan struct{})
	stopped := make(chan struct{})
	defer func() { close(stop); <-stopped }()
	go func() {
		defer close(stopped)
		ticker := time.NewTicker(50 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-stop:
				return
			case <-ticker.C:
				if syscall.Flock(int(alive.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) == nil {
					_ = syscall.Flock(int(alive.Fd()), syscall.LOCK_UN)
					// Normal release lets the Python holder close its roots cleanly;
					// an abrupt caller loss cancels provisioning immediately.
					var released struct {
						Schema int `json:"schema"`
					}
					if readJSON(filepath.Join(directory, "outgoing", "released.json"), &released) == nil && released.Schema == 1 {
						return
					}
					_ = syscall.Kill(os.Getpid(), syscall.SIGTERM)
					return
				}
			}
		}
	}()
	return taskCommand("command", path)
}
