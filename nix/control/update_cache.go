package main

// Update candidates are disposable diagnostics, not recovery archives. The host
// owns collection so container clients never need access to an engine socket.
import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const updateLimit int64 = 12 << 30
const updateAge = 24 * time.Hour
const updateSchema = 2

type updateReceipt struct {
	Schema           int               `json:"schema"`
	Token            string            `json:"token"`
	Touched          time.Time         `json:"touched"`
	Complete         bool              `json:"complete"`
	Engines          []string          `json:"engines,omitempty"`
	EngineIdentities map[string]string `json:"engine_identities,omitempty"`
}

type updateEntry struct {
	Path     string    `json:"path"`
	Bytes    int64     `json:"bytes"`
	Active   bool      `json:"active"`
	Eligible bool      `json:"eligible"`
	Expires  time.Time `json:"expires"`
	Error    string    `json:"error,omitempty"`
	receipt  updateReceipt
}

func updateFile(path string, create bool) (*os.File, error) {
	flags := os.O_RDWR | syscall.O_NOFOLLOW | syscall.O_NONBLOCK
	if create {
		flags |= os.O_CREATE | os.O_EXCL
	}
	f, err := os.OpenFile(path, flags, 0600)
	if err != nil {
		return nil, err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, err
	}
	s := st.Sys().(*syscall.Stat_t)
	if !st.Mode().IsRegular() || int(s.Uid) != os.Geteuid() || s.Nlink != 1 || st.Mode().Perm()&0077 != 0 {
		f.Close()
		return nil, fmt.Errorf("invalid update lease: %s", path)
	}
	return f, nil
}

func updateGate(base string) (string, *os.File, error) {
	pool := filepath.Join(base, "v1")
	if err := private(pool); err != nil {
		return "", nil, err
	}
	f, err := updateFile(filepath.Join(pool, ".gate"), true)
	if os.IsExist(err) {
		f, err = updateFile(filepath.Join(pool, ".gate"), false)
	}
	if err != nil {
		return "", nil, err
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		f.Close()
		return "", nil, err
	}
	return pool, f, nil
}

func updateRead(path string) (updateReceipt, error) {
	var r updateReceipt
	if err := existingPrivate(path); err != nil {
		return r, err
	}
	f, err := updateFile(filepath.Join(path, ".transaction.json"), false)
	if err != nil {
		return r, err
	}
	f.Close()
	if err = readJSON(filepath.Join(path, ".transaction.json"), &r); err != nil {
		return r, err
	}
	if (r.Schema != 1 && r.Schema != updateSchema) || !consentID.MatchString(r.Token) || r.Touched.IsZero() {
		return r, fmt.Errorf("unknown update receipt")
	}
	return r, nil
}

func updateLegacyGuard(path string) string {
	return filepath.Join(path, ".legacy-collector-guard")
}

// A schema-1 writer cannot retain daemon identities or understand new fields.
// Its Engines list can, however, retain a synthetic container witness. Put that
// witness first so an unbound real engine never authorizes legacy collection.
// Called under the pool gate; preserve the lease inode and the old reader's
// format while its original supervisor may still be using them.
func updateProtectLegacy(path string, r updateReceipt) error {
	guard := updateLegacyGuard(path)
	encoded, err := json.Marshal([]string{guard})
	if err != nil {
		return err
	}
	// The frozen schema-1 writer emits compact JSON with Engines last. Accept
	// only its exact, JSON-escaped singleton list as proof of host-only work.
	// Anything else, including later registrations or unreadable/reformatted
	// data, returns a positive witness. This is deliberately not a JSON parser.
	// Shell builtins keep the guard independent of temporary native exports,
	// Nix roots, host language runtimes and executable search paths.
	body := "#!/bin/sh\nreceipt=\nIFS= read -r receipt < " + quote(filepath.Join(path, ".transaction.json")) + " || :\n" +
		"case \"$receipt\" in\n    *" + quote(`,"engines":`+string(encoded)+"}") + ") exit 0 ;;\nesac\n" +
		"printf '%s\\n' chainman-legacy-update-requires-inspection\n"
	f, err := updateFile(guard, false)
	missing := os.IsNotExist(err)
	if err == nil {
		st, statError := f.Stat()
		data, readError := io.ReadAll(io.LimitReader(f, int64(len(body)+1)))
		f.Close()
		if statError != nil || readError != nil || st.Mode().Perm() != 0700 || string(data) != body {
			return fmt.Errorf("invalid legacy update collection guard: %s", guard)
		}
	} else if !missing {
		return err
	}
	engines := []string{guard}
	for _, engine := range r.Engines {
		if engine != guard {
			engines = append(engines, engine)
		}
	}
	if !slices.Equal(r.Engines, engines) {
		r.Engines = engines
		if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
			return err
		}
	}
	// Publish the reference first: interruption before the atomic script install
	// leaves a missing engine, which makes old collectors refuse deletion too.
	if missing {
		return hookWrite(guard, []byte(body), 0700)
	}
	return nil
}

