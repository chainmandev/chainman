package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
)

var hookEvents = []string{"pre-commit", "pre-push"}

func hookBridge(event string) []byte {
	return []byte("#!/bin/sh\n# chainman Git hook bridge v1\nset -eu\nroot=$(git rev-parse --show-toplevel)\nexec just --justfile \"$root/justfile\" --working-directory \"$root\" chainman hooks run " + event + " \"$@\"\n")
}
func (p HookPlan) hookRepo() bool {
	root, e := p.query("rev-parse", "--show-toplevel")
	return e == nil && root == p.Root
}
func (p HookPlan) hookDirectory() (string, error) {
	d, e := p.query("rev-parse", "--absolute-git-dir")
	if e != nil {
		return "", e
	}
	return hookPath(d, "chainman-hooks")
}
func (p HookPlan) hookSelected() (string, error) {
	b, e := p.git(p.Root, "", nil, true, "config", "--path", "--get", "core.hooksPath")
	if hookAbsent(e) {
		e = nil
	}
	return strings.TrimSuffix(string(b), "\n"), e
}
func hookAbsolute(root, path string) string {
	if !filepath.IsAbs(path) {
		path = filepath.Join(root, path)
	}
	return filepath.Clean(path)
}
func (p HookPlan) hookStatus() (map[string]any, error) {
	if !p.hookRepo() {
		return map[string]any{"applicable": false, "reason": "not a Git project root"}, nil
	}
	target, e := p.hookDirectory()
	if e != nil {
		return nil, e
	}
	selected, e := p.hookSelected()
	if e != nil {
		return nil, e
	}
	intact := true
	for _, event := range hookEvents {
		f, e := hookRead(filepath.Join(target, event))
		if e != nil || !f.Exists || !bytes.Equal(f.Body, hookBridge(event)) || f.Mode&0111 == 0 {
			intact = false
		}
	}
	return map[string]any{"applicable": true, "installed": selected != "" && hookAbsolute(p.Root, selected) == target && intact, "path": target, "selected_path": selected}, nil
}
func (p HookPlan) hookCheck() error {
	if !p.hookRepo() {
		return nil
	}
	target, e := p.hookDirectory()
	if e != nil {
		return e
	}
	selected, e := p.hookSelected()
	if e != nil {
		return e
	}
	intact := true
	for _, event := range hookEvents {
		f, e := hookRead(filepath.Join(target, event))
		if e != nil {
			return e
		}
		if f.Exists && !bytes.Equal(f.Body, hookBridge(event)) {
			return fmt.Errorf("refusing to replace modified hook: %s", event)
		}
		if !f.Exists {
			intact = false
		}
	}
	record, e := hookRead(filepath.Join(target, "ownership.json"))
	if e != nil {
		return e
	}
	var ownership struct {
		Setting string `json:"setting"`
	}
	if record.Exists {
		if e = json.Unmarshal(record.Body, &ownership); e != nil {
			return e
		}
	}
	if selected != "" && hookAbsolute(p.Root, selected) != target && !(intact && selected == ownership.Setting) {
		return fmt.Errorf("Git hooks are managed at %q; resolve core.hooksPath before just hooks install; existing hooks were preserved", selected)
	}
	common, e := p.query("rev-parse", "--path-format=absolute", "--git-common-dir")
	if e != nil {
		return e
	}
	if selected == "" {
		entries, e := os.ReadDir(filepath.Join(common, "hooks"))
		if e != nil && !os.IsNotExist(e) {
			return e
		}
		for _, entry := range entries {
			if !entry.IsDir() && !strings.HasSuffix(entry.Name(), ".sample") {
				return fmt.Errorf("existing Git hooks at %s; preserve or explicitly move them before installation", filepath.Join(common, "hooks"))
			}
		}
	}
	return nil
}
func (p HookPlan) hookInstall(remove bool) error {
	if !p.hookRepo() {
		fmt.Println("Git hooks: not applicable outside a Git project root")
		return nil
	}
	if !remove {
		if e := p.hookValidate(); e != nil {
			return e
		}
	}
	common, e := p.query("rev-parse", "--path-format=absolute", "--git-common-dir")
	if e != nil {
		return e
	}
	lockPath, e := hookPath(common, "chainman-hooks.lock")
	if e != nil {
		return e
	}
	admin, e := locked(lockPath, true)
	if e != nil {
		return fmt.Errorf("Git hook installation or removal is active: %w", e)
	}
	defer admin.Close()
	target, e := p.hookDirectory()
	if e != nil {
		return e
	}
	if !remove {
		if e = p.hookCheck(); e != nil {
			return e
		}
	}
	if e = os.MkdirAll(target, 0700); e != nil {
		return e
	}
	shared := filepath.Join(common, "config")
	primary := filepath.Join(common, "config.worktree")
	selected := filepath.Join(filepath.Dir(target), "config.worktree")
	configs := []string{primary}
	if selected != primary {
		configs = append(configs, selected)
	}
	siblings, e := filepath.Glob(filepath.Join(common, "worktrees", "*", "config.worktree"))
	if e != nil {
		return e
	}
	for _, s := range siblings {
		if s != selected {
			configs = append(configs, s)
		}
	}
	configs = append(configs, shared)
	if remove {
		configs = []string{selected}
	}
	sorted := append([]string{}, configs...)
	sort.Strings(sorted)
	for _, path := range sorted {
		if _, e = hookPath(filepath.Dir(path), filepath.Base(path)); e != nil {
			return e
		}
		f, e := os.OpenFile(path+".lock", os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0600)
		if e != nil {
			return fmt.Errorf("Git configuration is in use: %s: %w", path, e)
		}
		f.Close()
		defer os.Remove(path + ".lock")
	}
	if !remove {
		if e = p.hookCheck(); e != nil {
			return e
		}
	}
	old := map[string]hookFile{}
	copies := map[string]string{}
	temporary, e := os.MkdirTemp(target, "prepare-")
	if e != nil {
		return e
	}
	defer os.RemoveAll(temporary)
	for i, path := range configs {
		f, e := hookRead(path)
		if e != nil {
			return e
		}
		old[path] = f
		copy := filepath.Join(temporary, fmt.Sprint(i))
		if e = os.WriteFile(copy, f.Body, 0600); e != nil {
			return e
		}
		copies[path] = copy
	}
	replacements := map[string]hookFile{}
	order := []string{}
	put := func(path string, f hookFile) { replacements[path] = f; order = append(order, path) }
	if remove {
		s, e := p.hookSelected()
		if e != nil {
			return e
		}
		if s == "" || hookAbsolute(p.Root, s) != target {
			return fmt.Errorf("selected hook path is not owned by chainman; nothing removed")
		}
		for _, event := range hookEvents {
			path := filepath.Join(target, event)
			f, e := hookRead(path)
			if e != nil {
				return e
			}
			if !f.Exists || !bytes.Equal(f.Body, hookBridge(event)) {
				return fmt.Errorf("modified hook preserved: %s", path)
			}
			old[path] = f
		}
		if !old[selected].Exists {
			return fmt.Errorf("owned worktree hook configuration is missing")
		}
		if _, e = p.query("config", "--file", copies[selected], "--unset", "core.hooksPath"); e != nil {
			return e
		}
	} else {
		enabled, _ := p.query("config", "--local", "--includes", "--type=bool", "--get", "extensions.worktreeConfig")
		if enabled != "true" {
			for _, path := range configs {
				if path == shared || !old[path].Exists {
					continue
				}
				_, e = p.query("config", "--file", path, "--includes", "--get-regexp", `^(core\.(bare|worktree|hookspath)|include.*\.path)$`)
				if !hookAbsent(e) {
					return fmt.Errorf("dormant worktree configuration may change Git identity or hooks: %s", path)
				}
			}
			for _, key := range []string{"core.bare", "core.worktree"} {
				kind := []string{}
				if key == "core.bare" {
					kind = []string{"--type=bool"}
				}
				value, e := p.query(append(append([]string{"config", "--file", copies[shared]}, kind...), "--get", key)...)
				if e != nil && !hookAbsent(e) {
					return e
				}
				effective, x := p.query(append(append([]string{"config", "--local", "--includes"}, kind...), "--get", key)...)
				if x != nil && !hookAbsent(x) {
					return x
				}
				origins, x := p.git(p.Root, "", nil, false, "config", "--local", "--includes", "--show-origin", "--null", "--get-all", key)
				if x != nil && !hookAbsent(x) {
					return x
				}
				fields := bytes.Split(bytes.TrimSuffix(origins, []byte{0}), []byte{0})
				for i := 0; i+1 < len(fields); i += 2 {
					origin := string(fields[i])
					if !strings.HasPrefix(origin, "file:") || hookAbsolute(p.Root, strings.TrimPrefix(origin, "file:")) != shared {
						return fmt.Errorf("shared %s comes from included configuration; configure worktree settings explicitly", key)
					}
				}
				if value != effective {
					return fmt.Errorf("shared %s comes from included configuration", key)
				}
				if e == nil {
					if _, e = p.query("config", "--file", copies[primary], "--replace-all", key, value); e != nil {
						return e
					}
					if _, e = p.query("config", "--file", copies[shared], "--unset-all", key); e != nil {
						return e
					}
				}
			}
			if _, e = p.query("config", "--file", copies[shared], "--replace-all", "extensions.worktreeConfig", "true"); e != nil {
				return e
			}
		}
		setting := target
		if filepath.Dir(target) == filepath.Join(p.Root, ".git") {
			setting, e = filepath.Rel(p.Root, target)
			if e != nil {
				return e
			}
		}
		if _, e = p.query("config", "--file", copies[selected], "--replace-all", "core.hooksPath", setting); e != nil {
			return e
		}
		for _, event := range hookEvents {
			path := filepath.Join(target, event)
			f, e := hookRead(path)
			if e != nil {
				return e
			}
			old[path] = f
			put(path, hookFile{hookBridge(event), 0755, true})
		}
		path := filepath.Join(target, "ownership.json")
		f, e := hookRead(path)
		if e != nil {
			return e
		}
		old[path] = f
		b, _ := json.Marshal(map[string]string{"setting": setting})
		put(path, hookFile{append(b, '\n'), 0600, true})
	}
	// Config activation is last, after complete bridge publication.
	for _, path := range configs {
		b, e := os.ReadFile(copies[path])
		if e != nil {
			return e
		}
		mode := old[path].Mode
		if !old[path].Exists {
			mode = 0600
		}
		if !old[path].Exists && len(b) == 0 {
			continue
		}
		put(path, hookFile{b, mode, true})
	}
	if remove {
		for _, name := range append(append([]string{}, hookEvents...), "ownership.json") {
			path := filepath.Join(target, name)
			f, e := hookRead(path)
			if e != nil {
				return e
			}
			old[path] = f
			put(path, hookFile{})
		}
	}
	published := []string{}
	rollback := func(cause error) error {
		for i := len(published) - 1; i >= 0; i-- {
			if x := hookRestore(published[i], old[published[i]]); x != nil {
				return fmt.Errorf("%v; recovery failed at %s: %w", cause, published[i], x)
			}
		}
		return cause
	}
	for _, path := range order {
		current, e := hookRead(path)
		if e != nil {
			return rollback(e)
		}
		if !hookEqual(current, old[path]) {
			return rollback(fmt.Errorf("hook configuration changed during installation: %s", path))
		}
		if hookEqual(old[path], replacements[path]) {
			continue
		}
		published = append(published, path)
		if e = hookRestore(path, replacements[path]); e != nil {
			return rollback(e)
		}
	}
	if !remove {
		s, e := p.hookStatus()
		if e != nil {
			return rollback(e)
		}
		if s["installed"] != true {
			return rollback(fmt.Errorf("Git did not select installed hook bridges"))
		}
		fmt.Println("Git hooks installed (format staged content; scan outgoing commits)")
	} else {
		fmt.Println("Owned hook bridges removed; other Git configuration preserved")
	}
	return nil
}
