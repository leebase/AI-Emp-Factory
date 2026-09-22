package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"agent-board/internal/auth"
	"agent-board/internal/cli"
	"agent-board/internal/config"
	"agent-board/internal/employee"
	"agent-board/internal/httpapi"
	"agent-board/internal/roster"
	"agent-board/internal/services"
	"agent-board/internal/store"
	"agent-board/internal/worker"
)

var version = "dev"

// shutdownGrace bounds in-flight request draining on SIGINT/SIGTERM. It stays
// well inside a default systemd TimeoutStopSec so a stop never escalates.
const shutdownGrace = 10 * time.Second

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg, args, err := config.Load(os.Args[1:])
	if err != nil {
		exit(err)
	}
	if cfg.Version {
		fmt.Fprintln(os.Stdout, version)
		return
	}

	if len(args) > 0 && args[0] == "worker" {
		if err := worker.Run(ctx, cfg, args[1:], os.Stdout); err != nil {
			exit(err)
		}
		return
	}
	if len(args) > 0 && args[0] == "auth" {
		if err := cli.RunAuth(args[1:], os.Stdout); err != nil {
			exit(err)
		}
		return
	}
	st, err := store.Open(ctx, cfg.DBPath)
	if err != nil {
		exit(err)
	}
	defer st.Close()
	var employeeRegistry *employee.Registry
	if cfg.EmployeeRegistryDir != "" {
		employeeRegistry, err = employee.Load(cfg.EmployeeRegistryDir)
		if err != nil {
			exit(err)
		}
	}
	rosterFactoryDir := cfg.RosterFactoryDir
	if rosterFactoryDir == "" {
		rosterFactoryDir = cfg.EmployeeRegistryDir
	}
	monitor := roster.New(roster.Options{
		FactoryRegistryDir:  rosterFactoryDir,
		AutoOrchMissionsDir: cfg.RosterMissionsDir,
		HermesComposeFile:   cfg.RosterHermesCompose,
	})
	board := services.NewBoardWithRoster(st, employeeRegistry, monitor)
	// The quick shared state artifact is published to disk so a restart serves
	// validated last-known state instead of nothing.
	board.SetActiveStatePath(cfg.ActiveStateFile)

	if len(args) > 0 && args[0] == "server" {
		if cfg.PublicRead {
			log.Printf("WARNING: public read enabled for dashboard, board read APIs (snapshot, status, projects, machines, agents, tasks, events, leases, metrics, auto-orch reports), and artifact downloads")
		}
		if err := board.Migrate(ctx); err != nil {
			exit(err)
		}
		authn, err := auth.LoadFile(cfg.AuthConfigFile, cfg.AuthSessionTTL, !cfg.AuthInsecureCookies, board)
		if err != nil {
			exit(fmt.Errorf("local auth setup: %w", err))
		}
		api := httpapi.New(cfg, board, authn)
		api.StartStaleSweeper(ctx)
		server := &http.Server{
			Addr:    cfg.Addr(),
			Handler: api.Handler(),
		}
		log.Printf("agent-board listening on http://%s", cfg.Addr())
		// signal.NotifyContext replaces the default terminate-on-SIGTERM
		// behavior, so the server must observe ctx itself. Without this the
		// process ignores SIGTERM entirely and `systemctl stop/restart` only
		// completes when systemd escalates to SIGKILL after TimeoutStopSec --
		// which is exactly the restart step the cutover and rollback use.
		serveErr := make(chan error, 1)
		go func() {
			serveErr <- server.ListenAndServe()
		}()
		select {
		case err := <-serveErr:
			if err != nil && err != http.ErrServerClosed {
				exit(err)
			}
		case <-ctx.Done():
			log.Printf("agent-board shutting down")
			shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
			defer cancel()
			if err := server.Shutdown(shutdownCtx); err != nil {
				// A drain that overruns the grace period still terminates; the
				// supervisor must not be left waiting for SIGKILL.
				log.Printf("agent-board shutdown did not drain cleanly: %v", err)
				_ = server.Close()
			}
			if err := <-serveErr; err != nil && err != http.ErrServerClosed {
				exit(err)
			}
		}
		return
	}

	if err := cli.Run(ctx, board, args, os.Stdout); err != nil {
		exit(err)
	}
}

func exit(err error) {
	fmt.Fprintln(os.Stderr, "error:", err)
	os.Exit(1)
}
