package config

import (
	"bufio"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type Config struct {
	BindHost            string
	Port                int
	DBPath              string
	AuthConfigFile      string
	AuthSessionTTL      time.Duration
	AuthInsecureCookies bool
	MachineSecretFile   string
	EmployeeRegistryDir string
	RosterFactoryDir    string
	RosterMissionsDir   string
	// RosterHermesCompose is the Hermes compose path, or "off" to disable the
	// Hermes roster source.
	RosterHermesCompose string
	// StaffViewEnabled opts this deployment in to the read-only staff view. It
	// is off by default so enabling it on one private-LAN preview host cannot
	// turn it on anywhere else.
	StaffViewEnabled       bool
	StaffRelationshipsFile string
	// StaffBoardDBPath is an optional operator-configured native Board SQLite
	// database, opened read-only by the staff view. It is separate from DBPath:
	// the board's own database is the deployment's mutable store, while this is
	// an external source read for facts. Empty means no Board facts are read,
	// never that an employee has no Board work.
	StaffBoardDBPath string
	// StaffOperatorID is the human actor id whose direct Board asks appear as
	// "Needs you" in the Staff view. It is deployment configuration, never a
	// request parameter. Empty Config values retain the historical Lee default.
	StaffOperatorID      string
	PublicRead           bool
	ArtifactDir          string
	ActiveStateFile      string
	StaleInterval        time.Duration
	PresenceWindow       time.Duration
	ReviewSingleApproval bool
	Version              bool
}

func Default() Config {
	return Config{
		BindHost:       "127.0.0.1",
		Port:           8787,
		DBPath:         "/var/lib/agent-board/agent-board.db",
		AuthConfigFile: "/var/lib/agent-board/auth.json",
		AuthSessionTTL: 2 * time.Hour,
		ArtifactDir:    "/var/lib/agent-board/artifacts",
		// Restart-surviving location for the Mission Control quick shared state
		// artifact. An empty value keeps the artifact in memory only.
		ActiveStateFile: "/var/lib/agent-board/active-state.json",
		StaleInterval:   time.Minute,
		StaffOperatorID: "lee",
	}
}

func Load(args []string) (Config, []string, error) {
	cfg := Default()
	fs := flag.NewFlagSet("agent-board", flag.ContinueOnError)
	configPath := fs.String("config", "", "config file path")
	bindHost := fs.String("bind-host", "", "bind host")
	port := fs.Int("port", 0, "port")
	dbPath := fs.String("db", "", "SQLite database path")
	authConfigFile := fs.String("auth-config-file", "", "path to the mode-0600 local authentication configuration")
	authSessionTTL := fs.String("auth-session-ttl", "", "human session timeout")
	authInsecureCookies := fs.Bool("auth-insecure-cookies", false, "dev/test only: allow local auth cookies over plain HTTP")
	machineSecretFile := fs.String("machine-secret-file", "", "path to the mode-0600 machine API secret file for workers")
	employeeRegistryDir := fs.String("employee-registry-dir", "", "directory containing canonical employee deployment records")
	rosterFactoryDir := fs.String("roster-factory-dir", "", "factory registry records directory for live employee roster")
	rosterMissionsDir := fs.String("roster-missions-dir", "", "Auto-Orch missions directory for live employee roster")
	rosterHermesCompose := fs.String("roster-hermes-compose", "", "Hermes employee compose file for live employee roster, or off to disable Hermes")
	staffView := fs.String("staff-view", "", "enable the read-only staff view (off by default)")
	staffRelationshipsFile := fs.String("staff-relationships-file", "", "explicit source-attributed employee relationship file for the staff view")
	staffBoardDB := fs.String("staff-board-db", "", "operator-configured native Board SQLite database read read-only for staff facts")
	staffOperatorID := fs.String("staff-operator-id", "", "human actor id whose Board asks appear in the Staff view")
	publicRead := fs.Bool("public-read", false, "allow unauthenticated read-only routes")
	artifactDir := fs.String("artifact-dir", "", "directory for board-hosted artifact uploads")
	staleInterval := fs.String("stale-interval", "", "stale lease sweep interval")
	presenceWindow := fs.String("presence-window", "", "offline presence decay window")
	reviewSingleApproval := fs.Bool("review-single-approval", false, "one review approval moves a task to done")
	version := fs.Bool("version", false, "print version and exit")
	if err := fs.Parse(args); err != nil {
		return cfg, nil, err
	}

	if path := discoverConfig(*configPath); path != "" {
		values, err := readEnvFile(path)
		if err != nil {
			return cfg, nil, err
		}
		applyValues(&cfg, values)
	}
	applyEnv(&cfg)
	if *bindHost != "" {
		cfg.BindHost = *bindHost
	}
	if *port != 0 {
		cfg.Port = *port
	}
	if *dbPath != "" {
		cfg.DBPath = *dbPath
	}
	if *authConfigFile != "" {
		cfg.AuthConfigFile = *authConfigFile
	}
	if *authSessionTTL != "" {
		if ttl, ok := parseDuration(*authSessionTTL); ok {
			cfg.AuthSessionTTL = ttl
		}
	}
	if flagWasPassed(fs, "auth-insecure-cookies") {
		cfg.AuthInsecureCookies = *authInsecureCookies
	}
	if *machineSecretFile != "" {
		cfg.MachineSecretFile = *machineSecretFile
	}
	if *employeeRegistryDir != "" {
		cfg.EmployeeRegistryDir = *employeeRegistryDir
	}
	if *rosterFactoryDir != "" {
		cfg.RosterFactoryDir = *rosterFactoryDir
	}
	if *rosterMissionsDir != "" {
		cfg.RosterMissionsDir = *rosterMissionsDir
	}
	if *rosterHermesCompose != "" {
		cfg.RosterHermesCompose = *rosterHermesCompose
	}
	if *staffView != "" {
		if parsed, ok := parseBool(*staffView); ok {
			cfg.StaffViewEnabled = parsed
		}
	}
	if *staffRelationshipsFile != "" {
		cfg.StaffRelationshipsFile = *staffRelationshipsFile
	}
	if *staffBoardDB != "" {
		cfg.StaffBoardDBPath = *staffBoardDB
	}
	if *staffOperatorID != "" {
		cfg.StaffOperatorID = *staffOperatorID
	}
	if flagWasPassed(fs, "public-read") {
		cfg.PublicRead = *publicRead
	}
	if *artifactDir != "" {
		cfg.ArtifactDir = *artifactDir
	}
	if *staleInterval != "" {
		if interval, ok := parseDuration(*staleInterval); ok {
			cfg.StaleInterval = interval
		}
	}
	if *presenceWindow != "" {
		if window, ok := parseDuration(*presenceWindow); ok {
			cfg.PresenceWindow = window
		}
	}
	if flagWasPassed(fs, "review-single-approval") {
		cfg.ReviewSingleApproval = *reviewSingleApproval
	}
	cfg.Version = *version
	if _, err := cfg.EffectiveStaffOperatorID(); err != nil {
		return cfg, nil, err
	}
	return cfg, fs.Args(), nil
}

var staffOperatorIDPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*$`)

// EffectiveStaffOperatorID returns the configured Staff-view human identity.
// The identifier grammar matches the Board's existing opaque identifier
// convention. A zero-value Config remains source-compatible and selects Lee.
func (c Config) EffectiveStaffOperatorID() (string, error) {
	operatorID := strings.TrimSpace(c.StaffOperatorID)
	if operatorID == "" {
		return "lee", nil
	}
	if !staffOperatorIDPattern.MatchString(operatorID) {
		return "", fmt.Errorf("malformed staff operator id %q", c.StaffOperatorID)
	}
	return operatorID, nil
}

func (c Config) Addr() string {
	return fmt.Sprintf("%s:%d", c.BindHost, c.Port)
}

// EffectivePresenceWindow is intentionally derived from the sweep interval
// when it is not explicitly configured. This keeps presence decay at least
// two polling/sweep periods behind, and never below five minutes.
func (c Config) EffectivePresenceWindow() time.Duration {
	if c.PresenceWindow > 0 {
		return c.PresenceWindow
	}
	window := 2 * c.StaleInterval
	if window < 5*time.Minute {
		return 5 * time.Minute
	}
	return window
}

var executable = os.Executable

func discoverConfig(explicit string) string {
	if explicit != "" {
		return explicit
	}
	if exe, err := executable(); err == nil {
		path := filepath.Join(filepath.Dir(exe), "agent-board.env")
		if fileExists(path) {
			return path
		}
	}
	if fileExists("agent-board.env") {
		return "agent-board.env"
	}
	return ""
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func readEnvFile(path string) (map[string]string, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	values := map[string]string{}
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			return nil, fmt.Errorf("%s: invalid config line %q", path, line)
		}
		values[strings.TrimSpace(key)] = strings.Trim(strings.TrimSpace(value), `"'`)
	}
	return values, scanner.Err()
}
func applyEnv(cfg *Config) {
	values := map[string]string{}
	for _, key := range []string{
		"AGENT_BOARD_BIND_HOST",
		"AGENT_BOARD_PORT",
		"AGENT_BOARD_DB",
		"AGENT_BOARD_AUTH_CONFIG_FILE",
		"AGENT_BOARD_AUTH_SESSION_TTL",
		"AGENT_BOARD_AUTH_INSECURE_COOKIES",
		"AGENT_BOARD_MACHINE_SECRET_FILE",
		"AGENT_BOARD_EMPLOYEE_REGISTRY_DIR",
		"AGENT_BOARD_ROSTER_FACTORY_DIR",
		"AGENT_BOARD_ROSTER_MISSIONS_DIR",
		"AGENT_BOARD_ROSTER_HERMES_COMPOSE",
		"AGENT_BOARD_STAFF_VIEW",
		"AGENT_BOARD_STAFF_RELATIONSHIPS_FILE",
		"AGENT_BOARD_STAFF_BOARD_DB",
		"AGENT_BOARD_STAFF_OPERATOR_ID",
		"AGENT_BOARD_PUBLIC_READ",
		"AGENT_BOARD_ARTIFACT_DIR",
		"AGENT_BOARD_ACTIVE_STATE_FILE",
		"AGENT_BOARD_STALE_INTERVAL",
		"AGENT_BOARD_PRESENCE_WINDOW",
		"AGENT_BOARD_REVIEW_SINGLE_APPROVAL",
	} {
		if value, ok := os.LookupEnv(key); ok {
			values[key] = value
		}
	}
	applyValues(cfg, values)
}

