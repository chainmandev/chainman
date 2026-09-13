package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestContainerProbeBindsOwnedRunningIdentity(t *testing.T) {
	root := physicalTempDir(t)
	engine := filepath.Join(root, "engine")
	owner := &Container{Engine: engine, Name: "chainman-fixture", Token: strings.Repeat("a", 32)}
	id := strings.Repeat("b", 64)
	original := Command{Argv: []string{engine, "exec", owner.Name, "probe", owner.Name, "$(literal) two words"}}
	for _, kind := range []string{"owned", "replacement", "stopped", "missing", "outage"} {
		t.Run(kind, func(t *testing.T) {
			token := owner.Token
			if kind == "replacement" {
				token = "another-owner"
			}
			data, _ := json.Marshal([]any{map[string]any{"Id": id, "State": map[string]bool{"Running": kind != "stopped"}, "Config": map[string]any{"Labels": map[string]string{"dev.chainman.owner": token}}}})
			body := "#!/bin/sh\n[ \"$1 $2\" = 'container inspect' ] || exit 9\nprintf '%s\\n' " + quote(string(data)) + "\n"
			if kind == "missing" {
				body = "#!/bin/sh\n[ \"$1 $2\" = 'container ls' ]\n"
			} else if kind == "outage" {
				body = "#!/bin/sh\nexit 1\n"
			}
			if e := os.WriteFile(engine, []byte(body), 0700); e != nil {
				t.Fatal(e)
			}
			command, e := containerProbe(owner, original)
			if kind != "owned" {
				if e == nil {
					t.Fatal("accepted uncertain or changed probe ownership")
				}
				return
			}
			expected := []string{engine, "exec", id, "probe", owner.Name, "$(literal) two words"}
			if e != nil || !reflect.DeepEqual(command.Argv, expected) || original.Argv[2] != owner.Name {
				t.Fatalf("probe did not bind immutable identity and retain literal args: %+v %v", command, e)
			}
		})
	}
	for _, argv := range [][]string{{engine, "exec", "different", "probe"}, {engine, "run", owner.Name, "probe"}, {"other", "exec", owner.Name, "probe"}, {engine, "exec", owner.Name}} {
		if _, e := containerProbe(owner, Command{Argv: argv}); e == nil {
			t.Fatal("accepted probe without exact generated prefix")
		}
	}
	host := Command{Argv: []string{"/fixture/host-probe", "literal"}}
	if command, e := containerProbe(nil, host); e != nil || !reflect.DeepEqual(command, host) {
		t.Fatal("host probe changed", command, e)
	}
}
