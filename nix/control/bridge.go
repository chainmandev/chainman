package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// A private bridge is an ordinary engine resource. The existing parent-linked
// scope lease covers creation, service startup, task execution and final cleanup.
type Bridge struct {
	Engine         string `json:"engine"`
	EngineIdentity string `json:"engine_identity,omitempty"`
	Name           string `json:"name"`
	Scope          string `json:"scope"`
}

type BridgeState struct {
	ID     string            `json:"Id"`
	Name   string            `json:"Name"`
	Driver string            `json:"Driver"`
	Labels map[string]string `json:"Labels"`
}

func validateBridge(b *Bridge) error {
	if b == nil {
		return nil
	}
	if !filepath.IsAbs(b.Engine) || !regexp.MustCompile(`^[a-f0-9]{24}$`).MatchString(b.Scope) || b.Name != "chainman-"+b.Scope {
		return fmt.Errorf("invalid private network declaration")
	}
	return nil
}

func bridgeCommand(b *Bridge, args ...string) ([]byte, error) {
	if e := validateBridge(b); e != nil {
		return nil, e
	}
	if e := checkEngine(&Container{Engine: b.Engine, EngineIdentity: b.EngineIdentity, Name: b.Name}); e != nil {
		return nil, e
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	out, e := exec.CommandContext(ctx, b.Engine, append([]string{"network"}, args...)...).CombinedOutput()
	if e != nil {
		return nil, fmt.Errorf("network %s: %w: %s", b.Name, e, strings.TrimSpace(string(out)))
	}
	return out, nil
}

func inspectBridge(b *Bridge) (*BridgeState, error) {
	out, e := bridgeCommand(b, "inspect", b.Name)
	if e != nil {
		listed, err := bridgeCommand(b, "ls", "--format", "{{.Name}}")
		if err != nil {
			return nil, err
		}
		for _, name := range strings.Fields(string(listed)) {
			if name == b.Name {
				return nil, e
			}
		}
		return nil, nil
	}
	var entries []BridgeState
	if e = json.Unmarshal(out, &entries); e != nil {
		return nil, e
	}
	if len(entries) != 1 || entries[0].Name != b.Name || entries[0].ID == "" || entries[0].Driver != "bridge" || entries[0].Labels["dev.chainman.scope"] != b.Scope || entries[0].Labels["dev.chainman.network"] != "1" {
		return nil, fmt.Errorf("network %s is not an owned private bridge; no changes made", b.Name)
	}
	return &entries[0], nil
}

func ensureBridge(b *Bridge, active bool) error {
	current, e := inspectBridge(b)
	if e != nil {
		return e
	}
	if current != nil {
		return nil
	}
	if active {
		return fmt.Errorf("private network disappeared while clients still hold leases")
	}
	if _, e = bridgeCommand(b, "create", "--driver", "bridge", "--label", "dev.chainman.scope="+b.Scope, "--label", "dev.chainman.network=1", b.Name); e != nil {
		return e
	}
	current, e = inspectBridge(b)
	if e != nil {
		return e
	}
	if current == nil {
		return fmt.Errorf("created private network is missing")
	}
	return nil
}

func removeBridge(b *Bridge) error {
	current, e := inspectBridge(b)
	if e != nil || current == nil {
		return e
	}
	// Never disconnect endpoints or force removal. The immutable ID protects a
	// replacement name, and unrelated attached containers prevent removal.
	_, e = bridgeCommand(b, "rm", current.ID)
	return e
}
