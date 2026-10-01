package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

// The lease inode is also the inherited flock witness. Never rewrite or replace
// it after admission: a killed writer must not leave a partial receipt, and a
// replacement inode would disconnect surviving descendants from the lock.
func leaseTaskAlive(path string, lease Lease) (bool, error) {
	if !lease.TaskReceipt {
		return lease.Task != nil && lease.Task.alive(), nil
	}
	var identity Identity
	if err := readJSON(path+".task.json", &identity); os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, err
	}
	return identity.alive(), nil
}

func removeLease(path string) error {
	if err := os.Remove(path); err != nil {
		return err
	}
	for _, suffix := range []string{".task.json", ".command.json"} {
		if err := os.Remove(path + suffix); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}

func publishLeaseTask(path string, inherited *os.File) error {
	if !strings.HasSuffix(path, ".lease") {
		return fmt.Errorf("invalid task lease path")
	}
	if err := private(filepath.Dir(path)); err != nil {
		return err
	}
	gate, err := locked(filepath.Join(filepath.Dir(path), "gate"), false)
	if err != nil {
		return err
	}
	defer gate.Close()
	// Explicit stop may have removed the receipt before this child was scheduled.
	current, err := os.Lstat(path)
	if err != nil {
		return err
	}
	owned, err := inherited.Stat()
	if err != nil {
		return err
	}
	if !current.Mode().IsRegular() || !os.SameFile(current, owned) {
		return fmt.Errorf("task lease descriptor does not match its receipt")
	}
	if err = syscall.Flock(int(inherited.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return err
	}
	var lease Lease
	if err = readJSON(path, &lease); err != nil {
		return err
	}
	if !lease.TaskReceipt || lease.Persistent || lease.Parent != nil {
		return fmt.Errorf("invalid task identity receipt")
	}
	identity, err := identify(os.Getpid())
	if err != nil {
		return err
	}
	return atomic(path+".task.json", identity)
}

// Publish the exec identity before project code can close inherited descriptors
// or signal readiness. Exec preserves PID, process group, streams and arguments;
// this helper adds no resident supervisor or polling loop.
func leasedTask(path string) error {
	var command Command
	if err := readJSON(path+".command.json", &command); err != nil {
		return err
	}
	cmd, err := child(command)
	if err != nil {
		return err
	}
	if cmd.Err != nil {
		return cmd.Err
	}
	lease := os.NewFile(3, "task-lease")
	defer lease.Close()
	if err = publishLeaseTask(path, lease); err != nil {
		return err
	}
	// Unlike an os/exec child, this exec preserves descriptor numbers. Reacquire
	// ownership independently of the resource lease at descriptor 3, then expose
	// the actual open descriptor rather than ExtraFiles' spawn-time remapping.
	if err = forwardUpdateLease(cmd); err != nil {
		return err
	}
	defer closeForwarded(cmd)
	if len(cmd.ExtraFiles) > 0 {
		update := cmd.ExtraFiles[len(cmd.ExtraFiles)-1]
		cmd.Env = append(cmd.Env, fmt.Sprintf("CHAINMAN_UPDATE_LEASE_FD=%d", update.Fd()))
		if _, err = unix.FcntlInt(update.Fd(), unix.F_SETFD, 0); err != nil {
			return err
		}
	}
	if cmd.Dir != "" {
		if err = os.Chdir(cmd.Dir); err != nil {
			return err
		}
	}
	// Preserve os/exec's last-value-wins overrides. Raw Env can contain both
	// inherited and declared entries; execve leaves their interpretation to the
	// child, and Go/libc would otherwise keep the inherited value.
	return syscall.Exec(cmd.Path, cmd.Args, cmd.Environ())
}
