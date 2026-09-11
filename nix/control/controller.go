package main

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// The child registers itself before exec, so killing the acquiring client at any
// point cannot leave a started backend without a kernel identity receipt.
func controllerExec(path, generation string) int {
	state := filepath.Dir(path)
	if e := private(state); e != nil {
		return exitCode(e)
	}
	admission, e := locked(filepath.Join(state, "controller.admission"), false)
	if e != nil {
		return exitCode(e)
	}
	defer admission.Close()
	var p Plan
	if e := readJSON(path, &p); e != nil {
		return exitCode(e)
	}
	if p.State != state || generation == "" || generation != p.Generation {
		return exitCode(fmt.Errorf("controller generation changed before admission"))
	}
	if e := private(p.State); e != nil {
		return exitCode(e)
	}
	if syscall.Getpgrp() != os.Getpid() {
		return exitCode(fmt.Errorf("controller must own its process group"))
	}
	id, e := identify(os.Getpid())
	if e != nil {
		return exitCode(e)
	}
	if e = atomic(filepath.Join(p.State, "controller.json"), id); e != nil {
		return exitCode(e)
	}
	if _, e = os.Stat(filepath.Join(p.State, "stop-request.json")); e == nil {
		return 0
	} else if !os.IsNotExist(e) {
		return exitCode(e)
	}
	socket, e := socketPath(p)
	if e != nil {
		return exitCode(e)
	}
	args := []string{p.Backend, "--use-uds", "--unix-socket", socket, "--log-file", os.DevNull, "--ordered-shutdown", "up", "--config", filepath.Join(p.State, "compose.json"), "--disable-dotenv", "--tui=false", "--keep-project"}
	return exitCode(syscall.Exec(p.Backend, append(args, p.Requested...), os.Environ()))
}

func serviceStopping(p Plan, name string, stopping bool) error {
	admission, e := locked(filepath.Join(p.State, name+".admission"), false)
	if e != nil {
		return e
	}
	defer admission.Close()
	path := filepath.Join(p.State, name+".stopping")
	if stopping {
		return atomic(path, true)
	}
	return clearStopping(p, name)
}

func clearStopping(p Plan, name string) error {
	e := os.Remove(filepath.Join(p.State, name+".stopping"))
	if os.IsNotExist(e) {
		return nil
	}
	return e
}