func updateContainers(r updateReceipt, path string) (bool, error) {
	for _, engine := range r.Engines {
		if r.Schema == 1 && engine == updateLegacyGuard(path) {
			continue // Synthetic witness; all real engines still require identity.
		}
		if !filepath.IsAbs(engine) || r.EngineIdentities[engine] == "" {
			return false, fmt.Errorf("update engine has no recorded daemon identity: %s", engine)
		}
		owner := &Container{Engine: engine, EngineIdentity: r.EngineIdentities[engine], Name: path}
		if err := checkEngine(owner); err != nil {
			return false, err
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		out, err := queryCommand(ctx, engine, "ps", "--all", "--quiet", "--filter", "label=dev.chainman.update="+r.Token).Output()
		cancel()
		// An unavailable engine cannot prove a workspace is idle. Even stopped
		// containers retain mounts, so conservatively keep their candidates too.
		if err != nil {
			return false, fmt.Errorf("cannot inspect update containers: %w", err)
		}
		if strings.TrimSpace(string(out)) != "" {
			return true, nil
		}
		// Frozen candidate runtimes can predate the label protocol. Check their
		// actual mounts as well; never infer idleness just from a missing label.
		ctx, cancel = context.WithTimeout(context.Background(), 10*time.Second)
		out, err = queryCommand(ctx, engine, "ps", "--all", "--quiet").Output()
		cancel()
		if err != nil {
			return false, fmt.Errorf("cannot inventory update mounts: %w", err)
		}
		ids := strings.Fields(string(out))
		if len(ids) == 0 {
			if err = checkEngine(owner); err != nil {
				return false, err
			}
			continue
		}
		ctx, cancel = context.WithTimeout(context.Background(), 10*time.Second)
		out, err = queryCommand(ctx, engine, append([]string{"inspect"}, ids...)...).Output()
		cancel()
		inspectionError := err
		var containers []struct{ Mounts []struct{ Source string } }
		if err = json.Unmarshal(out, &containers); err != nil {
			return false, err
		}
		for _, container := range containers {
			for _, mount := range container.Mounts {
				source := filepath.Clean(mount.Source)
				if filepath.IsAbs(source) && (source == "/" || path == source || strings.HasPrefix(path, source+"/") || strings.HasPrefix(source, path+"/")) {
					return true, nil
				}
			}
		}
		// Docker can return valid partial JSON and fail when an unrelated
		// ephemeral container disappeared. A witnessed mount proves activity,
		// but incomplete negative evidence must never authorize collection.
		if inspectionError != nil {
			return false, fmt.Errorf("cannot inspect update mounts: %w", inspectionError)
		}
		// Reject a context change during inventory as well as before it.
		if err = checkEngine(owner); err != nil {
			return false, err
		}
	}
	return false, nil
}

func updateSize(path string) (int64, error) {
	var size int64
	err := filepath.WalkDir(path, func(_ string, d fs.DirEntry, err error) error {
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		st, err := d.Info()
		if os.IsNotExist(err) {
			return nil
		}
		if err == nil {
			size += st.Size()
		}
		return err
	})
	return size, err
}

func updateRemove(path string) error {
	// Nix exports and package caches contain read-only directories. Change only
	// owned directories, never symlink targets or possibly hard-linked files.
	err := filepath.WalkDir(path, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			return nil
		}
		st, err := d.Info()
		if err != nil {
			return err
		}
		if int(st.Sys().(*syscall.Stat_t).Uid) != os.Geteuid() {
			return fmt.Errorf("foreign update directory: %s", p)
		}
		return os.Chmod(p, st.Mode().Perm()|0700)
	})
	if err != nil {
		return err
	}
	return os.RemoveAll(path)
}

