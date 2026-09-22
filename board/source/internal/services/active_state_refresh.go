package services

import (
	"context"
	"fmt"
	"time"

	"agent-board/internal/operations"
)

// Refresh generation machinery for the quick shared state artifact.
//
// One generation at a time owns collection. It owns its deadline too: the wait
// ends on that deadline whether or not the collector honours its context, and a
// result that arrives afterwards is discarded rather than published.

// beginRefresh returns the in-flight generation when one exists, so ten
// overlapping callers cause exactly one collection.
func (c *ActiveStateCache) beginRefresh() *activeStateRefresh {
	c.mu.Lock()
	// Valid last-known disk state is adopted before any collection is
	// attempted, so a collection failure after restart preserves it.
	c.ensureLoadedLocked()
	if c.inflight != nil {
		r := c.inflight
		c.mu.Unlock()
		return r
	}
	r := &activeStateRefresh{done: make(chan struct{})}
	if c.collecting {
		r.err = errOutstandingCollection
		c.lastErr = r.err
		c.lastErrAt = c.now()
		c.mu.Unlock()
		close(r.done)
		return r
	}
	c.collecting = true
	c.inflight = r
	c.collections++
	c.mu.Unlock()
	go c.runRefresh(r)
	return r
}

type activeStateCollection struct {
	fleet operations.ManagerialFleet
	err   error
}

// runRefresh enforces the generation's own deadline. The collector is given a
// cancellable context, and that context's deadline is the generation's single
// authority: the wait ends when it expires whether or not the collector honours
// it, and a result produced at or after expiry is dropped on the floor rather
// than published. A second clock started later would expire at a different
// instant and let such a result win the race.
func (c *ActiveStateCache) runRefresh(r *activeStateRefresh) {
	ctx, cancel := context.WithTimeout(context.Background(), c.timeout)
	// runRefresh owns the cancel. If the collector cancelled on its own return,
	// a normal completion would masquerade as an expired deadline.
	defer cancel()
	// Buffered by one so an abandoned collector's send never blocks and never
	// keeps the goroutine alive waiting for a receiver that has moved on.
	results := make(chan activeStateCollection, 1)
	go func() {
		fleet, err := c.collect(ctx)
		results <- activeStateCollection{fleet: fleet, err: err}
		c.mu.Lock()
		c.collecting = false
		c.mu.Unlock()
	}()

	var (
		artifact *ActiveStateArtifact
		err      error
	)
	select {
	case res := <-results:
		if ctx.Err() != nil {
			// The result exists, but this generation's authority had already
			// expired when it arrived, so it cannot publish.
			err = fmt.Errorf("active state collection exceeded its %s deadline", c.timeout)
			break
		}
		err = res.err
		if err == nil {
			artifact, err = buildActiveStateArtifact(res.fleet, c.now())
		}
	case <-ctx.Done():
		err = fmt.Errorf("active state collection exceeded its %s deadline", c.timeout)
	}
	c.finishRefresh(r, artifact, err)
}

// finishRefresh publishes exactly one generation's outcome. Disk I/O runs before
// the lock is taken, so no warm read ever waits on a disk, while the ordering
// and the persistence receipt are still those of this generation.
func (c *ActiveStateCache) finishRefresh(r *activeStateRefresh, artifact *ActiveStateArtifact, err error) {
	persistErr := ""
	if err == nil {
		if perr := c.persist(artifact); perr != nil {
			persistErr = perr.Error()
		}
	}
	c.mu.Lock()
	if err != nil {
		c.lastErr = err
		c.lastErrAt = c.now()
	} else {
		c.current = artifact
		c.lastErr = nil
		c.lastErrAt = time.Time{}
		c.persistErr = persistErr
		c.loaded = true
	}
	r.err = err
	c.inflight = nil
	c.mu.Unlock()
	close(r.done)
}

// buildActiveStateArtifact derives the canonical object once and renders both
// representations from that exact value.
func buildActiveStateArtifact(fleet operations.ManagerialFleet, now time.Time) (*ActiveStateArtifact, error) {
	state, err := operations.BuildActiveState(fleet)
	if err != nil {
		return nil, err
	}
	if err := state.Validate(); err != nil {
		return nil, err
	}
	encoded, err := state.CanonicalJSON()
	if err != nil {
		return nil, err
	}
	return &ActiveStateArtifact{State: state, JSON: encoded, Markdown: state.Markdown(), PublishedAt: now.UTC()}, nil
}

// ActiveState returns the process-wide cache for this board, created once.
func (b *Board) ActiveState() *ActiveStateCache {
	b.activeStateOnce.Do(func() {
		b.activeState = NewActiveStateCache(b.collectActiveState, b.activeStatePath)
	})
	return b.activeState
}

// SetActiveStatePath selects the disk location for the published artifact. It
// must be called before the first ActiveState use; afterwards it is ignored.
func (b *Board) SetActiveStatePath(path string) { b.activeStatePath = path }

func (b *Board) collectActiveState(ctx context.Context) (operations.ManagerialFleet, error) {
	return b.ManagerialFleet(ctx, operations.DefaultHistoryLimit)
}
