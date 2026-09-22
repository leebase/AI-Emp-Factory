package httpapi

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"agent-board/internal/services"
)

// activeState is the authenticated local consumer seam for the Sprint 3A quick
// shared state artifact. Like the managerial routes it is never public-read and
// is denied to scoped observation producers by the default-deny seam, so only an
// authenticated operator identity can read estate state.
//
// A plain read never collects. `?refresh=1` asks for one bounded coalesced
// collection; overlapping refresh requests share it. Every response, in either
// representation, identifies the snapshot it came from and states its staleness
// and age, so two readers can always tell whether they hold the same artifact.
func (s *Server) activeState(w http.ResponseWriter, r *http.Request) {
	refresh, err := activeStateRefreshRequested(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	format := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("format")))
	if format == "" {
		format = "json"
	}
	if format != "json" && format != "markdown" {
		writeError(w, http.StatusBadRequest, fmt.Errorf("format must be json or markdown"))
		return
	}
	cache := s.board.ActiveState()
	var read services.ActiveStateRead
	if refresh {
		read, err = cache.Refresh(r.Context())
	} else {
		read, err = cache.Read(r.Context())
	}
	if err != nil {
		// No trustworthy artifact exists and collection failed. An empty
		// success is never invented in its place.
		writeError(w, http.StatusServiceUnavailable, err)
		return
	}
	writeActiveStateHeaders(w, read)
	if format == "markdown" {
		w.Header().Set("Content-Type", "text/markdown; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(activeStateMarkdownBanner(read) + read.Artifact.Markdown))
		return
	}
	body := map[string]any{
		"snapshot_id":  read.Artifact.SnapshotID(),
		"stale":        read.Stale,
		"age_seconds":  read.AgeSeconds,
		"collected":    read.Collected,
		"active_state": json.RawMessage(read.Artifact.JSON),
	}
	if read.RefreshError != "" {
		body["refresh_error"] = read.RefreshError
		body["refresh_error_at"] = read.RefreshErrorAt
	}
	if read.PersistError != "" {
		body["persist_error"] = read.PersistError
	}
	writeJSON(w, http.StatusOK, body)
}

func writeActiveStateHeaders(w http.ResponseWriter, read services.ActiveStateRead) {
	w.Header().Set("X-Active-State-Snapshot", read.Artifact.SnapshotID())
	w.Header().Set("X-Active-State-Stale", strconv.FormatBool(read.Stale))
	w.Header().Set("X-Active-State-Age-Seconds", strconv.FormatFloat(read.AgeSeconds, 'f', 3, 64))
	if read.RefreshError != "" {
		w.Header().Set("X-Active-State-Refresh-Error", "true")
	}
}

// activeStateMarkdownBanner states currency before any managerial content, so a
// stale Markdown answer can never be mistaken for current truth.
func activeStateMarkdownBanner(read services.ActiveStateRead) string {
	var b strings.Builder
	if read.Stale {
		fmt.Fprintf(&b, "> STALE last-known state: %.0f seconds old, valid_until %s has passed. This is not current truth.\n",
			read.AgeSeconds, read.Artifact.State.ValidUntil.Format("2006-01-02T15:04:05Z07:00"))
	} else {
		fmt.Fprintf(&b, "> Current state: %.0f seconds old, valid until %s.\n",
			read.AgeSeconds, read.Artifact.State.ValidUntil.Format("2006-01-02T15:04:05Z07:00"))
	}
	if read.RefreshError != "" {
		fmt.Fprintf(&b, "> Last refresh failed at %s: %s\n", read.RefreshErrorAt.Format("2006-01-02T15:04:05Z07:00"), read.RefreshError)
	}
	fmt.Fprintf(&b, "> snapshot_id: %s\n\n", read.Artifact.SnapshotID())
	return b.String()
}

func activeStateRefreshRequested(r *http.Request) (bool, error) {
	raw := strings.TrimSpace(r.URL.Query().Get("refresh"))
	if raw == "" {
		return false, nil
	}
	value, err := strconv.ParseBool(raw)
	if err != nil {
		return false, fmt.Errorf("refresh must be a boolean")
	}
	return value, nil
}