// The admission gate stays held through inventory, lease acquisition and removal.
// Unknown and legacy entries never become eligible just because they are old.
func updateCollect(base string, remove, all bool, now time.Time, limit int64) ([]updateEntry, []string, error) {
	pool, gate, err := updateGate(base)
	if err != nil {
		return nil, nil, err
	}
	defer gate.Close()
	children, err := os.ReadDir(pool)
	if err != nil {
		return nil, nil, err
	}
	entries := []updateEntry{}
	removed := []string{}
	var failures []error
	protect := func(entry *updateEntry, err error) {
		entry.Error = err.Error()
		failures = append(failures, fmt.Errorf("%s: %w", entry.Path, err))
	}
	var idleBytes int64
	for _, child := range children {
		if !strings.HasPrefix(child.Name(), "candidate.") || !child.IsDir() {
			continue
		}
		path := filepath.Join(pool, child.Name())
		r, err := updateRead(path)
		if err != nil {
			continue
		}
		lease, err := updateFile(filepath.Join(path, ".lease"), false)
		if err != nil {
			continue
		}
		entry := updateEntry{Path: path, receipt: r, Expires: r.Touched.Add(updateAge)}
		if remove && r.Schema == 1 {
			if err = updateProtectLegacy(path, r); err != nil {
				protect(&entry, err)
			}
		}
		err = syscall.Flock(int(lease.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if errors.Is(err, syscall.EWOULDBLOCK) {
			entry.Active = true
		} else if err != nil {
			protect(&entry, err)
		}
		if !entry.Active && entry.Error == "" {
			entry.Active, err = updateContainers(r, path)
			if err != nil {
				protect(&entry, err)
			}
		}
		// Admission remains gated, so no new owner can acquire an idle entry.
		// Do not keep one descriptor per candidate: many tiny failures must not
		// exhaust the descriptor limit and prevent their own collection.
		lease.Close()
		entry.Bytes, err = updateSize(path)
		if err != nil {
			protect(&entry, err)
		}
		if !entry.Active && entry.Error == "" {
			idleBytes += entry.Bytes
		}
		entries = append(entries, entry)
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].receipt.Touched.Before(entries[j].receipt.Touched) })
	for i := range entries {
		e := &entries[i]
		e.Eligible = !e.Active && e.Error == "" && (all || e.receipt.Complete || !now.Before(e.Expires) || idleBytes > limit)
		if !e.Eligible {
			continue
		}
		if remove {
			if err = updateRemove(e.Path); err != nil {
				protect(e, err)
				e.Eligible = false
				continue
			}
			removed = append(removed, e.Path)
		}
		idleBytes -= e.Bytes
	}
	return entries, removed, errors.Join(failures...)
}

func updateStart(base, resume string) (string, *os.File, updateReceipt, error) {
	var r updateReceipt
	pool, gate, err := updateGate(base)
	if err != nil {
		return "", nil, r, err
	}
	defer gate.Close()
	path := resume
	if path == "-" {
		path, err = os.MkdirTemp(pool, "candidate.")
		if err != nil {
			return "", nil, r, err
		}
		r = updateReceipt{Schema: updateSchema, Token: token(), Touched: time.Now()}
		if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
			return "", nil, r, err
		}
	} else {
		if filepath.Dir(path) != pool || !strings.HasPrefix(filepath.Base(path), "candidate.") {
			return "", nil, r, fmt.Errorf("resume must select a retained v1 transaction")
		}
		r, err = updateRead(path)
		if err != nil {
			return "", nil, r, fmt.Errorf("candidate unavailable (possibly expired): %w", err)
		}
	}
	f, err := updateFile(filepath.Join(path, ".lease"), resume == "-")
	if err != nil {
		return "", nil, r, err
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return "", nil, r, fmt.Errorf("candidate is active: %w", err)
	}
	active, err := updateContainers(r, path)
	if err != nil || active {
		f.Close()
		return "", nil, r, fmt.Errorf("candidate has containers or cannot be inspected: %v", err)
	}
	// Only the supervisor taking exclusive admission may migrate a receipt.
	// Its predecessor and all surviving children have released ownership, and
	// the new engine helper can now bind identities that old collectors ignore.
	if r.Schema != updateSchema {
		r.Engines = slices.DeleteFunc(r.Engines, func(engine string) bool { return engine == updateLegacyGuard(path) })
		r.Schema = updateSchema
		if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
			f.Close()
			return "", nil, r, err
		}
	}
	// Exclusive admission above prevents concurrent resumes. Active owners use
	// shared locks so detached service owners can acquire independent witnesses.
	// Conversion is protected by the same gate as collection and child admission.
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_SH); err != nil {
		f.Close()
		return "", nil, r, err
	}
	return path, f, r, nil
}

