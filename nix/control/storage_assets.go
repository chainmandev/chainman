package main

// Executables are disposable; service plans, volume identities and recovery
// receipts are not. Keep scope-local executable paths stable across versions.
import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
)

const storageAge = 30 * 24 * time.Hour

var assetDigest = regexp.MustCompile(`^[0-9a-f]{64}$`)
var serviceScopeName = regexp.MustCompile(`^[0-9a-f]{24}$`)

type storageEntry struct {
	Path     string `json:"path"`
	Bytes    int64  `json:"bytes"`
	Active   bool   `json:"active"`
	Eligible bool   `json:"eligible"`
	Reason   string `json:"reason"`
	Error    string `json:"error,omitempty"`
}

func verifyAsset(path, digest string) error {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return err
	}
	if !st.Mode().IsRegular() || st.Size() > 100<<20 || st.Mode().Perm() != 0700 || int(st.Sys().(*syscall.Stat_t).Uid) != os.Geteuid() {
		return fmt.Errorf("invalid executable asset: %s", path)
	}
	hash := sha256.New()
	if _, err = io.Copy(hash, io.LimitReader(f, 100<<20+1)); err != nil {
		return err
	}
	if hex.EncodeToString(hash.Sum(nil)) != digest {
		return fmt.Errorf("cached controller executable failed integrity check: %s", path)
	}
	return nil
}

// Caller holds the scope gate. Every writer orders scope before asset-pool gate.
func installServiceAsset(assets, digest string, data []byte) (string, error) {
	return installServiceAssetLink(assets, digest, data, os.Link)
}

func installServiceAssetLink(assets, digest string, data []byte, link func(string, string) error) (string, error) {
	destination := filepath.Join(assets, digest)
	destinationErr := verifyAsset(destination, digest)
	if destinationErr != nil && !os.IsNotExist(destinationErr) {
		return "", destinationErr
	}
	pool := filepath.Join(filepath.Dir(filepath.Dir(assets)), ".assets-v1")
	if err := existingPrivate(filepath.Dir(pool)); err != nil {
		return "", err
	}
	if err := private(pool); err != nil {
		return "", err
	}
	gate, err := locked(filepath.Join(pool, ".gate"), false)
	if err != nil {
		return "", err
	}
	defer gate.Close()
	shared := filepath.Join(pool, digest)
	if err = verifyAsset(shared, digest); os.IsNotExist(err) {
		err = hookWrite(shared, data, 0700)
	}
	if err != nil {
		return "", err
	}
	if a, err := os.Stat(shared); err == nil {
		if b, err := os.Stat(destination); err == nil && os.SameFile(a, b) {
			return destination, nil
		}
	}
	temporary, err := os.CreateTemp(assets, ".asset-link-")
	if err != nil {
		return "", err
	}
	name := temporary.Name()
	temporary.Close()
	defer os.Remove(name)
	if err = os.Remove(name); err != nil {
		return "", err
	}
	if err = link(shared, name); errors.Is(err, syscall.EXDEV) || errors.Is(err, syscall.EOPNOTSUPP) {
		// Unusual filesystems may not support hardlinks. Preserve the verified
		// scope-local copy; collection still bounds old executable generations.
		// Rewriting it would refresh its age on every maintenance pass.
		if destinationErr == nil {
			return destination, nil
		}
		err = hookWrite(name, data, 0700)
	}
	if err == nil {
		err = os.Rename(name, destination)
	}
	return destination, err
}

// Read-only ownership inspection. A stopped container still has recovery state.
func storageScopeActive(p Plan) (bool, error) {
	if controller(p).alive() {
		return true, nil
	}
	entries, err := os.ReadDir(p.State)
	if err != nil {
		return false, err
	}
	for _, entry := range entries {
		path := filepath.Join(p.State, entry.Name())
		if strings.HasSuffix(entry.Name(), ".lease") {
			f, err := updateFile(path, false)
			if err != nil {
				return false, err
			}
			err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
			f.Close()
			if errors.Is(err, syscall.EWOULDBLOCK) {
				return true, nil
			}
			if err != nil {
				return false, err
			}
			// Keep receipts until normal service recovery has resolved them.
			// Missing processes alone do not discharge persistent or parent leases.
			return true, nil
		}
		if strings.HasSuffix(entry.Name(), ".owner.json") || strings.HasSuffix(entry.Name(), ".watcher.json") || entry.Name() == "controller.json" {
			return true, nil
		}
	}
	for _, service := range p.Services {
		if service.Container != nil {
			info, err := inspectContainer(service.Container)
			if err != nil || info != nil {
				return info != nil, err
			}
		}
	}
	if p.TaskContainer != nil {
		info, err := inspectContainer(p.TaskContainer)
		if err != nil || info != nil {
			return info != nil, err
		}
	}
	return false, nil
}

