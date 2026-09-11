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

type Volume struct {
	Engine         string   `json:"engine"`
	EngineIdentity string   `json:"engine_identity,omitempty"`
	Name           string   `json:"name"`
	Scope          string   `json:"scope"`
	Compatibility  string   `json:"compatibility"`
	Policy         string   `json:"policy"`
	Services       []string `json:"services"`
}

type VolumeState struct {
	Name   string            `json:"Name"`
	Labels map[string]string `json:"Labels"`
}

func volumeCommand(v Volume, args ...string) ([]byte, error) {
	if e := checkEngine(&Container{Engine: v.Engine, EngineIdentity: v.EngineIdentity, Name: v.Name}); e != nil {
		return nil, e
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	out, e := exec.CommandContext(ctx, v.Engine, append([]string{"volume"}, args...)...).CombinedOutput()
	if e != nil {
		return nil, fmt.Errorf("volume %s: %w: %s", v.Name, e, strings.TrimSpace(string(out)))
	}
	return out, nil
}

func inspectVolume(v Volume) (*VolumeState, error) {
	out, e := volumeCommand(v, "inspect", v.Name)
	if e != nil {
		listed, err := volumeCommand(v, "ls", "--format", "{{.Name}}")
		if err != nil {
			return nil, err
		}
		for _, name := range strings.Fields(string(listed)) {
			if name == v.Name {
				return nil, e
			}
		}
		return nil, nil
	}
	var entries []VolumeState
	if e = json.Unmarshal(out, &entries); e != nil {
		return nil, e
	}
	if len(entries) != 1 || entries[0].Name != v.Name {
		return nil, fmt.Errorf("unexpected volume identity")
	}
	return &entries[0], nil
}

func ensureVolumes(volumes []Volume, active bool) error {
	digest := regexp.MustCompile(`^[a-f0-9]{64}$`)
	scope := regexp.MustCompile(`^[a-f0-9]{24}$`)
	for _, v := range volumes {
		if !filepath.IsAbs(v.Engine) || !validName.MatchString(v.Name) || !strings.HasPrefix(v.Name, "chainman-") || !scope.MatchString(v.Scope) || !digest.MatchString(v.Compatibility) || (v.Policy != "preserve" && v.Policy != "disposable") {
			return fmt.Errorf("invalid volume declaration")
		}
		current, e := inspectVolume(v)
		if e != nil {
			return e
		}
		if current != nil {
			if current.Labels["dev.chainman.scope"] != v.Scope {
				return fmt.Errorf("volume %s is not owned by this scope; no changes made", v.Name)
			}
			if current.Labels["dev.chainman.compatibility"] == v.Compatibility {
				continue
			}
			if active || v.Policy != "disposable" {
				return fmt.Errorf("volume %s has incompatible data; preserve/migrate it or explicitly reset disposable state after stopping its users", v.Name)
			}
			// Engine removal is intentionally unforced. References outside Chainman
			// also prevent removal, even when no Chainman client holds a lease.
			if _, e = volumeCommand(v, "rm", v.Name); e != nil {
				return e
			}
		} else if active {
			return fmt.Errorf("volume %s disappeared while services are in use", v.Name)
		}
		if _, e = volumeCommand(v, "create", "--label", "dev.chainman.scope="+v.Scope, "--label", "dev.chainman.compatibility="+v.Compatibility, v.Name); e != nil {
			return e
		}
		created, e := inspectVolume(v)
		if e != nil {
			return e
		}
		if created == nil || created.Labels["dev.chainman.scope"] != v.Scope || created.Labels["dev.chainman.compatibility"] != v.Compatibility {
			return fmt.Errorf("created volume ownership did not match")
		}
	}
	return nil
}
