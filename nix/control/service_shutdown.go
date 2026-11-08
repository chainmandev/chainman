package main

import (
	"fmt"
	"os"
	"path/filepath"
)

// Dependents have drained before this bounded hook runs. A failure preserves
// the old application snapshot, still releases owned processes, and is reported
// to the caller of stop. Recovery never executes an unowned container by name.
func shutdownService(p Plan, name string) error {
	s := p.Services[name]
	if s.ShutdownCommand == nil {
		return nil
	}
	var owner Owner
	if err := readJSON(filepath.Join(p.State, name+".owner.json"), &owner); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return err
	}
	if s.Container == nil && !owner.Identity.alive() {
		return nil
	}
	command, err := containerProbe(s.Container, *s.ShutdownCommand)
	if err != nil {
		return fmt.Errorf("shutdown hook %s: %w", name, err)
	}
	directory, err := os.MkdirTemp("", "chainman-shutdown-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(directory)
	path := filepath.Join(directory, "command.json")
	if err := atomic(path, TaskCommands{Commands: []Command{command}, Timeout: s.Shutdown, Shutdown: 5}); err != nil {
		return err
	}
	if code := taskCommand("command", path); code != 0 {
		return fmt.Errorf("shutdown hook %s failed (%d); last successful snapshot was preserved", name, code)
	}
	return nil
}
