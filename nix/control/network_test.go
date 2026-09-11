package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNetworkBorrowingUsesOwnedImmutableIdentity(t *testing.T) {
	root := t.TempDir()
	engine := filepath.Join(root, "engine")
	owner := &Container{Engine: engine, Name: "chainman-primary", Token: strings.Repeat("a", 32)}
	borrower := &Container{Engine: engine, Name: "chainman-borrower", Token: strings.Repeat("b", 32)}
	id := strings.Repeat("c", 64)
	respond := func(token string, running bool) {
		t.Helper()
		data, _ := json.Marshal([]any{map[string]any{"Id": id, "State": map[string]bool{"Running": running}, "Config": map[string]any{"Labels": map[string]string{"dev.chainman.owner": token}}}})
		if e := os.WriteFile(engine, []byte("#!/bin/sh\nprintf '%s\\n' "+quote(string(data))+"\n"), 0700); e != nil {
			t.Fatal(e)
		}
	}
	p := Plan{Services: map[string]Service{"primary": {Container: owner, Restart: "no"}}}
	respond(owner.Token, true)
	original := Command{Argv: []string{"/fixture/bootstrap", "run", "test"}, Environment: map[string]string{"APP": "original"}}
	joined, e := joinNetwork(p, "primary", borrower, original)
	if e != nil || joined.Environment["CHAINMAN_CONTAINER_NETWORK"] != id || original.Environment["CHAINMAN_CONTAINER_NETWORK"] != "" {
		t.Fatalf("network did not use immutable ID without mutating caller: %+v %v", joined, e)
	}
	data := Command{Argv: []string{engine, "run", "--rm", "image@sha256:fixture"}}
	joined, e = joinNetwork(p, "primary", borrower, data)
	if e != nil || strings.Join(joined.Argv, " ") != engine+" run --network container:"+id+" --rm image@sha256:fixture" {
		t.Fatalf("data container network arguments changed: %+v %v", joined, e)
	}
	for _, c := range []struct {
		token   string
		running bool
	}{{"replacement", true}, {owner.Token, false}} {
		respond(c.token, c.running)
		if _, e := joinNetwork(p, "primary", borrower, original); e == nil {
			t.Fatal("borrowed a replaced or stopped owner's namespace")
		}
	}
}

func TestNetworkBorrowingUsesSavedRepositoryOwner(t *testing.T) {
	state := t.TempDir()
	owner := &Container{Engine: "/fixture/docker", Name: "chainman-db", Token: strings.Repeat("a", 32)}
	saved := Plan{State: state, Services: map[string]Service{"database": {Container: owner, Restart: "no"}}}
	if e := atomic(filepath.Join(state, "plan.json"), saved); e != nil {
		t.Fatal(e)
	}
	p := Plan{Resources: []Plan{{State: state}}}
	got, e := networkOwner(p, "database")
	if e != nil || *got != *owner {
		t.Fatalf("repository network owner not resolved from saved state: %+v %v", got, e)
	}
	saved.Services["database"] = Service{Container: owner, Restart: "always"}
	if e = atomic(filepath.Join(state, "plan.json"), saved); e != nil {
		t.Fatal(e)
	}
	if _, e = networkOwner(p, "database"); e == nil {
		t.Fatal("accepted restartable network owner")
	}
}
