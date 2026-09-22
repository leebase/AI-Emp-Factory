package cli

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"strings"
	"time"

	"agent-board/internal/services"
)

// runActiveState is the local CLI consumer seam for the Sprint 3A quick shared
// state artifact. Without --refresh it performs no collection at all: it serves
// the published artifact and states its snapshot id, age and staleness. With
// --refresh it joins or starts exactly one bounded coalesced collection.
func runActiveState(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("active-state", flag.ContinueOnError)
	fs.SetOutput(stdout)
	format := fs.String("format", "json", "output format: json or markdown")
	refresh := fs.Bool("refresh", false, "request one bounded coalesced refresh before reading")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *format != "json" && *format != "markdown" {
		return errors.New("--format must be json or markdown")
	}
	cache := b.ActiveState()
	var (
		read services.ActiveStateRead
		err  error
	)
	if *refresh {
		read, err = cache.Refresh(ctx)
	} else {
		read, err = cache.Read(ctx)
	}
	if err != nil {
		return err
	}
	if *format == "markdown" {
		if _, err := io.WriteString(stdout, activeStateCLIBanner(read)); err != nil {
			return err
		}
		_, err = fmt.Fprint(stdout, read.Artifact.Markdown)
		return err
	}
	return writeActiveStateJSON(stdout, read)
}

// activeStateCLIBanner states currency before any managerial content, so a stale
// Markdown answer cannot be mistaken for current truth.
func activeStateCLIBanner(read services.ActiveStateRead) string {
	currency := "current"
	if read.Stale {
		currency = "STALE last-known state, not current truth"
	}
	var b strings.Builder
	fmt.Fprintf(&b, "# snapshot_id=%s %s age_seconds=%.3f collected=%t\n",
		read.Artifact.SnapshotID(), currency, read.AgeSeconds, read.Collected)
	if read.RefreshError != "" {
		fmt.Fprintf(&b, "# last refresh failed at %s: %s\n", read.RefreshErrorAt.Format(time.RFC3339), read.RefreshError)
	}
	if read.PersistError != "" {
		fmt.Fprintf(&b, "# persistence receipt: %s\n", read.PersistError)
	}
	return b.String()
}

// writeActiveStateJSON emits one machine-readable object. The currency and error
// receipts are fields of that object rather than comment lines, so the output
// parses as JSON without being stripped first.
//
// The canonical artifact bytes are embedded by reference and are never appended
// to: append would write through the shared backing array of the cached
// artifact, which every consumer must treat as read-only.
func writeActiveStateJSON(stdout io.Writer, read services.ActiveStateRead) error {
	envelope := map[string]any{
		"snapshot_id":  read.Artifact.SnapshotID(),
		"stale":        read.Stale,
		"age_seconds":  read.AgeSeconds,
		"collected":    read.Collected,
		"active_state": json.RawMessage(read.Artifact.JSON),
	}
	if read.RefreshError != "" {
		envelope["refresh_error"] = read.RefreshError
		envelope["refresh_error_at"] = read.RefreshErrorAt.Format(time.RFC3339Nano)
	}
	if read.PersistError != "" {
		envelope["persist_error"] = read.PersistError
	}
	encoded, err := json.Marshal(envelope)
	if err != nil {
		return err
	}
	if _, err := stdout.Write(encoded); err != nil {
		return err
	}
	_, err = io.WriteString(stdout, "\n")
	return err
}