func storageAssetReferences(state string) (map[string]bool, error) {
	refs := map[string]bool{}
	entries, err := os.ReadDir(state)
	if err != nil {
		return nil, err
	}
	var visit func(any)
	visit = func(value any) {
		switch value := value.(type) {
		case string:
			if filepath.Dir(value) == filepath.Join(state, "assets") && assetDigest.MatchString(filepath.Base(value)) {
				refs[filepath.Base(value)] = true
			}
		case []any:
			for _, item := range value {
				visit(item)
			}
		case map[string]any:
			for _, item := range value {
				visit(item)
			}
		}
	}
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		var value any
		if err := readJSON(filepath.Join(state, entry.Name()), &value); err != nil {
			return nil, err
		}
		visit(value)
	}
	return refs, nil
}

func storageScope(p Plan, apply, all bool, now time.Time) (row storageEntry, removed []string) {
	row.Path = p.State
	row.Reason = "current service references"
	fail := func(err error) { row.Error = err.Error(); row.Eligible = false; row.Reason = "inspection failed" }
	if err := existingPrivate(p.State); err != nil {
		fail(err)
		return
	}
	gate, err := updateFile(filepath.Join(p.State, "gate"), false)
	if err == nil {
		mode := syscall.LOCK_SH
		if apply {
			mode = syscall.LOCK_EX
		}
		err = syscall.Flock(int(gate.Fd()), mode|syscall.LOCK_NB)
		if err != nil {
			gate.Close()
		}
	}
	if errors.Is(err, syscall.EWOULDBLOCK) {
		row.Active = true
		row.Reason = "scope admission active"
		return
	}
	if err != nil {
		fail(err)
		return
	}
	defer gate.Close()
	if err = readJSON(filepath.Join(p.State, "plan.json"), &p); err != nil {
		fail(err)
		return
	}
	if p.State != row.Path || !filepath.IsAbs(p.Root) {
		fail(fmt.Errorf("invalid saved service scope"))
		return
	}
	row.Active, err = storageScopeActive(p)
	if err != nil {
		fail(err)
		return
	}
	if row.Active {
		row.Reason = "ownership or recovery receipt"
		return
	}
	refs, err := storageAssetReferences(p.State)
	if err != nil {
		fail(err)
		return
	}
	assets := filepath.Join(p.State, "assets")
	if err := existingPrivate(assets); os.IsNotExist(err) {
		return
	} else if err != nil {
		fail(err)
		return
	}
	entries, err := os.ReadDir(assets)
	if os.IsNotExist(err) {
		return
	}
	if err != nil {
		fail(err)
		return
	}
	// A missing worktree is insufficient: saved volume/resource/bridge identities
	// remain useful for explicit recovery even after the source has disappeared.
	_, rootError := os.Stat(p.Root)
	planInfo, err := os.Stat(filepath.Join(p.State, "plan.json"))
	if err != nil {
		fail(err)
		return
	}
	abandoned := os.IsNotExist(rootError) && len(p.Volumes) == 0 && p.Bridge == nil && p.TaskContainer == nil && len(p.Resources) == 0 && now.Sub(planInfo.ModTime()) >= storageAge
	if abandoned {
		refs = map[string]bool{}
	}
	for _, entry := range entries {
		if !assetDigest.MatchString(entry.Name()) {
			continue
		}
		path := filepath.Join(assets, entry.Name())
		info, err := entry.Info()
		if err != nil {
			fail(err)
			continue
		}
		row.Bytes += info.Size()
		if err = verifyAsset(path, entry.Name()); err != nil {
			fail(err)
			continue
		}
		eligible := !refs[entry.Name()] && (all || abandoned || now.Sub(info.ModTime()) >= storageAge)
		if eligible {
			row.Eligible = true
			row.Reason = "unreferenced executable generations"
			if apply {
				if err = os.Remove(path); err != nil {
					fail(err)
				} else {
					removed = append(removed, path)
				}
			}
		} else if apply {
			data, err := os.ReadFile(path)
			if err == nil {
				_, err = installServiceAsset(assets, entry.Name(), data)
			}
			if err != nil {
				fail(err)
			}
		}
	}
	if apply && abandoned && row.Error == "" {
		// Keep admission lock inodes stable for an older client waiting to enter.
		// Retire only the obsolete plan; unknown files and recovery records stay.
		path := filepath.Join(p.State, "plan.json")
		if err = os.Remove(path); err != nil {
			fail(err)
		} else {
			removed = append(removed, path)
		}
	}
	return
}