func applyValues(cfg *Config, values map[string]string) {
	if value := values["AGENT_BOARD_BIND_HOST"]; value != "" {
		cfg.BindHost = value
	}
	if value := values["AGENT_BOARD_PORT"]; value != "" {
		if port, err := strconv.Atoi(value); err == nil {
			cfg.Port = port
		}
	}
	if value := values["AGENT_BOARD_DB"]; value != "" {
		cfg.DBPath = value
	}
	if value := values["AGENT_BOARD_AUTH_CONFIG_FILE"]; value != "" {
		cfg.AuthConfigFile = value
	}
	if value := values["AGENT_BOARD_AUTH_SESSION_TTL"]; value != "" {
		if ttl, ok := parseDuration(value); ok {
			cfg.AuthSessionTTL = ttl
		}
	}
	if value := values["AGENT_BOARD_AUTH_INSECURE_COOKIES"]; value != "" {
		if parsed, ok := parseBool(value); ok {
			cfg.AuthInsecureCookies = parsed
		}
	}
	if value := values["AGENT_BOARD_MACHINE_SECRET_FILE"]; value != "" {
		cfg.MachineSecretFile = value
	}
	if value := values["AGENT_BOARD_EMPLOYEE_REGISTRY_DIR"]; value != "" {
		cfg.EmployeeRegistryDir = value
	}
	if value := values["AGENT_BOARD_ROSTER_FACTORY_DIR"]; value != "" {
		cfg.RosterFactoryDir = value
	}
	if value := values["AGENT_BOARD_ROSTER_MISSIONS_DIR"]; value != "" {
		cfg.RosterMissionsDir = value
	}
	if value := values["AGENT_BOARD_ROSTER_HERMES_COMPOSE"]; value != "" {
		cfg.RosterHermesCompose = value
	}
	if value := values["AGENT_BOARD_STAFF_VIEW"]; value != "" {
		if parsed, ok := parseBool(value); ok {
			cfg.StaffViewEnabled = parsed
		}
	}
	if value := values["AGENT_BOARD_STAFF_RELATIONSHIPS_FILE"]; value != "" {
		cfg.StaffRelationshipsFile = value
	}
	if value := values["AGENT_BOARD_STAFF_BOARD_DB"]; value != "" {
		cfg.StaffBoardDBPath = value
	}
	if value := values["AGENT_BOARD_STAFF_OPERATOR_ID"]; value != "" {
		cfg.StaffOperatorID = value
	}
	if value := values["AGENT_BOARD_PUBLIC_READ"]; value != "" {
		if parsed, ok := parseBool(value); ok {
			cfg.PublicRead = parsed
		}
	}
	if value := values["AGENT_BOARD_ARTIFACT_DIR"]; value != "" {
		cfg.ArtifactDir = value
	}
	if value := values["AGENT_BOARD_ACTIVE_STATE_FILE"]; value != "" {
		cfg.ActiveStateFile = value
	}
	if value := values["AGENT_BOARD_STALE_INTERVAL"]; value != "" {
		if interval, ok := parseDuration(value); ok {
			cfg.StaleInterval = interval
		}
	}
	if value := values["AGENT_BOARD_PRESENCE_WINDOW"]; value != "" {
		if window, ok := parseDuration(value); ok {
			cfg.PresenceWindow = window
		}
	}
	if value := values["AGENT_BOARD_REVIEW_SINGLE_APPROVAL"]; value != "" {
		if parsed, ok := parseBool(value); ok {
			cfg.ReviewSingleApproval = parsed
		}
	}
}

