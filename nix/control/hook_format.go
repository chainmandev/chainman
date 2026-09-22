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

type hookEntry struct {
	Mode string `json:"mode"`
	OID  string `json:"oid"`
	Path string `json:"path"`
}
type hookFormatInput struct {
	Paths     []string          `json:"paths"`
	Authority map[string][]byte `json:"authority"`
}
type hookFormatResult struct {
	Touched  []string `json:"touched"`
	Selected []string `json:"selected"`
}
type hookTextPolicy struct {
	Staged, Working []byte
	Autocrlf, EOL   string
}

func (p HookPlan) textPolicy(index string, paths []string) (hookTextPolicy, error) {
	policy := hookTextPolicy{}
	names := []byte(strings.Join(paths, "\x00") + "\x00")
	argv := []string{"check-attr", "-z", "--stdin", "text", "eol", "filter", "ident", "working-tree-encoding"}
	var e error
	policy.Staged, e = p.git(p.Root, index, names, true, append(argv, "--cached")...)
	if e != nil {
		return policy, e
	}
	policy.Working, e = p.git(p.Root, index, names, true, argv...)
	if e != nil {
		return policy, e
	}
	b, e := p.git(p.Root, index, nil, true, "config", "--type=bool-or-str", "--get", "core.autocrlf")
	if e != nil && !hookAbsent(e) {
		return policy, e
	}
	policy.Autocrlf = strings.ToLower(strings.TrimSpace(string(b)))
	b, e = p.git(p.Root, index, nil, true, "config", "--get", "core.eol")
	if e != nil && !hookAbsent(e) {
		return policy, e
	}
	policy.EOL = strings.ToLower(strings.TrimSpace(string(b)))
	return policy, nil
}
func hookAttributes(data []byte) map[string]map[string]string {
	result := map[string]map[string]string{}
	fields := bytes.Split(bytes.TrimSuffix(data, []byte{0}), []byte{0})
	for i := 0; i+2 < len(fields); i += 3 {
		path := string(fields[i])
		if result[path] == nil {
			result[path] = map[string]string{}
		}
		result[path][string(fields[i+1])] = string(fields[i+2])
	}
	return result
}
func (p hookTextPolicy) endings(path string) (bool, bool, error) {
	staged, working := hookAttributes(p.Staged)[path], hookAttributes(p.Working)[path]
	a, _ := json.Marshal(staged)
	b, _ := json.Marshal(working)
	if !bytes.Equal(a, b) {
		return false, false, fmt.Errorf("staged and working Git attributes differ for %q; stage a coherent policy", path)
	}
	for _, key := range []string{"filter", "ident", "working-tree-encoding"} {
		if staged[key] != "unspecified" && staged[key] != "unset" {
			return false, false, fmt.Errorf("unsupported Git content transformation for %q: %s; no clean/smudge filters were run", path, key)
		}
	}
	switch p.Autocrlf {
	case "", "true", "false", "input":
	default:
		return false, false, fmt.Errorf("unsupported core.autocrlf")
	}
	switch p.EOL {
	case "", "native", "lf", "crlf":
	default:
		return false, false, fmt.Errorf("unsupported core.eol")
	}
	text, eol := staged["text"], staged["eol"]
	enabled := text != "unset" && (text == "set" || text == "auto" || eol == "lf" || eol == "crlf" || p.Autocrlf == "true" || p.Autocrlf == "input")
	crlf := eol == "crlf" || (eol != "lf" && (p.Autocrlf == "true" || (p.Autocrlf != "input" && p.EOL == "crlf")))
	return enabled, crlf, nil
}
func (p HookPlan) hookFormat() error {
	done, e := hookOperation(p.Root)
	if e != nil {
		return e
	}
	defer done()
	lockPath, e := hookPath(p.Root, ".cache/toolchain/staged-format.lock")
	if e != nil {
		return e
	}
	serial, e := locked(lockPath, true)
	if e != nil {
		return fmt.Errorf("staged formatting is already running; retry when it finishes: %w", e)
	}
	defer serial.Close()
	if !p.hookRepo() {
		return fmt.Errorf("formatting requires the project's own Git repository")
	}
	index := os.Getenv("GIT_INDEX_FILE")
	if index == "" {
		index, e = p.query("rev-parse", "--path-format=absolute", "--git-path", "index")
		if e != nil {
			return e
		}
	}
	index = hookAbsolute(p.Root, index)
	beforeIndex, e := hookRead(index)
	if e != nil {
		return e
	}
	head, _ := p.query("rev-parse", "--verify", "HEAD")
	branch, _ := p.query("symbolic-ref", "-q", "HEAD")
	raw, e := p.git(p.Root, index, nil, false, "ls-files", "--stage", "-z")
	if e != nil {
		return e
	}
	items := map[string]hookEntry{}
	ordered := []hookEntry{}
	for _, record := range bytes.Split(raw, []byte{0}) {
		if len(record) == 0 {
			continue
		}
		header, path, ok := strings.Cut(string(record), "\t")
		fields := strings.Fields(header)
		if !ok || len(fields) != 3 || fields[2] != "0" {
			return fmt.Errorf("resolve unmerged index entries before formatting")
		}
		if path == ".git" || strings.HasPrefix(path, ".git/") || strings.Contains(path, "/.git/") {
			return fmt.Errorf("unsafe index path %q", path)
		}
		entry := hookEntry{fields[0], fields[1], path}
		items[path] = entry
		ordered = append(ordered, entry)
	}
	raw, e = p.git(p.Root, index, nil, false, "diff", "--cached", "--no-renames", "--name-only", "--diff-filter=ACMT", "-z")
	if e != nil {
		return e
	}
	paths := []string{}
	for _, raw := range bytes.Split(raw, []byte{0}) {
		path := string(raw)
		if items[path].Mode == "100644" || items[path].Mode == "100755" {
			paths = append(paths, path)
		}
	}
	if len(paths) == 0 {
		return nil
	}
	beforeFiles := map[string]hookFile{}
	fileErrors := map[string]error{}
	for _, name := range paths {
		path, e := hookPath(p.Root, name)
		if e != nil {
			fileErrors[name] = e
			continue
		}
		beforeFiles[name], fileErrors[name] = hookRead(path)
	}
	policy, e := p.textPolicy(index, paths)
	if e != nil {
		return e
	}
	flags, e := p.git(p.Root, index, nil, false, "ls-files", "-v", "-z")
	if e != nil {
		return e
	}
	flagged := map[string]bool{}
	for _, flag := range bytes.Split(flags, []byte{0}) {
		if len(flag) > 2 && (flag[0] == 'S' || (flag[0] >= 'a' && flag[0] <= 'z')) {
			flagged[string(flag[2:])] = true
		}
	}
	pool, e := hookPath(p.Root, ".chainman/staged-format")
	if e != nil {
		return e
	}
	if e = os.MkdirAll(pool, 0700); e != nil {
		return e
	}
	journals, e := filepath.Glob(filepath.Join(pool, "*", "apply.json"))
	if e != nil {
		return e
	}
	if len(journals) > 0 {
		return fmt.Errorf("interrupted formatting requires review: %s; original index/files and results are preserved; do not overwrite subsequent edits", filepath.Dir(journals[0]))
	}
	directory, e := os.MkdirTemp(pool, "transaction-")
	if e != nil {
		return e
	}
	keep := false
	defer func() {
		if !keep {
			os.RemoveAll(directory)
		} else {
			fmt.Fprintln(os.Stderr, "Formatting recovery material:", directory)
		}
	}()
	snapshot := filepath.Join(directory, "snapshot")
	if e = os.Mkdir(snapshot, 0700); e != nil {
		return e
	}
	saved := filepath.Join(directory, "original-index")
	if beforeIndex.Exists {
		e = os.WriteFile(saved, beforeIndex.Body, 0600)
	} else {
		_, e = p.git(p.Root, saved, nil, false, "read-tree", "--empty")
	}
	if e != nil {
		return e
	}
	if _, e = p.git(snapshot, "", nil, false, "init", "-q", "--template="); e != nil {
		return e
	}
	var indexInfo bytes.Buffer
	for start := 0; start < len(ordered); start += 64 {
		end := start + 64
		if end > len(ordered) {
			end = len(ordered)
		}
		batch := []hookEntry{}
		oids := []string{}
		for _, entry := range ordered[start:end] {
			if entry.Mode == "100644" || entry.Mode == "100755" {
				batch = append(batch, entry)
				oids = append(oids, entry.OID)
			}
		}
		if len(batch) == 0 {
			continue
		}
		bodies, e := p.blobBodies(oids)
		if e != nil {
			return e
		}
		files := []string{}
		for i, entry := range batch {
			body := bodies[i]
			path, e := hookPath(snapshot, entry.Path)
			if e != nil {
				return e
			}
			if e = os.MkdirAll(filepath.Dir(path), 0700); e != nil {
				return e
			}
			mode := os.FileMode(0644)
			if entry.Mode == "100755" {
				mode = 0755
			}
			if e = os.WriteFile(path, body, mode); e != nil {
				return e
			}
			files = append(files, "./"+entry.Path)
			fmt.Fprintf(&indexInfo, "%s %s\t%s\x00", entry.Mode, entry.OID, entry.Path)
		}
		if _, e = p.git(snapshot, "", nil, false, append([]string{"hash-object", "-w", "--no-filters", "--"}, files...)...); e != nil {
			return e
		}
	}
	if _, e = p.git(snapshot, "", indexInfo.Bytes(), false, "update-index", "-z", "--index-info"); e != nil {
		return e
	}
	tree, e := p.git(snapshot, "", nil, false, "write-tree")
	if e != nil {
		return e
	}
	commit, e := p.git(snapshot, "", []byte("Staged formatting snapshot\n"), false, "-c", "user.name=chainman snapshot", "-c", "user.email=snapshot@example.invalid", "commit-tree", strings.TrimSpace(string(tree)))
	if e != nil {
		return e
	}
	if _, e = p.git(snapshot, "", nil, false, "update-ref", "HEAD", strings.TrimSpace(string(commit))); e != nil {
		return e
	}
	if e = atomic(filepath.Join(directory, "input.json"), hookFormatInput{paths, p.Authority}); e != nil {
		return e
	}
	if e = p.worker("format", directory); e != nil {
		return e
	}
	var result hookFormatResult
	if e = readJSON(filepath.Join(directory, "result", "manifest.json"), &result); e != nil {
		return e
	}
	for _, path := range result.Selected {
		if _, ok := beforeFiles[path]; !ok {
			return fmt.Errorf("invalid formatter selection: %q", path)
		}
		if _, _, e = policy.endings(path); e != nil {
			return e
		}
	}
	if len(result.Touched) == 0 {
		return nil
	}
	sort.Strings(result.Touched)
	replacements := map[string][]byte{}
	formattedBlobs := map[string][]byte{}
	partial := []string{}
	for _, path := range result.Touched {
		entry, ok := items[path]
		if !ok || beforeFiles[path].Exists == false {
			return fmt.Errorf("missing regular working input for %q; restore it before formatting", path)
		}
		if e = fileErrors[path]; e != nil {
			return e
		}
		if flagged[path] {
			return fmt.Errorf("clear assume-unchanged/skip-worktree on %q", path)
		}
		base, e := p.git(p.Root, "", nil, false, "cat-file", "blob", entry.OID)
		if e != nil {
			return e
		}
		file, e := hookPath(snapshot, path)
		if e != nil {
			return e
		}
		out, e := hookRead(file)
		if e != nil {
			return e
		}
		want := os.FileMode(0644)
		if entry.Mode == "100755" {
			want = 0755
		}
		if !out.Exists || out.Mode != want {
			return fmt.Errorf("formatter changed file mode or removed %q", path)
		}
		normalize, crlf, e := policy.endings(path)
		if e != nil {
			return e
		}
		working, formatted := beforeFiles[path].Body, out.Body
		if normalize {
			if bytes.Contains(base, []byte{0}) || bytes.Contains(working, []byte{0}) || bytes.Contains(base, []byte("\r\n")) {
				return fmt.Errorf("cannot safely normalize %q; use a normalized text index", path)
			}
			working = bytes.ReplaceAll(working, []byte("\r\n"), []byte("\n"))
			formatted = bytes.ReplaceAll(formatted, []byte("\r\n"), []byte("\n"))
		}
		if bytes.Equal(base, formatted) {
			continue
		}
		if !bytes.Equal(base, working) {
			partial = append(partial, path)
			continue
		}
		formattedBlobs[path] = formatted
		if normalize && crlf {
			formatted = bytes.ReplaceAll(formatted, []byte("\n"), []byte("\r\n"))
		}
		replacements[path] = formatted
	}
	if len(partial) > 0 {
		return fmt.Errorf("partially staged files need formatting: %q; nothing applied. Format and review these files, then stage the intended changes and retry", partial)
	}
	if len(replacements) == 0 {
		return nil
	}
	replacement := filepath.Join(directory, "formatted-index")
	original, e := os.ReadFile(saved)
	if e != nil {
		return e
	}
	if e = os.WriteFile(replacement, original, 0600); e != nil {
		return e
	}
	touched := []string{}
	for _, path := range result.Touched {
		if _, ok := replacements[path]; !ok {
			continue
		}
		touched = append(touched, path)
		oid, e := p.git(p.Root, "", formattedBlobs[path], false, "hash-object", "-w", "--stdin", "--no-filters")
		if e != nil {
			return e
		}
		info := []byte(items[path].Mode + " " + strings.TrimSpace(string(oid)) + "\t" + path + "\x00")
		if _, e = p.git(p.Root, replacement, info, false, "update-index", "-z", "--index-info"); e != nil {
			return e
		}
	}
	f, e := os.OpenFile(index+".lock", os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0600)
	if e != nil {
		return fmt.Errorf("Git index is in use; no formatting applied: %w", e)
	}
	defer f.Close()
	defer os.Remove(index + ".lock")
	body, e := os.ReadFile(replacement)
	if e != nil {
		return e
	}
	if _, e = f.Write(body); e != nil {
		return e
	}
	if e = f.Sync(); e != nil {
		return e
	}
	if beforeIndex.Exists {
		if e = f.Chmod(beforeIndex.Mode); e != nil {
			return e
		}
	}
	if e = f.Close(); e != nil {
		return e
	}
	now, e := hookRead(index)
	if e != nil {
		return e
	}
	h, _ := p.query("rev-parse", "--verify", "HEAD")
	b, _ := p.query("symbolic-ref", "-q", "HEAD")
	currentPolicy, e := p.textPolicy(index, paths)
	if e != nil {
		return e
	}
	pa, _ := json.Marshal(policy)
	pb, _ := json.Marshal(currentPolicy)
	if !hookEqual(now, beforeIndex) || h != head || b != branch || !bytes.Equal(pa, pb) {
		return fmt.Errorf("HEAD, index or Git policy changed during formatting; nothing applied")
	}
	for _, path := range paths {
		if fileErrors[path] != nil {
			continue
		}
		now, e := hookRead(filepath.Join(p.Root, path))
		if e != nil {
			return e
		}
		if !hookEqual(now, beforeFiles[path]) {
			return fmt.Errorf("working file changed during formatting: %q; nothing applied", path)
		}
	}
	for path, body := range p.Authority {
		target, e := hookPath(p.Root, path)
		if e != nil {
			return e
		}
		f, e := hookRead(target)
		if e != nil {
			return e
		}
		if !f.Exists || !bytes.Equal(f.Body, body) {
			return fmt.Errorf("orchestration configuration changed during formatting; nothing applied")
		}
	}
	modes := []uint32{}
	for i, path := range touched {
		if e = os.WriteFile(filepath.Join(directory, fmt.Sprintf("original-%d", i)), beforeFiles[path].Body, 0600); e != nil {
			return e
		}
		if e = os.WriteFile(filepath.Join(directory, fmt.Sprintf("formatted-%d", i)), replacements[path], 0600); e != nil {
			return e
		}
		modes = append(modes, uint32(beforeFiles[path].Mode))
	}
	if e = atomic(filepath.Join(directory, "apply.json"), map[string]any{"index": index, "paths": touched, "modes": modes}); e != nil {
		return e
	}
	keep = true
	for _, path := range touched {
		current, e := hookRead(filepath.Join(p.Root, path))
		if e != nil {
			return e
		}
		if !hookEqual(current, beforeFiles[path]) {
			return fmt.Errorf("concurrent edit while applying %q; inspect retained recovery files", path)
		}
		if e = hookWrite(filepath.Join(p.Root, path), replacements[path], beforeFiles[path].Mode); e != nil {
			return e
		}
	}
	if e = os.Rename(index+".lock", index); e != nil {
		return e
	}
	if e = os.Remove(filepath.Join(directory, "apply.json")); e != nil {
		return e
	}
	keep = false
	fmt.Printf("Formatted %d staged file(s); unrelated edits preserved\n", len(touched))
	return nil
}
