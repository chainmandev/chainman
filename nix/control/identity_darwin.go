package main

import (
	"fmt"
	"golang.org/x/sys/unix"
)

// Use upstream Darwin process structures and sysctl wrappers rather than
// maintaining syscall numbers or kernel structure offsets in Chainman.
func birth(pid int) (string, error) {
	info, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
	if err != nil {
		return "", err
	}
	if info.Proc.P_pid != int32(pid) || info.Proc.P_stat == 5 {
		return "", fmt.Errorf("process is gone")
	}
	return fmt.Sprintf("%d:%d", info.Proc.P_starttime.Sec, info.Proc.P_starttime.Usec), nil
}
func groupMembers(group int) ([]int, error) {
	entries, err := unix.SysctlKinfoProcSlice("kern.proc.pgrp", group)
	if err != nil {
		return nil, err
	}
	result := make([]int, 0, len(entries))
	for _, entry := range entries {
		if entry.Eproc.Pgid == int32(group) {
			result = append(result, int(entry.Proc.P_pid))
		}
	}
	return result, nil
}
