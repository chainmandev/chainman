package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
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

func validateVolume(v Volume) error {
	digest := regexp.MustCompile(`^[a-f0-9]{64}$`)
	scope := regexp.MustCompile(`^[a-f0-9]{24}$`)
	if !filepath.IsAbs(v.Engine) || !validName.MatchString(v.Name) || !strings.HasPrefix(v.Name, "chainman-") || !scope.MatchString(v.Scope) || !digest.MatchString(v.Compatibility) || (v.Policy != "preserve" && v.Policy != "disposable") {
		return fmt.Errorf("invalid volume declaration")
	}
	return nil
}

func ensureVolumes(volumes []Volume, active bool) error {
	for _, v := range volumes {
		if e := validateVolume(v); e != nil {
			return e
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

// Explicit reset holds the same admission gates as service acquisition. It
// refuses live users in either scope, validates every selected volume before
// mutation, and never asks the engine for forced removal. This is separate from
// stop: ordinary recovery must preserve application data.
func resetVolumes(p Plan) error {
	if e := validate(p); e != nil {
		return e
	}
	scopes := append([]Plan{p}, p.Resources...)
	saved := []Plan{}
	volumes := []Volume{}
	for _, scope := range scopes {
		if e := private(scope.State); e != nil {
			return e
		}
		gate, e := locked(filepath.Join(scope.State, "gate"), false)
		if e != nil {
			return e
		}
		defer gate.Close()
		var previous Plan
		e = readJSON(filepath.Join(scope.State, "plan.json"), &previous)
		if e == nil {
			if previous.State != scope.State || previous.Root != scope.Root {
				return fmt.Errorf("saved reset scope does not match this project")
			}
			used, err := active(previous)
			if err != nil {
				return err
			}
			if len(used) > 0 || hasLeases(previous) {
				return fmt.Errorf("volume reset requires all users of the selected worktree and repository service pool to stop first")
			}
			// Resources already have their own gate in this transaction.
			previous.Resources = nil
			saved = append(saved, previous)
		} else if !os.IsNotExist(e) {
			return e
		}
		selected, e := ordered(scope, scope.Requested)
		if e != nil {
			return e
		}
		for _, volume := range scope.Volumes {
			needed := false
			for _, service := range selected {
				for _, owner := range volume.Services {
					if owner == service {
						needed = true
					}
				}
			}
			if !needed {
				continue
			}
			if e := validateVolume(volume); e != nil {
				return e
			}
			for _, old := range previous.Volumes {
				if old.Name == volume.Name && (old.Engine != volume.Engine || old.EngineIdentity != volume.EngineIdentity) {
					return fmt.Errorf("volume engine identity changed; restore the original engine context before reset")
				}
			}
			current, e := inspectVolume(volume)
			if e != nil {
				return e
			}
			if current != nil {
				if current.Labels["dev.chainman.scope"] != volume.Scope || current.Labels["dev.chainman.compatibility"] == "" {
					return fmt.Errorf("volume %s is not owned by this scope; no changes made", volume.Name)
				}
				volumes = append(volumes, volume)
			}
		}
	}
	// All identity and live-claim checks precede process or volume changes.
	for _, previous := range saved {
		if e := releaseUnused(previous, false); e != nil {
			return e
		}
	}
	for _, volume := range volumes {
		// Recheck ownership after cleanup, before addressing the volume by name.
		current, e := inspectVolume(volume)
		if e != nil {
			return e
		}
		if current == nil {
			continue
		}
		if current.Labels["dev.chainman.scope"] != volume.Scope || current.Labels["dev.chainman.compatibility"] == "" {
			return fmt.Errorf("volume ownership changed during reset")
		}
		if _, e = volumeCommand(volume, "rm", volume.Name); e != nil {
			return e
		}
		fmt.Printf("Removed owned volume %s\n", volume.Name)
	}
	return nil
}