func serviceStorage(base string, apply, all bool, now time.Time) ([]storageEntry, []string, error) {
	rows := []storageEntry{}
	removed := []string{}
	if err := existingPrivate(base); os.IsNotExist(err) {
		return rows, removed, nil
	} else if err != nil {
		return rows, removed, err
	}
	entries, err := os.ReadDir(base)
	if err != nil {
		return rows, removed, err
	}
	var failures []error
	for _, entry := range entries {
		if !serviceScopeName.MatchString(entry.Name()) {
			continue
		}
		state := filepath.Join(base, entry.Name())
		if _, err := os.Lstat(filepath.Join(state, "plan.json")); os.IsNotExist(err) {
			continue
		}
		row, paths := storageScope(Plan{State: state}, apply, all, now)
		rows = append(rows, row)
		removed = append(removed, paths...)
		if row.Error != "" {
			failures = append(failures, fmt.Errorf("%s: %s", state, row.Error))
		}
	}
	pool := filepath.Join(base, ".assets-v1")
	if err := existingPrivate(pool); os.IsNotExist(err) {
		return rows, removed, errors.Join(failures...)
	} else if err != nil {
		return rows, removed, errors.Join(append(failures, err)...)
	}
	var gate *os.File
	if apply {
		gate, err = locked(filepath.Join(pool, ".gate"), false)
	} else {
		gate, err = observeLock(filepath.Join(pool, ".gate"))
	}
	if err != nil {
		return rows, removed, errors.Join(append(failures, err)...)
	}
	defer gate.Close()
	files, err := os.ReadDir(pool)
	if err != nil {
		return rows, removed, errors.Join(append(failures, err)...)
	}
	for _, file := range files {
		if !assetDigest.MatchString(file.Name()) {
			continue
		}
		path := filepath.Join(pool, file.Name())
		row := storageEntry{Path: path, Reason: "referenced scope asset"}
		info, err := file.Info()
		if err == nil {
			err = verifyAsset(path, file.Name())
		}
		if err == nil {
			row.Bytes = info.Size()
			row.Eligible = info.Sys().(*syscall.Stat_t).Nlink == 1 && (all || now.Sub(info.ModTime()) >= storageAge)
			if row.Eligible {
				row.Reason = "unreferenced shared executable"
			}
			if apply && row.Eligible {
				err = os.Remove(path)
				if err == nil {
					removed = append(removed, path)
				}
			}
		}
		if err != nil {
			row.Error = err.Error()
			failures = append(failures, err)
		}
		rows = append(rows, row)
	}
	return rows, removed, errors.Join(failures...)
}

func serviceStorageAction(args []string) int {
	if len(args) < 2 || len(args) > 3 || (args[0] != "status" && args[0] != "prune") || (len(args) == 3 && (args[0] != "prune" || args[2] != "--all")) {
		return 2
	}
	rows, removed, err := serviceStorage(args[1], args[0] == "prune", len(args) == 3, time.Now())
	if e := json.NewEncoder(os.Stdout).Encode(map[string]any{"entries": rows, "removed": removed}); e != nil {
		return exitCode(e)
	}
	return exitCode(err)
}

// Automatic maintenance is bounded to one scan per day per host cache domain.
// No timer or global project scan runs in the background.
func serviceStorageMaintenance(base string) {
	if existingPrivate(base) != nil {
		return
	}
	pool := filepath.Join(base, ".assets-v1")
	if private(pool) != nil {
		return
	}
	gate, err := locked(filepath.Join(pool, ".maintenance.lock"), true)
	if err != nil {
		return
	}
	defer gate.Close()
	stamp := filepath.Join(pool, ".maintenance.json")
	var previous time.Time
	if readJSON(stamp, &previous) == nil && time.Since(previous) < 24*time.Hour {
		return
	}
	_, _, err = serviceStorage(base, true, false, time.Now())
	if err != nil {
		fmt.Fprintln(os.Stderr, "Chainman: service storage maintenance:", err)
	}
	if err = atomic(stamp, time.Now()); err != nil {
		fmt.Fprintln(os.Stderr, "Chainman: service storage receipt:", err)
	}
}
