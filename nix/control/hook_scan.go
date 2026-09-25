package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

var hookOID = regexp.MustCompile(`^([0-9a-f]{40}|[0-9a-f]{64})$`)

type hookScanEntry struct {
	Mode     string `json:"mode"`
	Blob     string `json:"blob"`
	Path     string `json:"path"`
	Revision string `json:"revision"`
}
type hookScanInput struct {
	Entries []hookScanEntry `json:"entries"`
	Roots   []string        `json:"roots"`
}

// Remote advertisements are metadata, not source or policy. Bound captured
// output, suppress transport diagnostics (which may contain credential URLs),
// and never import objects or change remote-tracking refs during a push hook.
type hookAdvertisement struct{ body bytes.Buffer }

func (b *hookAdvertisement) Write(p []byte) (int, error) {
	if b.body.Len()+len(p) > 4<<20 {
		return 0, fmt.Errorf("remote advertisement exceeds 4 MiB")
	}
	return b.body.Write(p)
}

func advertisedHookObjects(body []byte) ([]string, error) {
	objects := []string{}
	seen := map[string]bool{}
	for _, line := range bytes.Split(bytes.TrimSpace(body), []byte{'\n'}) {
		if len(line) == 0 {
			continue
		}
		fields := strings.Fields(string(line))
		if len(fields) != 2 || !hookOID.MatchString(fields[0]) || strings.Trim(fields[0], "0") == "" || !strings.HasPrefix(fields[1], "refs/") {
			return nil, fmt.Errorf("invalid remote advertisement")
		}
		if !seen[fields[0]] {
			objects = append(objects, fields[0])
			seen[fields[0]] = true
		}
	}
	return objects, nil
}

func (p HookPlan) advertisedBases(destination string) ([]string, error) {
	if destination == "" {
		return nil, fmt.Errorf("push destination is unavailable")
	}
	c := exec.Command(p.Git, "-C", p.Root, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "credential.interactive=false", "ls-remote", "--refs", "--", destination)
	c.Env = append(hookEnv(false), "GIT_TERMINAL_PROMPT=0", "GIT_ASKPASS=false", "SSH_ASKPASS_REQUIRE=never")
	c.Dir = p.Root
	var output hookAdvertisement
	c.Stdout, c.Stderr = &output, io.Discard
	if e := hookRunBounded(c, 0, 10*time.Second); e != nil {
		var interrupted *startupInterrupted
		if errors.As(e, &interrupted) {
			return nil, e
		}
		return nil, fmt.Errorf("remote discovery failed or exceeded its deadline")
	}
	objects, e := advertisedHookObjects(output.body.Bytes())
	if e != nil || len(objects) == 0 {
		return nil, fmt.Errorf("remote advertisement has no usable baseline")
	}
	selectors := make([]string, len(objects))
	for i, oid := range objects {
		selectors[i] = oid + "^{commit}"
	}
	raw, e := p.git(p.Root, "", []byte(strings.Join(selectors, "\n")+"\n"), false, "cat-file", "--batch-check=%(objectname) %(objecttype)")
	if e != nil {
		return nil, fmt.Errorf("advertised objects could not be inspected locally")
	}
	bases := []string{}
	seen := map[string]bool{}
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[1] == "commit" && hookOID.MatchString(fields[0]) && !seen[fields[0]] {
			bases = append(bases, fields[0])
			seen[fields[0]] = true
		}
	}
	if len(bases) == 0 {
		return nil, fmt.Errorf("advertised commits are unavailable locally")
	}
	return bases, nil
}

