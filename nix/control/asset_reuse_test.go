package main

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestServiceLessScopeRetainsNativeRecoveryWithoutBackend(t *testing.T) {
	for _, emptyMap := range []bool{false, true} {
		t.Run(map[bool]string{false: "nil", true: "empty"}[emptyMap], func(t *testing.T) {
			base := physicalTempDir(t)
			if err := os.Chmod(base, 0700); err != nil {
				t.Fatal(err)
			}
			root := filepath.Join(base, "project")
			if err := os.Mkdir(root, 0700); err != nil {
				t.Fatal(err)
			}
			p := Plan{Schema: 1, Root: root, State: filepath.Join(base, "state"), Backend: filepath.Join(base, "absent-backend"), Watcher: filepath.Join(base, "absent-watcher"), Fingerprint: "empty-scope"}
			if emptyMap {
				p.Services = map[string]Service{}
			}
			startup := &startupGuard{signals: make(chan os.Signal, 1), stops: map[string]string{}}
			lease, _, err := acquire(p, false, nil, startup)
			if err != nil {
				t.Fatal("service-less admission tried to read an absent backend or watcher", err)
			}
			lease.Close()
			self, err := os.Executable()
			if err != nil {
				t.Fatal(err)
			}
			body, err := os.ReadFile(self)
			if err != nil {
				t.Fatal(err)
			}
			digest := sha256.Sum256(body)
			assets := filepath.Join(p.State, "assets")
			cached := filepath.Join(assets, hex.EncodeToString(digest[:]))
			entries, err := os.ReadDir(assets)
			if err != nil || len(entries) != 1 || entries[0].Name() != filepath.Base(cached) {
				t.Fatalf("expected only the native recovery executable: %v %v", entries, err)
			}
			if err := verifyAsset(cached, filepath.Base(cached)); err != nil {
				t.Fatal("native recovery executable lost integrity", err)
			}
			var saved Plan
			if err := readJSON(filepath.Join(p.State, "plan.json"), &saved); err != nil {
				t.Fatal(err)
			}
			if saved.Backend != p.Backend || saved.Watcher != p.Watcher {
				t.Fatal("unused tool declarations were rewritten")
			}
			if err := releaseUnused(saved, true); err != nil {
				t.Fatal("empty-scope recovery required the absent backend", err)
			}
		})
	}
}

func TestServiceAssetPairHashesEachDistinctInodeOnce(t *testing.T) {
	for _, linked := range []bool{true, false} {
		t.Run(map[bool]string{true: "hardlinks", false: "independent-copies"}[linked], func(t *testing.T) {
			base := physicalTempDir(t)
			data := []byte("verified executable bytes")
			digest := sha256.Sum256(data)
			left, right := filepath.Join(base, "left"), filepath.Join(base, "right")
			if err := os.WriteFile(left, data, 0700); err != nil {
				t.Fatal(err)
			}
			var err error
			if linked {
				err = os.Link(left, right)
			} else {
				err = os.WriteFile(right, data, 0700)
			}
			if err != nil {
				t.Fatal(err)
			}
			files := []*os.File{}
			for _, path := range []string{left, right} {
				file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
				if err != nil {
					t.Fatal(err)
				}
				defer file.Close()
				files = append(files, file)
			}
			verified, err := verifyAssetPair(files[0], files[1], hex.EncodeToString(digest[:]))
			if err != nil || (verified != nil) != linked {
				t.Fatalf("unexpected verified inode match: %v %v", verified, err)
			}
			for index, file := range files {
				offset, err := file.Seek(0, io.SeekCurrent)
				want := int64(len(data))
				if linked && index == 1 {
					want = 0
				}
				if err != nil || offset != want {
					t.Fatalf("content read offset[%d]=%d, want %d: %v", index, offset, want, err)
				}
			}
		})
	}
}

func TestServiceAssetReuseRejectsCorruptionAndIndirection(t *testing.T) {
	for _, kind := range []string{"linked-content", "destination-content", "pool-content", "destination-mode", "pool-mode", "destination-symlink", "pool-symlink"} {
		t.Run(kind, func(t *testing.T) {
			base := physicalTempDir(t)
			p := storageFixture(t, base, "a")
			data := []byte("verified executable bytes")
			digest := sha256.Sum256(data)
			name := hex.EncodeToString(digest[:])
			assets := filepath.Join(p.State, "assets")
			destination, err := installServiceAsset(assets, name, data)
			if err != nil {
				t.Fatal(err)
			}
			shared := filepath.Join(base, ".assets-v1", name)
			if kind != "linked-content" {
				copy := filepath.Join(assets, "copy")
				if err := os.WriteFile(copy, data, 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.Rename(copy, destination); err != nil {
					t.Fatal(err)
				}
			}
			target := destination
			if kind == "pool-content" || kind == "pool-mode" || kind == "pool-symlink" {
				target = shared
			}
			switch kind {
			case "linked-content", "destination-content", "pool-content":
				err = os.WriteFile(target, []byte("corrupt"), 0700)
			case "destination-mode", "pool-mode":
				err = os.Chmod(target, 0600)
			default:
				outside := filepath.Join(base, "outside")
				if err = os.Rename(target, outside); err == nil {
					err = os.Symlink(outside, target)
				}
			}
			if err != nil {
				t.Fatal(err)
			}
			linked := false
			_, err = installServiceAssetLink(assets, name, data, func(source, destination string) error {
				linked = true
				return os.Link(source, destination)
			})
			if err == nil || linked {
				t.Fatalf("invalid asset was accepted or relinked: %v linked=%v", err, linked)
			}
		})
	}
}

func TestServiceAssetVerifiedNameRejectsReplacement(t *testing.T) {
	base := physicalTempDir(t)
	path := filepath.Join(base, "asset")
	data := []byte("verified executable bytes")
	digest := sha256.Sum256(data)
	if err := os.WriteFile(path, data, 0700); err != nil {
		t.Fatal(err)
	}
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	verified, err := verifyAssetFile(file, hex.EncodeToString(digest[:]))
	if err != nil {
		t.Fatal(err)
	}
	if err := verifyAssetName(path, verified); err != nil {
		t.Fatal(err)
	}
	replacement := filepath.Join(base, "replacement")
	if err := os.WriteFile(replacement, data, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(replacement, path); err != nil {
		t.Fatal(err)
	}
	if err := verifyAssetName(path, verified); err == nil {
		t.Fatal("unverified replacement inode was accepted")
	}
}
