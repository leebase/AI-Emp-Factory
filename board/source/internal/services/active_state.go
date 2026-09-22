package services

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"agent-board/internal/operations"
)

// Sprint 3A quick shared state artifact cache.
//
// Invariants implemented here:
//   - A published artifact is immutable. Publication is a single pointer swap
//     under one lock, so a concurrent reader observes either the whole previous
//     artifact or the whole new one, never a partial or mixed pair.
//   - A warm read performs no collection at all.
//   - An expired read promptly returns the last known artifact explicitly marked
//     stale, with its age and the last refresh error, and never claims currency.
//   - Overlapping refreshes join one in-flight collection generation.
//   - Runtime authority for a shared collection belongs to this cache, not to any
//     one caller: a caller may abandon its wait, but cannot cancel a collection
//     other waiters depend on.
//   - A failed refresh retains the previous artifact plus a visible error
//     receipt. With no artifact at all, a failed refresh returns an error; it
//     never fabricates an empty success.
//   - Deadline authority belongs to the generation, not to the collector. The
//     bounded wait always ends, with a failure receipt, even if the collector
//     ignores its context; a result that arrives after that deadline is
//     discarded and can never mutate published state.
//   - Persisted last-known state is adopted before any collection is attempted,
//     including on an explicit Refresh, so a failure after restart preserves it.
const defaultActiveStateRefreshTimeout = 30 * time.Second

// ActiveStateArtifact is one immutable published artifact: the canonical state,
// its exact published JSON bytes, and the Markdown derived from that same
// value. Nothing in it is modified after publication.
type ActiveStateArtifact struct {
	State       operations.ActiveState
	JSON        []byte
	Markdown    string
	PublishedAt time.Time
}

// SnapshotID is the content digest shared by the JSON and the Markdown.
func (a *ActiveStateArtifact) SnapshotID() string { return a.State.SnapshotID }

// ActiveStateRead is one read receipt. Stale is authoritative: a true value
// means the artifact is last-known state, not current truth.
type ActiveStateRead struct {
	Artifact       *ActiveStateArtifact
	Stale          bool
	AgeSeconds     float64
	Collected      bool
	RefreshError   string
	RefreshErrorAt time.Time
	PersistError   string
}

// ActiveStateCollector returns the accepted managerial fleet. The only source is
// the Board's own bounded authenticated database projection; no live external
// system is collected.
type ActiveStateCollector func(ctx context.Context) (operations.ManagerialFleet, error)

type activeStateRefresh struct {
	done chan struct{}
	err  error
}

// ActiveStateCache publishes and serves the quick shared state artifact.
type ActiveStateCache struct {
	collect ActiveStateCollector
	path    string
	timeout time.Duration
	now     func() time.Time

	mu          sync.Mutex
	current     *ActiveStateArtifact
	lastErr     error
	lastErrAt   time.Time
	persistErr  string
	inflight    *activeStateRefresh
	collections int
	loaded      bool
	// collecting is true while a collector goroutine from some generation has
	// still not returned. A collector that ignored its deadline is abandoned,
	// not awaited, but exactly one such goroutine may exist: further refreshes
	// fail fast with a receipt rather than stacking more of them.
	collecting bool
}

// errOutstandingCollection is the receipt for a refresh refused because a
// previous non-cooperative collector has not yet returned. Recovery is
// automatic: once that goroutine returns, the next refresh collects normally.
var errOutstandingCollection = errors.New("a previous active state collection has not returned; refusing to start another")

// NewActiveStateCache builds a cache over a collector. An empty path keeps the
// artifact in memory only; a non-empty path adds restart-surviving disk state
// that is validated before it is ever trusted.
func NewActiveStateCache(collect ActiveStateCollector, path string) *ActiveStateCache {
	return &ActiveStateCache{collect: collect, path: path, timeout: defaultActiveStateRefreshTimeout, now: func() time.Time { return time.Now().UTC() }}
}

// Collections reports how many collections this cache has started. Overlapping
// refreshes share one, which is what the coalescing guarantee means.
func (c *ActiveStateCache) Collections() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.collections
}

// Read returns the current artifact without collecting whenever one is
// available. An expired artifact is returned immediately, explicitly stale,
// while one bounded coalesced refresh is started in the background. Only a cold
// cache (nothing in memory and nothing trustworthy on disk) waits for a
// collection, and a failure there is an error rather than an empty success.
func (c *ActiveStateCache) Read(ctx context.Context) (ActiveStateRead, error) {
	c.mu.Lock()
	c.ensureLoadedLocked()
	if c.current != nil {
		read := c.readLocked(false)
		expired := read.Stale
		c.mu.Unlock()
		if expired {
			c.beginRefresh()
		}
		return read, nil
	}
	c.mu.Unlock()
	return c.Refresh(ctx)
}

// Refresh joins or starts exactly one collection generation and reports the
// resulting read. A failed collection keeps the previous artifact and surfaces
// the error receipt instead of replacing truth with an empty snapshot.
func (c *ActiveStateCache) Refresh(ctx context.Context) (ActiveStateRead, error) {
	r := c.beginRefresh()
	select {
	case <-r.done:
	case <-ctx.Done():
		// The caller abandons its wait; the shared collection continues for the
		// other waiters that depend on it.
		return ActiveStateRead{}, ctx.Err()
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.current == nil {
		if r.err == nil {
			return ActiveStateRead{}, fmt.Errorf("active state refresh published no artifact")
		}
		return ActiveStateRead{}, fmt.Errorf("active state unavailable and refresh failed: %w", r.err)
	}
	return c.readLocked(r.err == nil), nil
}

// readLocked builds the receipt for the currently published artifact.
func (c *ActiveStateCache) readLocked(collected bool) ActiveStateRead {
	now := c.now()
	read := ActiveStateRead{Artifact: c.current, Collected: collected, PersistError: c.persistErr}
	if c.lastErr != nil {
		read.RefreshError = c.lastErr.Error()
		read.RefreshErrorAt = c.lastErrAt
	}
	if c.current != nil {
		generated := c.current.State.GeneratedAt
		validUntil := c.current.State.ValidUntil
		age := now.Sub(generated).Seconds()
		switch {
		case age < 0:
			// Clock rollback. Currency cannot be established, so it fails
			// closed, and the age never reads as a negative "current" label.
			read.Stale = true
			read.AgeSeconds = 0
		case !validUntil.After(generated):
			// No fresh source backed this artifact, so it is last-known state
			// from its own generation instant onwards, never current.
			read.Stale = true
			read.AgeSeconds = age
		default:
			// An ordinary fresh source keeps inclusive expiry: the artifact is
			// still current at exactly valid_until, matching the owner rules.
			read.Stale = now.After(validUntil)
			read.AgeSeconds = age
		}
	}
	return read
}
