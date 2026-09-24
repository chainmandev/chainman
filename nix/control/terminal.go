package main

import (
	"errors"
	"os"
	"os/exec"
	"os/signal"
	"syscall"

	"golang.org/x/sys/unix"
)

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
