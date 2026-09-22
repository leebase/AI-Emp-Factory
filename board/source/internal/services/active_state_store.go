package services

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"agent-board/internal/operations"
)

// Disk state for the quick shared artifact.
//
// Publication is atomic: the canonical bytes are written to a temporary file in
// the destination directory, synced, and renamed over the target, so a reader or
// a restart never sees a half-written artifact. Loading fails closed: content
// that does not parse, does not validate, or does not re-derive its own snapshot
// digest is rejected and recorded, never served as truth.

// persist writes the artifact atomically. It deliberately holds no cache lock:
// it touches only the immutable artifact and the fixed path, so a slow disk
// cannot block a warm read. A persistence failure is a receipt, not a truth
// failure: the in-memory artifact stays authoritative.
func (c *ActiveStateCache) persist(artifact *ActiveStateArtifact) error {
	if c.path == "" {
		return nil
	}
	dir := filepath.Dir(c.path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("active state directory: %w", err)
	}
	tmp, err := os.CreateTemp(dir, ".active-state-*.json")
	if err != nil {
		return fmt.Errorf("active state temp file: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := writeArtifactFile(tmp, artifact.JSON); err != nil {
		return err
	}
	if err := os.Rename(tmpName, c.path); err != nil {
		return fmt.Errorf("active state publish: %w", err)
	}
	return nil
}

func writeArtifactFile(f *os.File, payload []byte) error {
	defer f.Close()
	if err := f.Chmod(0o600); err != nil {
		return fmt.Errorf("active state permissions: %w", err)
	}
	if _, err := f.Write(payload); err != nil {
		return fmt.Errorf("active state write: %w", err)
	}
	if err := f.Sync(); err != nil {
		return fmt.Errorf("active state sync: %w", err)
	}
	return nil
}

// ensureLoadedLocked adopts persisted state at most once per process. A missing
// file is an ordinary cold start. Any other defect is recorded and the cache
// stays empty, so a corrupt file can never be served.
func (c *ActiveStateCache) ensureLoadedLocked() {
	if c.loaded {
		return
	}
	c.loaded = true
	if c.path == "" || c.current != nil {
		return
	}
	artifact, err := loadActiveStateFile(c.path)
	if err != nil {
		if !os.IsNotExist(err) {
			c.persistErr = "rejected persisted active state: " + err.Error()
		}
		return
	}
	c.current = artifact
}

// loadActiveStateFile validates schema, content and digest before trust.
func loadActiveStateFile(path string) (*ActiveStateArtifact, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var state operations.ActiveState
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&state); err != nil {
		return nil, fmt.Errorf("decode: %w", err)
	}
	if decoder.More() {
		return nil, fmt.Errorf("decode: trailing content after active state object")
	}
	if err := state.Validate(); err != nil {
		return nil, err
	}
	encoded, err := state.CanonicalJSON()
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	return &ActiveStateArtifact{State: state, JSON: encoded, Markdown: state.Markdown(), PublishedAt: info.ModTime().UTC()}, nil
}
