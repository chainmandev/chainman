package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode"

	"golang.org/x/sys/unix"
)

// Presentation data never participates in admission, ownership or commands.
type Presentation struct {
	Task    string            `json:"task"`
	Title   string            `json:"title"`
	URLs    map[string]string `json:"urls"`
	Details map[string]string `json:"details"`
}

type ApplicationStatus struct {
	ID             string       `json:"id"`
	Presentation   Presentation `json:"presentation"`
	Owner          Identity     `json:"owner"`
	Phase          string       `json:"phase"`
	Started        time.Time    `json:"started"`
	Updated        time.Time    `json:"updated"`
	Outcome        string       `json:"outcome,omitempty"`
	ReachedReady   bool         `json:"reached_ready"`
	ElapsedSeconds float64      `json:"elapsed_seconds"`
	Problems       []string     `json:"problems,omitempty"`
}

func terminalPhase(phase string) bool { return phase == "stopped" || phase == "failed" }

func printable(value string) bool {
	return len(value) <= 8192 && strings.TrimSpace(value) != "" && !strings.ContainsFunc(value, func(r rune) bool {
		return unicode.IsControl(r) || unicode.Is(unicode.Cf, r)
	})
}

func (p Presentation) validate() error {
	if !printable(p.Title) || (p.Task != "" && !validName.MatchString(p.Task)) || len(p.URLs) > 16 || len(p.Details) > 16 {
		return fmt.Errorf("invalid development presentation")
	}
	for _, values := range []map[string]string{p.URLs, p.Details} {
		for label, value := range values {
			if !printable(label) || !printable(value) {
				return fmt.Errorf("invalid development display value")
			}
		}
	}
	for _, value := range p.URLs {
		u, err := url.Parse(value)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil {
			return fmt.Errorf("development URLs require HTTP(S), a hostname and no credentials")
		}
	}
	return nil
}

func applicationStatuses(state string) ([]ApplicationStatus, error) {
	path := filepath.Join(state, "applications")
	entries, err := os.ReadDir(path)
	if os.IsNotExist(err) {
		return []ApplicationStatus{}, nil
	}
	if err != nil {
		return nil, err
	}
	if err = private(path); err != nil {
		return nil, err
	}
	rows := []ApplicationStatus{}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		var row ApplicationStatus
		if err = readJSON(filepath.Join(path, entry.Name()), &row); err != nil {
			// Another client may prune completed records after this directory
			// snapshot. Their absence must not prevent inspection or task startup.
			if os.IsNotExist(err) {
				continue
			}
			return nil, err
		}
		if len(row.ID) != 32 || strings.Trim(row.ID, "0123456789abcdef") != "" || row.ID+".json" != entry.Name() {
			return nil, fmt.Errorf("development status identity mismatch")
		}
		if err = row.Presentation.validate(); err != nil {
			return nil, err
		}
		if !terminalPhase(row.Phase) {
			if !row.Owner.alive() {
				row.Phase, row.Outcome = "stopped", "owner lost; inspect service status"
			} else if _, err := os.Stat(filepath.Join(state, "stop-request.json")); err == nil && row.Phase != "starting" {
				row.Phase = "stopping"
			}
		}
		if terminalPhase(row.Phase) {
			row.ElapsedSeconds = row.Updated.Sub(row.Started).Seconds()
		} else {
			row.ElapsedSeconds = time.Since(row.Started).Seconds()
		}
		rows = append(rows, row)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].Started.After(rows[j].Started) })
	return rows, nil
}

