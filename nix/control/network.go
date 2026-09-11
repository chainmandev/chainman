package main

import (
	"fmt"
	"path/filepath"
)

// A declared borrower uses the engine's ordinary container network namespace.
// No proxy, host networking, extra daemon or project-visible engine socket.
func networkOwner(p Plan, name string) (*Container, error) {
	if !validName.MatchString(name) {
		return nil, fmt.Errorf("invalid network service")
	}
	if service, ok := p.Services[name]; ok {
		if service.Container == nil || service.NetworkService != "" || (service.Restart != "" && service.Restart != "no") {
			return nil, fmt.Errorf("network service requires a non-restarting container owner")
		}
		return service.Container, nil
	}
	for _, resource := range p.Resources {
		var saved Plan
		if e := readJSON(filepath.Join(resource.State, "plan.json"), &saved); e != nil {
			return nil, e
		}
		if saved.State != resource.State || len(saved.Resources) != 0 {
			return nil, fmt.Errorf("invalid network resource scope")
		}
		if _, ok := saved.Services[name]; ok {
			return networkOwner(saved, name)
		}
	}
	return nil, fmt.Errorf("network service is not in the acquired plan")
}

func joinNetwork(p Plan, name string, borrower *Container, command Command) (Command, error) {
	owner, e := networkOwner(p, name)
	if e != nil {
		return command, e
	}
	if borrower == nil || owner.Engine != borrower.Engine {
		return command, fmt.Errorf("network peers require the same host engine")
	}
	if e = checkEngine(borrower); e != nil {
		return command, e
	}
	current, e := inspectContainer(owner)
	if e != nil {
		return command, e
	}
	if current == nil || !current.Running {
		return command, fmt.Errorf("network owner is no longer running")
	}
	if len(command.Argv) > 1 && command.Argv[0] == borrower.Engine && command.Argv[1] == "run" {
		command.Argv = append([]string{borrower.Engine, "run", "--network", "container:" + current.ID}, command.Argv[2:]...)
	} else {
		env := map[string]string{}
		for key, value := range command.Environment {
			env[key] = value
		}
		env["CHAINMAN_CONTAINER_NETWORK"] = current.ID
		command.Environment = env
	}
	return command, nil
}