// Process Compose and Watchexec may close inherited descriptors. Reacquire a
// shared candidate lease at their native execution boundaries, under the pool
// gate, and keep it in both the native owner and its launched child.
func forwardUpdateLease(cmd *exec.Cmd) error {
	path := ""
	for _, value := range cmd.Environ() {
		if strings.HasPrefix(value, "CHAINMAN_UPDATE_TRANSACTION=") {
			path = strings.TrimPrefix(value, "CHAINMAN_UPDATE_TRANSACTION=")
		}
	}
	env := cmd.Environ()
	cmd.Env = nil
	for _, value := range env {
		if !strings.HasPrefix(value, "CHAINMAN_UPDATE_LEASE_FD=") {
			cmd.Env = append(cmd.Env, value)
		}
	}
	if path == "" {
		return nil
	}
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || filepath.Base(filepath.Dir(path)) != "v1" || !strings.HasPrefix(filepath.Base(path), "candidate.") {
		return fmt.Errorf("invalid update transaction path")
	}
	_, gate, err := updateGate(filepath.Dir(filepath.Dir(path)))
	if err != nil {
		return err
	}
	defer gate.Close()
	r, err := updateRead(path)
	if err != nil {
		return err
	}
	lease, err := updateFile(filepath.Join(path, ".lease"), false)
	if err != nil {
		return err
	}
	// A frozen launcher from schema 1 holds an exclusive lease. Only a verified
	// inherited description of this exact inode may convert that ownership to
	// shared mode. Closed/reused backend descriptors must never be unlocked.
	if r.Schema == 1 {
		fd, parseError := strconv.Atoi(os.Getenv("CHAINMAN_UPDATE_LEASE_FD"))
		var inherited, current syscall.Stat_t
		if parseError == nil && fd >= 3 && syscall.Fstat(fd, &inherited) == nil && syscall.Fstat(int(lease.Fd()), &current) == nil && inherited.Dev == current.Dev && inherited.Ino == current.Ino {
			if err = syscall.Flock(fd, syscall.LOCK_SH|syscall.LOCK_NB); err != nil {
				lease.Close()
				return err
			}
		}
	}
	if err = syscall.Flock(int(lease.Fd()), syscall.LOCK_SH|syscall.LOCK_NB); err != nil {
		lease.Close()
		return fmt.Errorf("cannot acquire update lifetime lease: %w", err)
	}
	// Sharing lifetime ownership does not transfer receipt ownership. A frozen
	// supervisor and CHAINMAN_UPDATE_HELPER may still need to register engines,
	// finalize or resume this operation using their original schema.
	if r.Schema == 1 {
		if err = updateProtectLegacy(path, r); err != nil {
			lease.Close()
			return err
		}
	}
	cmd.Env = append(cmd.Env, "CHAINMAN_UPDATE_LEASE_FD="+strconv.Itoa(3+len(cmd.ExtraFiles)))
	cmd.ExtraFiles = append(cmd.ExtraFiles, lease)
	return nil
}