func developmentSummary(row ApplicationStatus, reminder bool) string {
	var out strings.Builder
	elapsed := time.Since(row.Started)
	if terminalPhase(row.Phase) {
		elapsed = row.Updated.Sub(row.Started)
	}
	fmt.Fprintf(&out, "\n%s — %s (%s)\n", row.Presentation.Title, row.Phase, elapsed.Truncate(time.Second))
	if !reminder {
		for _, section := range []map[string]string{row.Presentation.URLs, row.Presentation.Details} {
			keys := make([]string, 0, len(section))
			for key := range section {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			for _, key := range keys {
				fmt.Fprintf(&out, "  %s: %s\n", key, section[key])
			}
		}
	}
	for _, problem := range row.Problems {
		fmt.Fprintln(&out, "  "+problem)
	}
	if row.Outcome != "" {
		fmt.Fprintln(&out, "  "+row.Outcome)
	}
	fmt.Fprintln(&out, "Logs: just chainman services-logs --follow | Status: just chainman services-status --human | Stop: Ctrl-C")
	return out.String()
}

func printDevelopmentStatus(state string, value map[string]any, human bool) int {
	rows, err := applicationStatuses(state)
	if err != nil {
		return exitCode(err)
	}
	value["applications"] = rows
	if !human {
		return exitCode(json.NewEncoder(os.Stdout).Encode(value))
	}
	writeHumanDevelopmentStatus(os.Stdout, rows, value)
	return 0
}

func writeHumanDevelopmentStatus(w io.Writer, rows []ApplicationStatus, value map[string]any) {
	if len(rows) == 0 {
		fmt.Fprintln(w, "No recorded application operation in this service scope.")
	}
	for i, row := range rows {
		// Show every active client and the newest completed operation only.
		if i > 0 && terminalPhase(row.Phase) {
			continue
		}
		fmt.Fprintf(w, "Operation %s (%s)\n", row.ID, row.Presentation.Task)
		fmt.Fprint(w, developmentSummary(row, false))
	}
	writeServiceStatus(w, value, "")
	if resources, ok := value["resources"].([]map[string]any); ok {
		for _, resource := range resources {
			fmt.Fprintf(w, "Shared resource %s:\n", resource["state"])
			writeServiceStatus(w, resource, "  ")
		}
	}
}

func writeServiceStatus(w io.Writer, value map[string]any, indent string) {
	if bridge, ok := value["bridge"].(string); ok {
		fmt.Fprintf(w, "%sNetwork bridge %s: running=%v (clients=%v)\n", indent, bridge, value["running"], value["clients"])
	}
	if services, ok := value["services"].([]Process); ok {
		for _, service := range services {
			fmt.Fprintf(w, "%sService %s: %s (ready=%s)\n", indent, service.Name, service.Status, service.Ready)
		}
	}
	if value["recovery_required"] == true {
		fmt.Fprintln(w, indent+"Service controller recovery required; inspect JSON status and service logs.")
	}
}

// Neither a full output pipe nor a stalled terminal may hold a service lease.
type developmentOutput struct {
	messages chan string
	done     chan struct{}
}

func newDevelopmentOutput() *developmentOutput {
	o := &developmentOutput{make(chan string, 16), make(chan struct{})}
	go func() {
		defer close(o.done)
		for message := range o.messages {
			fmt.Fprint(os.Stderr, message)
		}
	}()
	return o
}
func (o *developmentOutput) write(message string) {
	select {
	case o.messages <- message:
	default:
	}
}
func (o *developmentOutput) close() {
	close(o.messages)
	select {
	case <-o.done:
	case <-time.After(50 * time.Millisecond):
	}
}

type diagnosticTail struct {
	mu   sync.Mutex
	data []byte
}

func (t *diagnosticTail) Write(data []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	size := len(data)
	if len(data) > logHistoryBytes {
		data = data[len(data)-logHistoryBytes:]
	}
	t.data = append(t.data, data...)
	if len(t.data) > logHistoryBytes {
		t.data = t.data[len(t.data)-logHistoryBytes:]
	}
	return size, nil
}
func (t *diagnosticTail) excerpt() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	data := t.data
	if len(data) > 16*1024 {
		data = data[len(data)-16*1024:]
		if i := bytes.IndexByte(data, '\n'); i >= 0 {
			data = data[i+1:]
		}
	}
	return string(data)
}