// ReadSecretFile reads a machine secret from a regular mode-0600 file. The
// descriptor is validated after opening and symlinks are rejected.
func ReadSecretFile(path string) (string, error) {
	// Open once and validate the opened descriptor (not the path) to close the
	// stat-then-read TOCTOU window; O_NOFOLLOW rejects a symlinked token file.
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return "", fmt.Errorf("read secret file %q: %w", path, err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return "", fmt.Errorf("read secret file %q: %w", path, err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		return "", fmt.Errorf("secret file %q must be a regular mode-0600 file", path)
	}
	contents, err := io.ReadAll(file)
	if err != nil {
		return "", fmt.Errorf("read secret file %q: %w", path, err)
	}
	token := strings.TrimSpace(string(contents))
	if token == "" {
		return "", fmt.Errorf("secret file %q is empty", path)
	}
	return token, nil
}

func parseDuration(value string) (time.Duration, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0, false
	}
	if duration, err := time.ParseDuration(value); err == nil {
		return duration, true
	}
	seconds, err := strconv.Atoi(value)
	if err != nil {
		return 0, false
	}
	return time.Duration(seconds) * time.Second, true
}

func parseBool(value string) (bool, bool) {
	parsed, err := strconv.ParseBool(strings.TrimSpace(value))
	return parsed, err == nil
}

func flagWasPassed(fs *flag.FlagSet, name string) bool {
	found := false
	fs.Visit(func(flag *flag.Flag) {
		if flag.Name == name {
			found = true
		}
	})
	return found
}