func updateRun(base, resume, action string, argv []string) int {
	if len(argv) == 0 {
		return 2
	}
	if _, _, err := updateCollect(base, true, false, time.Now(), updateLimit); err != nil {
		fmt.Fprintln(os.Stderr, "Chainman: update cache maintenance:", err)
	}
	path, lease, _, err := updateStart(base, resume)
	if err != nil {
		return exitCode(err)
	}
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Env = os.Environ()
	if err = forwardLeases(cmd); err != nil {
		lease.Close()
		return exitCode(err)
	}
	fd := 3 + len(cmd.ExtraFiles)
	cmd.ExtraFiles = append(cmd.ExtraFiles, lease)
	cmd.Env = append(cmd.Env, "CHAINMAN_UPDATE_TRANSACTION="+path, "CHAINMAN_UPDATE_LEASE_FD="+strconv.Itoa(fd))
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	result := exitCode(hookRun(cmd, 0))
	// Children inherit the open description. Closing, rather than LOCK_UN,
	// leaves their lifetime witness intact after the supervisor exits.
	_, gate, maintenance := updateGate(base)
	if maintenance == nil {
		r, e := updateRead(path)
		if e == nil {
			r.Touched, r.Complete = time.Now(), result == 0
			e = atomic(filepath.Join(path, ".transaction.json"), r)
		}
		maintenance = e
		gate.Close()
	}
	closeForwarded(cmd)
	if maintenance != nil {
		fmt.Fprintln(os.Stderr, "Chainman: update receipt:", maintenance)
	}
	if _, _, err = updateCollect(base, true, false, time.Now(), updateLimit); err != nil {
		fmt.Fprintln(os.Stderr, "Chainman: update cache maintenance:", err)
	}
	if result != 0 {
		if _, err = os.Stat(path); err == nil {
			fmt.Fprintf(os.Stderr, "Chainman: temporary candidate at %s/candidate; disposable, not a backup. Retention: up to 24 hours / 12 GiB across idle candidates. Resume is best-effort: just %s %s\n", path, action, quote("resume="+path))
		} else {
			fmt.Fprintln(os.Stderr, "Chainman: temporary candidate discarded by retention policy; no resume is available.")
		}
	}
	return result
}

func updateCacheAction(args []string) int {
	if len(args) < 2 {
		return 2
	}
	action, base := args[0], args[1]
	switch action {
	case "run":
		if len(args) < 5 {
			return 2
		}
		return updateRun(base, args[2], args[3], args[4:])
	case "engine":
		if len(args) != 4 {
			return 2
		}
		pool, gate, err := updateGate(base)
		if err != nil {
			return exitCode(err)
		}
		defer gate.Close()
		path, engine := args[2], args[3]
		if filepath.Dir(path) != pool || !filepath.IsAbs(engine) {
			return 2
		}
		r, err := updateRead(path)
		if err != nil {
			return exitCode(err)
		}
		if r.Schema != updateSchema {
			// Keeping new identity metadata in a schema-1 receipt is unsafe: its
			// old supervisor would discard it and its collector would ignore it.
			// Registration must use the helper belonging to that supervisor;
			// only updateStart can migrate an idle transaction to this runtime.
			return exitCode(fmt.Errorf("legacy update receipt: use its original update helper, or resume with the updated runtime"))
		}
		found := false
		for _, old := range r.Engines {
			if old == engine {
				found = true
			}
		}
		if !found {
			identity, err := engineIdentity(engine)
			if err != nil {
				return exitCode(err)
			}
			if r.EngineIdentities == nil {
				r.EngineIdentities = map[string]string{}
			}
			r.EngineIdentities[engine] = identity
			r.Engines = append(r.Engines, engine)
			if err = atomic(filepath.Join(path, ".transaction.json"), r); err != nil {
				return exitCode(err)
			}
		} else if r.EngineIdentities[engine] == "" {
			return exitCode(fmt.Errorf("update engine has no recorded daemon identity; cannot adopt its current context"))
		} else if err = checkEngine(&Container{Engine: engine, EngineIdentity: r.EngineIdentities[engine], Name: path}); err != nil {
			return exitCode(err)
		}
		fmt.Println(r.Token)
		return 0
	case "status", "prune":
		if len(args) > 3 || (len(args) == 3 && (action != "prune" || args[2] != "--all")) {
			return 2
		}
		entries, removed, err := updateCollect(base, action == "prune", len(args) == 3, time.Now(), updateLimit)
		outputError := json.NewEncoder(os.Stdout).Encode(struct {
			Transactions []updateEntry `json:"transactions"`
			Removed      []string      `json:"removed"`
		}{entries, removed})
		return exitCode(errors.Join(err, outputError))
	}
	return 2
}