type developmentSession struct {
	mu      sync.Mutex
	row     ApplicationStatus
	path    string
	channel string
	plan    Plan
	summary bool
	output  *developmentOutput
	tail    diagnosticTail
	stop    chan struct{}
	done    chan struct{}
	health  map[string][]string
}

// Reuse the existing service monitor's observations, without adding probes or
// changing the monitor's cancellation/restart policy.
func (s *developmentSession) services(plan Plan, processes []Process, wanted map[string]bool) {
	if s == nil {
		return
	}
	var problems []string
	for _, process := range processes {
		if wanted[process.Name] && (!process.Running || (plan.Services[process.Name].Readiness != nil && process.Ready != "Ready")) {
			problems = append(problems, "Service not ready: "+process.Name)
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.health[plan.State] = problems
}

func developmentMode(value string, tty bool) (bool, error) {
	switch value {
	case "", "auto":
		return tty, nil
	case "summary":
		return true, nil
	case "logs":
		return false, nil
	default:
		return false, fmt.Errorf("CHAINMAN_DEV_OUTPUT must be auto, summary or logs")
	}
}

func beginDevelopment(p *Plan) (*developmentSession, error) {
	if !p.WaitForServices {
		return nil, nil
	}
	_, ttyErr := unix.IoctlGetTermios(int(os.Stderr.Fd()), terminalGet)
	_, inputErr := unix.IoctlGetTermios(int(os.Stdin.Fd()), terminalGet)
	summary, err := developmentMode(os.Getenv("CHAINMAN_DEV_OUTPUT"), ttyErr == nil && inputErr == nil)
	if err != nil {
		return nil, err
	}
	if err = private(filepath.Join(p.State, "applications")); err != nil {
		return nil, err
	}
	rows, err := applicationStatuses(p.State)
	if err != nil {
		return nil, err
	}
	completed := 0
	for _, row := range rows {
		if terminalPhase(row.Phase) {
			completed++
			if completed > 20 {
				if err = os.Remove(filepath.Join(p.State, "applications", row.ID+".json")); err != nil && !os.IsNotExist(err) {
					return nil, err
				}
			}
		}
	}
	channel, err := os.MkdirTemp("", "chainman-development-")
	if err != nil {
		return nil, err
	}
	realChannel, err := filepath.EvalSymlinks(channel)
	if err != nil {
		_ = os.RemoveAll(channel)
		return nil, err
	}
	channel = realChannel
	owner, err := identify(os.Getpid())
	if err != nil {
		_ = os.RemoveAll(channel)
		return nil, err
	}
	presentation := p.Presentation
	if presentation.Title == "" {
		presentation.Title = "Development"
	}
	if err = presentation.validate(); err != nil {
		_ = os.RemoveAll(channel)
		return nil, err
	}
	now := time.Now().UTC()
	id := token()
	s := &developmentSession{
		row:  ApplicationStatus{ID: id, Presentation: presentation, Owner: owner, Phase: "starting", Started: now, Updated: now},
		path: filepath.Join(p.State, "applications", id+".json"), channel: channel,
		plan: *p, summary: summary, output: newDevelopmentOutput(), stop: make(chan struct{}), done: make(chan struct{}), health: map[string][]string{},
	}
	if p.Task.Environment == nil {
		p.Task.Environment = map[string]string{}
	}
	p.Task.Environment["CHAINMAN_DEV_CHANNEL"] = channel
	p.Task.Environment["CHAINMAN_DEV_OPERATION"] = id
	p.Task.Environment["CHAINMAN_DEV_TASK"] = presentation.Task
	s.saveLocked()
	s.output.write(developmentSummary(s.row, false))
	go s.observe()
	return s, nil
}

func (s *developmentSession) saveLocked() {
	s.row.ElapsedSeconds = s.row.Updated.Sub(s.row.Started).Seconds()
	if err := atomic(s.path, s.row); err != nil {
		s.output.write(fmt.Sprintln("chainman: development status unavailable:", err))
	}
}
func (s *developmentSession) phase(phase, outcome string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.row.Phase == phase && s.row.Outcome == outcome {
		return
	}
	s.row.Phase, s.row.Outcome, s.row.Updated = phase, outcome, time.Now().UTC()
	s.saveLocked()
	s.output.write(developmentSummary(s.row, false))
}
func (s *developmentSession) diagnostic() {
	if s != nil && s.summary {
		s.output.write("Recent service output (current operation):\n" + s.tail.excerpt())
	}
}
func (s *developmentSession) writer() io.Writer {
	if s.summary {
		return &s.tail
	}
	return io.MultiWriter(&s.tail, os.Stderr)
}

func readProgress(path, id string) string {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return ""
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() || st.Size() > 1024 {
		return ""
	}
	var event struct{ Operation, Phase string }
	if json.NewDecoder(io.LimitReader(f, 1024)).Decode(&event) != nil || event.Operation != id {
		return ""
	}
	if event.Phase != "ready" && event.Phase != "preparing" {
		return ""
	}
	return event.Phase
}

func (s *developmentSession) observe() {
	defer close(s.done)
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()
	reminded := time.Now()
	for {
		select {
		case <-s.stop:
			return
		case <-tick.C:
		}
		s.mu.Lock()
		if s.row.Phase != "stopping" && !terminalPhase(s.row.Phase) {
			phase := readProgress(filepath.Join(s.channel, "progress.json"), s.row.ID)
			changed := false
			if phase == "ready" && !s.row.ReachedReady {
				s.row.ReachedReady, s.row.Phase, changed = true, "ready", true
			}
			if phase == "preparing" && s.row.Phase == "starting" {
				s.row.Phase, changed = "preparing", true
			}
			if s.row.ReachedReady {
				problems := developmentProblems(s.plan)
				for _, values := range s.health {
					problems = append(problems, values...)
				}
				sort.Strings(problems)
				if strings.Join(problems, "\n") != strings.Join(s.row.Problems, "\n") {
					s.row.Problems, changed = problems, true
					s.row.Phase = "ready"
					if len(problems) != 0 {
						s.row.Phase = "degraded"
						if s.summary {
							s.output.write("Recent service output (current operation):\n" + s.tail.excerpt())
						}
					}
				}
			}
			if changed {
				s.row.Updated = time.Now().UTC()
				s.saveLocked()
				s.output.write(developmentSummary(s.row, false))
				reminded = time.Now()
			} else if !s.row.ReachedReady && time.Since(reminded) >= 15*time.Second {
				s.output.write(developmentSummary(s.row, true))
				reminded = time.Now()
			}
		}
		s.mu.Unlock()
	}
}

func developmentProblems(plan Plan) []string {
	var problems []string
	for _, requested := range append([]Plan{plan}, plan.Resources...) {
		var saved Plan
		if readJSON(filepath.Join(requested.State, "plan.json"), &saved) != nil {
			continue
		}
		names, err := ordered(saved, requested.Requested)
		if err != nil {
			continue
		}
		for _, name := range names {
			if saved.Services[name].Watch == nil {
				continue
			}
			var result map[string]string
			if readJSON(filepath.Join(saved.State, name+".build-result.json"), &result) == nil && result["generation"] == saved.Generation && result["error"] != "" {
				problems = append(problems, "Build failed: "+name+"; the last successful service may still be running")
			}
		}
	}
	sort.Strings(problems)
	return problems
}

func (s *developmentSession) finish(result int) {
	if s == nil {
		return
	}
	close(s.stop)
	<-s.done
	phase, outcome := "stopped", "completed"
	if result == 130 || result == 143 || result == 129 {
		outcome = "cancelled"
	} else if result != 0 {
		phase, outcome = "failed", fmt.Sprintf("exit status %d", result)
		s.diagnostic()
	}
	s.phase(phase, outcome)
	_ = os.RemoveAll(s.channel)
	s.output.close()
}
