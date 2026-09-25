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

// Only a foreground command may lend its controlling terminal to its owned
// group. Services and probes never use this path; pipes keep their original IO.
func taskTerminal(cmd *exec.Cmd) (func() error, error) {
	nothing := func() error { return nil }
	fd := int(os.Stdin.Fd())
	foreground, err := unix.IoctlGetInt(fd, unix.TIOCGPGRP)
	if err != nil || foreground != syscall.Getpgrp() {
		return nothing, nil
	}
	settings, err := unix.IoctlGetTermios(fd, terminalGet)
	if err != nil {
		return nil, err
	}
	copy, err := unix.Dup(fd)
	if err != nil {
		return nil, err
	}
	unix.CloseOnExec(copy)
	// Go performs setpgid and tcsetpgrp in the child with signals blocked,
	// before exec. There is no interval where the command can read as a
	// background job (SIGTTIN), or change terminal settings (SIGTTOU).
	cmd.SysProcAttr = &syscall.SysProcAttr{Foreground: true, Ctty: copy}
	return func() error {
		defer unix.Close(copy)
		if cmd.Process == nil {
			return nil
		}
		current, err := unix.IoctlGetInt(copy, unix.TIOCGPGRP)
		if err != nil {
			return err
		}
		if current != cmd.Process.Pid {
			// A shell or another foreground owner has already reclaimed it.
			return nil
		}
		// Restoring from the now-background parent must not suspend its group.
		ignored := signal.Ignored(syscall.SIGTTOU)
		signal.Ignore(syscall.SIGTTOU)
		defer func() {
			if !ignored {
				signal.Reset(syscall.SIGTTOU)
			}
		}()
		return errors.Join(
			unix.IoctlSetPointerInt(copy, unix.TIOCSPGRP, foreground),
			unix.IoctlSetTermios(copy, terminalSet, settings),
		)
	}, nil
}