func (p HookPlan) outgoing(records []byte) ([]string, error) {
	roots := []string{}
	seen := map[string]bool{}
	discovered := false
	var advertised []string
	add := func(s string) {
		if !seen[s] {
			roots = append(roots, s)
			seen[s] = true
		}
	}
	for _, line := range bytes.Split(records, []byte{'\n'}) {
		if len(line) == 0 {
			continue
		}
		fields := strings.Fields(string(line))
		if len(fields) != 4 || !hookOID.MatchString(fields[1]) || !hookOID.MatchString(fields[3]) {
			return nil, fmt.Errorf("malformed Git pre-push record")
		}
		local, remote := fields[1], fields[3]
		if strings.Trim(local, "0") == "" {
			continue
		}
		peeled, e := p.query("rev-parse", "--verify", local+"^{}")
		if e != nil {
			return nil, e
		}
		kind, e := p.query("cat-file", "-t", peeled)
		if e != nil {
			return nil, e
		}
		if kind == "commit" {
			selectors := []string{local}
			if strings.Trim(remote, "0") != "" {
				base, e := p.query("rev-parse", "--verify", remote+"^{commit}")
				if e == nil {
					selectors = append(selectors, "^"+base)
				} else {
					fmt.Fprintf(os.Stderr, "Trojan Source: remote base %s is unavailable; discovering destination refs\n", remote)
				}
			}
			if len(selectors) == 1 {
				if !discovered {
					discovered = true
					advertised, e = p.advertisedBases(os.Getenv("CHAINMAN_HOOK_REMOTE_URL"))
					if e != nil {
						var interrupted *startupInterrupted
						if errors.As(e, &interrupted) {
							return nil, e
						}
						fmt.Fprintf(os.Stderr, "Trojan Source: %s; conservatively scanning all locally reachable history\n", e)
					}
				}
				for _, base := range advertised {
					selectors = append(selectors, "^"+base)
				}
			}
			revisions, e := p.git(p.Root, "", []byte(strings.Join(selectors, "\n")+"\n"), false, "rev-list", "--topo-order", "--reverse", "--stdin")
			if e != nil {
				return nil, e
			}
			for _, rev := range strings.Fields(string(revisions)) {
				add(rev)
			}
			// A new ref may introduce no commits relative to another remote ref.
			// Its proposed tree must still pass the current scanner policy.
			add(peeled)
		} else if kind == "tree" || kind == "blob" {
			add(peeled)
		} else {
			return nil, fmt.Errorf("unsupported outgoing Git object %s", local)
		}
	}
	return roots, nil
}
func (p HookPlan) hookScan(args []string) error {
	var roots []string
	var e error
	if len(args) > 0 {
		if len(args) != 1 || !hookOID.MatchString(args[0]) {
			return fmt.Errorf("use trojan-source [full commit SHA]; otherwise provide pre-push records on stdin")
		}
		peeled, x := p.query("rev-parse", "--verify", args[0]+"^{}")
		if x != nil {
			return x
		}
		roots = []string{peeled}
	} else {
		records, e := io.ReadAll(os.Stdin)
		if e != nil {
			return e
		}
		roots, e = p.outgoing(records)
		if e != nil {
			return e
		}
	}
	input := hookScanInput{Roots: roots, Entries: []hookScanEntry{}}
	previous := ""
	for _, revision := range roots {
		kind, e := p.query("cat-file", "-t", revision)
		if e != nil {
			return e
		}
		if kind == "blob" {
			input.Entries = append(input.Entries, hookScanEntry{"100644", revision, "<blob tag>", revision})
			continue
		}
		if previous == "" {
			raw, e := p.git(p.Root, "", nil, false, "ls-tree", "-r", "-z", revision)
			if e != nil {
				return e
			}
			for _, record := range bytes.Split(raw, []byte{0}) {
				if len(record) == 0 {
					continue
				}
				header, path, ok := strings.Cut(string(record), "\t")
				fields := strings.Fields(header)
				if !ok || len(fields) != 3 || !utf8.ValidString(path) {
					return fmt.Errorf("invalid Git tree record")
				}
				if fields[1] == "blob" {
					input.Entries = append(input.Entries, hookScanEntry{fields[0], fields[2], path, revision})
				}
			}
		} else {
			raw, e := p.git(p.Root, "", nil, false, "diff-tree", "--no-commit-id", "--raw", "--no-abbrev", "-r", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", previous, revision)
			if e != nil {
				return e
			}
			records := bytes.Split(raw, []byte{0})
			for i := 0; i+1 < len(records); i += 2 {
				fields := strings.Fields(string(records[i]))
				if len(fields) != 5 || !utf8.Valid(records[i+1]) {
					return fmt.Errorf("invalid Git diff record")
				}
				input.Entries = append(input.Entries, hookScanEntry{fields[1], fields[3], string(records[i+1]), revision})
			}
		}
		previous = revision
	}
	directory, e := os.MkdirTemp(p.Directory, "scan-")
	if e != nil {
		return e
	}
	defer os.RemoveAll(directory)
	if e = atomic(filepath.Join(directory, "input.json"), input); e != nil {
		return e
	}
	if e = p.worker("scan-select", directory); e != nil {
		return e
	}
	var requested []string
	requestFile, e := os.Open(filepath.Join(directory, "result", "requested.json"))
	if e != nil {
		return e
	}
	e = json.NewDecoder(requestFile).Decode(&requested)
	requestFile.Close()
	if e != nil {
		return e
	}
	known := map[string]bool{}
	for _, entry := range input.Entries {
		known[entry.Blob] = true
	}
	blobs := filepath.Join(directory, "blobs")
	if e = os.Mkdir(blobs, 0700); e != nil {
		return e
	}
	for _, oid := range requested {
		if !hookOID.MatchString(oid) || !known[oid] {
			return fmt.Errorf("invalid scanner blob request")
		}
	}
	for start := 0; start < len(requested); start += 64 {
		end := start + 64
		if end > len(requested) {
			end = len(requested)
		}
		batch := requested[start:end]
		bodies, e := p.blobBodies(batch)
		if e != nil {
			return e
		}
		for i, oid := range batch {
			if e = os.WriteFile(filepath.Join(blobs, oid), bodies[i], 0600); e != nil {
				return e
			}
		}
	}
	return p.worker("scan-check", directory)
}
