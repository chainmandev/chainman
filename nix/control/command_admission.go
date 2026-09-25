package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"syscall"
)

// Start a verified helper with a closed admission gate. Until permission is
// written, neither normal execution nor a cancelled supervisor can launch the
// workload. The helper keeps default signal handling and becomes the workload
// with exec, so a foreground interrupt cannot fall between two workload PIDs.
func startAdmitted(cmd *exec.Cmd, signals chan os.Signal) error {
	select {
	case sig := <-signals:
		return &startupInterrupted{sig.(syscall.Signal)}
	default:
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	read, permit, err := os.Pipe()
	if err != nil {
		return err
	}
	defer read.Close()
	defer permit.Close()
	path, args, files := cmd.Path, cmd.Args, cmd.ExtraFiles
	defer func() { cmd.Path, cmd.Args, cmd.ExtraFiles = path, args, files }()
	cmd.Path = self
	cmd.Args = append([]string{self, "admitted", strconv.Itoa(3 + len(files)), path}, args...)
	cmd.ExtraFiles = append(files, read)
	if err = cmd.Start(); err != nil {
		return err
	}
	read.Close()
	return admitStarted(cmd, permit, signals)
}

func admitStarted(cmd *exec.Cmd, permit *os.File, signals chan os.Signal) error {
	// Stop drains the runtime's pending signal notifications. A signal from
	// before the helper existed must veto admission, rather than be mistaken for
	// one the helper already received. While notifications are reset, default
	// termination closes the permit pipe; the blocked helper then fails closed.
	signal.Stop(signals)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	select {
	case sig := <-signals:
		// No permit was sent, so this process cannot contain application work.
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return &startupInterrupted{sig.(syscall.Signal)}
	default:
	}
	if _, err := permit.Write([]byte{1}); err != nil {
		// A terminal interrupt may have already ended the gated helper.
		if ended := cmd.Wait(); ended != nil {
			return ended
		}
		return err
	}
	return nil
}

func admittedCommand(args []string) int {
	if len(args) < 3 {
		return 2
	}
	fd, err := strconv.Atoi(args[0])
	var stat syscall.Stat_t
	if err != nil || fd < 3 || syscall.Fstat(fd, &stat) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFIFO {
		return exitCode(fmt.Errorf("invalid command admission descriptor"))
	}
	permit := os.NewFile(uintptr(fd), "command-admission")
	var value [1]byte
	_, err = io.ReadFull(permit, value[:])
	permit.Close()
	if err != nil || value[0] != 1 {
		return 125
	}
	// No signal.Notify here: default handling must remain active through exec.
	return exitCode(syscall.Exec(args[1], args[2:], os.Environ()))
}
