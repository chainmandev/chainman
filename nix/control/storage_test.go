package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func storageFixture(t *testing.T, base, name string) Plan {
	t.Helper()
	if err := os.Chmod(base, 0700); err != nil {
		t.Fatal(err)
	}
	state := filepath.Join(base, strings.Repeat(name, 24))
	if err := private(filepath.Join(state, "assets")); err != nil {
		t.Fatal(err)
	}
	gate, err := locked(filepath.Join(state, "gate"), false)
	if err != nil {
		t.Fatal(err)
	}
	gate.Close()
	p := Plan{Schema: 1, State: state, Root: base, Services: map[string]Service{}}
	if err := atomic(filepath.Join(state, "plan.json"), p); err != nil {
		t.Fatal(err)
	}
	return p
}

func storageAsset(t *testing.T, p Plan, data string) string {
	t.Helper()
	digest := sha256.Sum256([]byte(data))
	path, err := installServiceAsset(filepath.Join(p.State, "assets"), hex.EncodeToString(digest[:]), []byte(data))
	if err != nil {
		t.Fatal(err)
	}
	return path
}

func TestServiceStorageDeduplicatesAndRetainsCurrentAsset(t *testing.T) {
	base := physicalTempDir(t)
	a, b := storageFixture(t, base, "a"), storageFixture(t, base, "b")
	first, second := storageAsset(t, a, "binary"), storageAsset(t, b, "binary")
	left, _ := os.Stat(first)
	right, _ := os.Stat(second)
	if !os.SameFile(left, right) {
		t.Fatal("identical assets were copied")
	}
	a.Backend = first
	if err := atomic(filepath.Join(a.State, "plan.json"), a); err != nil {
		t.Fatal(err)
	}
	rows, removed, err := serviceStorage(base, true, true, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) == 0 || len(removed) != 1 || removed[0] != second {
		t.Fatalf("unexpected collection: %+v %v", rows, removed)
	}
	if _, err := os.Stat(first); err != nil {
		t.Fatal("current backend was removed", err)
	}
}

func TestServiceStorageLinkFallbackPreservesExistingCopyAge(t *testing.T) {
	base := physicalTempDir(t)
	p := storageFixture(t, base, "a")
	data := []byte("binary")
	digest := sha256.Sum256(data)
	assets := filepath.Join(p.State, "assets")
	noLinks := func(string, string) error { return syscall.EXDEV }
	path, err := installServiceAssetLink(assets, hex.EncodeToString(digest[:]), data, noLinks)
	if err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-storageAge - time.Hour)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatal(err)
	}
	before, _ := os.Stat(path)
	if _, err := installServiceAssetLink(assets, hex.EncodeToString(digest[:]), data, noLinks); err != nil {
		t.Fatal(err)
	}
	after, _ := os.Stat(path)
	if !os.SameFile(before, after) || !before.ModTime().Equal(after.ModTime()) {
		t.Fatal("fallback refreshed an existing executable's retention age")
	}
}

func TestServiceStoragePreservesRecoveryAndRejectsAssetEscape(t *testing.T) {
	base := physicalTempDir(t)
	p := storageFixture(t, base, "a")
	asset := storageAsset(t, p, "recoverable")
	if err := atomic(filepath.Join(p.State, "task.owner.json"), Owner{}); err != nil {
		t.Fatal(err)
	}
	rows, removed, err := serviceStorage(base, true, true, time.Now())
	if err != nil || len(removed) != 0 || !rows[0].Active {
		t.Fatalf("recovery was collected: %v %v", removed, err)
	}
	if err := os.Remove(filepath.Join(p.State, "task.owner.json")); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(base, "outside")
	if err := os.Rename(filepath.Join(p.State, "assets"), outside); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(p.State, "assets")); err != nil {
		t.Fatal(err)
	}
	_, removed, err = serviceStorage(base, true, true, time.Now())
	if err == nil || len(removed) != 0 {
		t.Fatal("asset symlink was accepted")
	}
	if _, err := os.Stat(filepath.Join(outside, filepath.Base(asset))); err != nil {
		t.Fatal(err)
	}
}

func TestServiceStorageRetiresOrphanWithoutReplacingGate(t *testing.T) {
	base := physicalTempDir(t)
	p := storageFixture(t, base, "a")
	p.Root = filepath.Join(base, "missing")
	p.Backend = storageAsset(t, p, "binary")
	if err := atomic(filepath.Join(p.State, "plan.json"), p); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-storageAge - time.Hour)
	if err := os.Chtimes(filepath.Join(p.State, "plan.json"), old, old); err != nil {
		t.Fatal(err)
	}
	gate, _ := os.Stat(filepath.Join(p.State, "gate"))
	_, _, err := serviceStorage(base, true, false, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	after, _ := os.Stat(filepath.Join(p.State, "gate"))
	if !os.SameFile(gate, after) {
		t.Fatal("admission inode was replaced")
	}
	if _, err := os.Stat(filepath.Join(p.State, "plan.json")); !os.IsNotExist(err) {
		t.Fatal("abandoned plan was retained")
	}
	preserved := storageFixture(t, base, "b")
	preserved.Root = filepath.Join(base, "missing")
	preserved.Volumes = []Volume{{}}
	preserved.Backend = storageAsset(t, preserved, "preserved binary")
	if err := atomic(filepath.Join(preserved.State, "plan.json"), preserved); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(filepath.Join(preserved.State, "plan.json"), old, old); err != nil {
		t.Fatal(err)
	}
	if _, _, err := serviceStorage(base, true, true, time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(preserved.Backend); err != nil {
		t.Fatal("volume recovery backend removed", err)
	}
}

func TestStorageLeaseForwardingUsesIndependentDescription(t *testing.T) {
	pool := filepath.Join(physicalTempDir(t), storagePool)
	entry := filepath.Join(pool, "downloads")
	if err := private(entry); err != nil {
		t.Fatal(err)
	}
	gate, err := updateFile(filepath.Join(pool, ".gate"), true)
	if err != nil {
		t.Fatal(err)
	}
	gate.Close()
	if err = atomic(filepath.Join(entry, ".receipt.json"), storageReceipt{Schema: 1, Kind: "downloads", Touched: float64(time.Now().Unix()), Epoch: strings.Repeat("a", 32)}); err != nil {
		t.Fatal(err)
	}
	lease, err := updateFile(filepath.Join(entry, ".lease"), true)
	if err != nil {
		t.Fatal(err)
	}
	if err = syscall.Flock(int(lease.Fd()), syscall.LOCK_SH); err != nil {
		t.Fatal(err)
	}
	paths, _ := json.Marshal([]string{entry})
	cmd := exec.Command("true")
	cmd.Env = []string{"CHAINMAN_STORAGE_PATHS=" + string(paths), "CHAINMAN_STORAGE_FDS=[99999]"}
	if err = forwardStorageLeases(cmd); err != nil {
		t.Fatal(err)
	}
	lease.Close()
	probe, err := updateFile(filepath.Join(entry, ".lease"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer probe.Close()
	if err = syscall.Flock(int(probe.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err == nil {
		t.Fatal("native owner did not retain independent lease")
	}
	closeForwarded(cmd)
	if err = syscall.Flock(int(probe.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
}
