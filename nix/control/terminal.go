package main

import (
	"errors"
	"os"
	"os/exec"
	"os/signal"
	"syscall"

	"golang.org/x/sys/unix"
)

func foregroundTask(group int) bool {
	foreground, err := unix.IoctlGetInt(int(os.Stdin.Fd()), unix.TIOCGPGRP)
	return err == nil && foreground == group
}

// Foreground SIGINT is a group event, whether it comes from the terminal or a
// direct interrupt of the public command. Its owner observes it without sending
// it twice. Direct owner cancellation (including explicit stop) uses SIGTERM.
func interruptTask(cmd *exec.Cmd, sig os.Signal) {
	if sig == syscall.SIGINT && foregroundTask(cmd.Process.Pid) {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGINT)
	} else {
		_ = cmd.Process.Signal(sig)
	}
	_ = cmd.Process.Signal(syscall.SIGCONT)
}

type taskTTY struct {
	fd       int
	group    int
	settings *unix.Termios
	stopped  *unix.Termios
}

// Keep a terminal descriptor even for an initially background job: a later fg
// may lend it the terminal. Services and probes never use this path.
func taskTerminal(cmd *exec.Cmd) (*taskTTY, error) {
	t := &taskTTY{fd: -1, group: syscall.Getpgrp()}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	fd := int(os.Stdin.Fd())
	foreground, err := unix.IoctlGetInt(fd, unix.TIOCGPGRP)
	if err != nil {
		return t, nil
	}
	copy, err := unix.Dup(fd)
	if err != nil {
		return nil, err
	}
	unix.CloseOnExec(copy)
	t.fd = copy
	if foreground != t.group {
		return t, nil
	}
	t.settings, err = unix.IoctlGetTermios(copy, terminalGet)
	if err != nil {
		unix.Close(copy)
		return nil, err
	}
	// Go performs setpgid and tcsetpgrp in the child with signals blocked,
	// before exec. There is no interval where the command can read as a
	// background job (SIGTTIN), or change terminal settings (SIGTTOU).
	cmd.SysProcAttr = &syscall.SysProcAttr{Foreground: true, Ctty: copy}
	return t, nil
}

func withoutTerminalStop(action func() error) error {
	ignored := signal.Ignored(syscall.SIGTTOU)
	signal.Ignore(syscall.SIGTTOU)
	defer func() {
		if !ignored {
			signal.Reset(syscall.SIGTTOU)
		}
	}()
	return action()
}

func (t *taskTTY) reclaim(pid int, stopping bool) error {
	if t.fd < 0 {
		return nil
	}
	current, err := unix.IoctlGetInt(t.fd, unix.TIOCGPGRP)
	if err != nil || current != pid {
		// Never take the terminal away from the shell or a different job.
		return err
	}
	return withoutTerminalStop(func() error {
		if stopping {
			t.stopped, err = unix.IoctlGetTermios(t.fd, terminalGet)
			if err != nil {
				return err
			}
		}
		err = unix.IoctlSetPointerInt(t.fd, unix.TIOCSPGRP, t.group)
		if t.settings != nil {
			err = errors.Join(err, unix.IoctlSetTermios(t.fd, terminalSet, t.settings))
		}
		return err
	})
}

func (t *taskTTY) resume(pid int) error {
	if t.fd < 0 {
		return nil
	}
	current, err := unix.IoctlGetInt(t.fd, unix.TIOCGPGRP)
	if err != nil || current != t.group {
		return err // bg resumes execution, but must not acquire the terminal.
	}
	return withoutTerminalStop(func() error {
		t.settings, err = unix.IoctlGetTermios(t.fd, terminalGet)
		if err != nil {
			return err
		}
		if err = unix.IoctlSetPointerInt(t.fd, unix.TIOCSPGRP, pid); err != nil {
			if errors.Is(err, syscall.ESRCH) {
				return nil
			}
			return err
		}
		if t.stopped != nil {
			return unix.IoctlSetTermios(t.fd, terminalSet, t.stopped)
		}
		return nil
	})
}

func (t *taskTTY) close(cmd *exec.Cmd) error {
	if t.fd < 0 {
		return nil
	}
	defer unix.Close(t.fd)
	if cmd.Process == nil {
		return nil
	}
	return t.reclaim(cmd.Process.Pid, false)
}

// os/exec only waits for exits. This is the sole status waiter for the task
// anchor, so stops cannot be missed or exits reaped by a competing goroutine.
// The command uses file-backed stdio (no copying goroutines); Cmd.Wait below
// releases os/exec resources after wait4 has collected the actual exit status.
func waitTask(cmd *exec.Cmd, terminal *taskTTY, signals <-chan os.Signal, changed, resumed <-chan os.Signal) int {
	interrupted := false
	cancel := func(sig os.Signal) {
		if !interrupted {
			interrupted = true
			interruptTask(cmd, sig)
		}
	}
	for {
		var status syscall.WaitStatus
		pid, err := syscall.Wait4(cmd.Process.Pid, &status, syscall.WNOHANG|syscall.WUNTRACED, nil)
		if errors.Is(err, syscall.EINTR) {
			continue
		}
		if err != nil {
			interruptTask(cmd, syscall.SIGTERM)
			_ = cmd.Wait()
			return exitCode(err)
		}
		if pid != 0 {
			if status.Exited() || status.Signaled() {
				if err = cmd.Wait(); err != nil && !errors.Is(err, syscall.ECHILD) {
					return exitCode(err)
				}
				if status.Signaled() {
					return 128 + int(status.Signal())
				}
				return status.ExitStatus()
			}
			if status.Stopped() {
				select {
				case sig := <-signals:
					cancel(sig)
				default:
				}
				if !interrupted && terminal.fd < 0 {
					// Non-interactive owners stay available for direct cancellation.
					continue
				}
				if !interrupted {
					err = terminal.reclaim(pid, true)
					if err == nil {
						// Ignore a previous continue notification, then wait for the
						// next one explicitly. With Go's threads, Kill can return before
						// the whole process stops; resuming the child then would race
						// the shell reclaiming the terminal.
						select {
						case <-resumed:
						default:
						}
						// Pause waiting shells/runtime wrappers in this job too.
						err = syscall.Kill(-terminal.group, syscall.SIGSTOP)
						if err == nil {
							<-resumed
							err = terminal.resume(pid)
						}
					}
					if err != nil {
						cancel(syscall.SIGTERM)
					}
				}
				_ = syscall.Kill(-pid, syscall.SIGCONT)
				if err != nil {
					// Preserve bounded native cleanup before reporting a tty error.
					_ = cmd.Wait()
					return exitCode(err)
				}
			}
			continue
		}
		select {
		case sig := <-signals:
			cancel(sig)
		case <-changed:
		}
	}
}
