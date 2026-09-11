package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"syscall"
)

// Linux start ticks distinguish a process from later reuse of the same PID.
func birth(pid int) (string, error) {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return "", err
	}
	i := strings.LastIndexByte(string(b), ')')
	if i < 0 {
		return "", fmt.Errorf("invalid process stat")
	}
	f := strings.Fields(string(b[i+1:]))
	if len(f) < 20 || f[0] == "Z" {
		return "", fmt.Errorf("process is gone")
	}
	if _, err := strconv.ParseUint(f[19], 10, 64); err != nil {
		return "", err
	}
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	return strings.TrimSpace(string(boot)) + ":" + f[19], err
}

func groupMembers(group int) ([]int, error) {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil, err
	}
	var result []int
	for _, entry := range entries {
		pid, e := strconv.Atoi(entry.Name())
		if e != nil {
			continue
		}
		g, e := syscall.Getpgid(pid)
		if e == nil && g == group {
			result = append(result, pid)
		}
	}
	return result, nil
}
