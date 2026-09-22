package roster

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	DefaultFactoryRegistryDir  = "/home/lee/projects/ai-employee-factory/hiring/registry/records"
	DefaultAutoOrchMissionsDir = "/home/lee/projects/auto-orch/missions"
	DefaultHermesComposeFile   = "/home/lee/projects/hermes/docker-compose.employees.yml"
)

type Options struct {
	FactoryRegistryDir  string
	AutoOrchMissionsDir string
	HermesComposeFile   string
	Now                 func() time.Time
	Crontab             func(context.Context) (string, error)
	DockerPS            func(context.Context) (string, error)
}

type Row struct {
	EmployeeID       string   `json:"employee_id"`
	Name             string   `json:"name"`
	Status           string   `json:"status"`
	CurrentStage     string   `json:"current_stage,omitempty"`
	Activity         string   `json:"activity,omitempty"`
	NextFire         string   `json:"next_fire,omitempty"`
	ScheduleRunnable bool     `json:"schedule_runnable"`
	LastOutcome      string   `json:"last_outcome,omitempty"`
	PauseReason      string   `json:"pause_reason,omitempty"`
	LoopState        string   `json:"loop_state,omitempty"`
	Cadence          string   `json:"cadence,omitempty"`
	LaunchApproved   *bool    `json:"launch_approved,omitempty"`
	LifecycleState   string   `json:"lifecycle_state,omitempty"`
	Mission          string   `json:"mission,omitempty"`
	Uptime           string   `json:"uptime,omitempty"`
	Sources          []string `json:"sources,omitempty"`
	// The fields below are additive, explicitly recorded values kept separate
	// from the derived Status/Activity prose above. A staff reader needs the
	// recorded fact and the file it was recorded in, not a classification.
	Paused              bool   `json:"paused,omitempty"`
	PauseActor          string `json:"pause_actor,omitempty"`
	PauseReasonText     string `json:"pause_reason_text,omitempty"`
	PausedAt            string `json:"paused_at,omitempty"`
	LastCycleEnd        string `json:"last_cycle_end,omitempty"`
	LastCycleReport     string `json:"last_cycle_report,omitempty"`
	NonDeliveringCycles string `json:"non_delivering_cycles,omitempty"`
	RegistryPath        string `json:"registry_path,omitempty"`
	ConfigPath          string `json:"config_path,omitempty"`
	StatePath           string `json:"state_path,omitempty"`
	CronMatchedOn       string `json:"cron_matched_on,omitempty"`
	// MissionPurpose is a bounded excerpt of the mission charter's own recorded
	// remit section. It is quoted, never summarised, and absent when the charter
	// has no listed section: a registration identifier is not a purpose.
	MissionPurpose string `json:"mission_purpose,omitempty"`
	MissionPath    string `json:"mission_path,omitempty"`
	// BoardAgentID, BoardMachineID and BoardRef are the employee's exact
	// registry-declared Board binding, copied verbatim from the Factory
	// deployment record's board_identity object. They are the only thing that
	// binds an employee to a Board row: a name, a task title or a mission
	// substring is never matched against them.
	BoardAgentID   string `json:"board_agent_id,omitempty"`
	BoardMachineID string `json:"board_machine_id,omitempty"`
	BoardRef       string `json:"board_ref,omitempty"`
	// CronExactBinding is true only when a crontab line names this mission as
	// an argument to the Auto-Orch CLI. A substring match is not a binding and
	// must never be presented as a scheduled run.
	CronExactBinding bool `json:"cron_exact_binding,omitempty"`
	// PauseConflict marks two equally current pause records that disagree.
	PauseConflict bool `json:"pause_conflict,omitempty"`
}
type Cycle struct {
	CycleID   string `json:"cycle_id"`
	Path      string `json:"path"`
	Outcome   string `json:"outcome,omitempty"`
	RunID     string `json:"run_id,omitempty"`
	RunStatus string `json:"run_status,omitempty"`
	RunPassed string `json:"run_passed,omitempty"`
	// The producer records the work it selected and its own delivery
	// disposition under evaluate_summary.value_assessment. These are read as
	// the producer's own fields; nothing here re-decides whether value landed.
	Title         string   `json:"title,omitempty"`
	ValueStatus   string   `json:"value_status,omitempty"`
	ValueReason   string   `json:"value_reason,omitempty"`
	CycleEnd      string   `json:"cycle_end,omitempty"`
	EvidencePath  string   `json:"evidence_path,omitempty"`
	EvidencePaths []string `json:"evidence_paths,omitempty"`
	Errors        []string `json:"errors,omitempty"`
}

type Detail struct {
	Row           Row      `json:"employee"`
	MissionText   string   `json:"mission_text,omitempty"`
	Config        string   `json:"config,omitempty"`
	State         string   `json:"state,omitempty"`
	RecentCycles  []Cycle  `json:"recent_cycles,omitempty"`
	Errors        []string `json:"errors,omitempty"`
	EvidencePaths []string `json:"evidence_paths,omitempty"`
}

type Snapshot struct {
	GeneratedAt  time.Time `json:"generated_at"`
	Employees    []Row     `json:"employees"`
	SourceErrors []string  `json:"source_errors,omitempty"`
}

type Monitor struct {
	opts Options
}

func New(opts Options) *Monitor {
	if opts.FactoryRegistryDir == "" {
		opts.FactoryRegistryDir = DefaultFactoryRegistryDir
	}
	if opts.AutoOrchMissionsDir == "" {
		opts.AutoOrchMissionsDir = DefaultAutoOrchMissionsDir
	}
	if opts.HermesComposeFile == "" {
		opts.HermesComposeFile = DefaultHermesComposeFile
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Crontab == nil {
		opts.Crontab = readCrontab
	}
	if opts.DockerPS == nil {
		opts.DockerPS = readDockerPS
	}
	return &Monitor{opts: opts}
}

func (m *Monitor) Snapshot(ctx context.Context) Snapshot {
	now := m.opts.Now().UTC()
	rows := map[string]*aggregate{}
	errorsOut := make([]string, 0)
	if err := m.readFactory(rows); err != nil {
		errorsOut = append(errorsOut, err.Error())
	}
	if _, err := m.readMissions(rows); err != nil {
		errorsOut = append(errorsOut, err.Error())
	}
	hermesNote, err := m.readHermes(rows)
	if hermesNote != "" {
		errorsOut = append(errorsOut, hermesNote)
	}
	if err != nil {
		errorsOut = append(errorsOut, err.Error())
	}
	m.readFactoryManifestos(rows)
	cronText, err := m.opts.Crontab(ctx)
	if err != nil {
		errorsOut = append(errorsOut, "cron: "+err.Error())
	}
	applyCron(rows, cronText, now)
	for _, a := range rows {
		finalize(a)
	}
	out := make([]Row, 0, len(rows))
	for _, a := range rows {
		out = append(out, a.row)
	}
	sort.Slice(out, func(i, j int) bool { return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name) })
	return Snapshot{GeneratedAt: now, Employees: out, SourceErrors: uniqueStrings(errorsOut)}
}

func (m *Monitor) Detail(ctx context.Context, name string) (Detail, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return Detail{}, errors.New("employee name is required")
	}
	// Detail is intentionally built from the same live adapters as the roster,
	// so a drill-down cannot silently describe a stale list.
	rows := map[string]*aggregate{}
	if err := m.readFactory(rows); err != nil { /* source absence is represented below */
	}
	if _, err := m.readMissions(rows); err != nil { /* source absence is represented below */
	}
	if _, err := m.readHermes(rows); err != nil { /* source absence is represented below */
	}
	m.readFactoryManifestos(rows)
	cronText, _ := m.opts.Crontab(ctx)
	applyCron(rows, cronText, m.opts.Now().UTC())
	var found *aggregate
	for _, a := range rows {
		if strings.EqualFold(a.row.EmployeeID, name) || strings.EqualFold(a.row.Name, name) || strings.EqualFold(a.missionName, name) {
			found = a
			break
		}
	}
	if found == nil {
		return Detail{}, fmt.Errorf("employee %q not found", name)
	}
	finalize(found)
	d := Detail{Row: found.row, MissionText: found.missionText, Config: found.config, State: found.state, RecentCycles: found.cycles, Errors: uniqueStrings(found.errors), EvidencePaths: uniqueStrings(found.evidence)}
	return d, nil
}

type aggregate struct {
	row             Row
	missionName     string
	missionText     string
	config          string
	state           string
	paused          bool
	pauseReason     string
	actor           string
	pausedAt        string
	lastCycleEnd    string
	nonDelivering   string
	loopState       string
	valueExhausted  bool
	lastOutcome     string
	lastRunStatus   string
	lastCycle       string
	cycles          []Cycle
	errors          []string
	evidence        []string
	cronLines       []cronEntry
	container       bool
	containerStatus string
	profile         bool
	factorySpecRef  string
	autoOrchMission bool
}

// readAll performs one read of every configured source. Snapshot, Detail and
// Fleet all go through it so a drill-down or a staff view can never describe a
// different read from the list it was opened from.
func (m *Monitor) readAll(ctx context.Context) (map[string]*aggregate, []string) {
	rows := map[string]*aggregate{}
	errorsOut := make([]string, 0)
	if err := m.readFactory(rows); err != nil {
		errorsOut = append(errorsOut, err.Error())
	}
	if _, err := m.readMissions(rows); err != nil {
		errorsOut = append(errorsOut, err.Error())
	}
	hermesNote, err := m.readHermes(rows)
	if hermesNote != "" {
		errorsOut = append(errorsOut, hermesNote)
	}
	if err != nil {
		errorsOut = append(errorsOut, err.Error())
	}
	m.readFactoryManifestos(rows)
	cronText, err := m.opts.Crontab(ctx)
	if err != nil {
		errorsOut = append(errorsOut, "cron: "+err.Error())
	}
	applyCron(rows, cronText, m.opts.Now().UTC())
	return rows, uniqueStrings(errorsOut)
}

// Fleet is one read of every employee together with its own bounded records.
// It exists so a per-employee view does not have to re-read every source once
// per employee, which would also let two employees be described from two
// different reads of the estate.
func (m *Monitor) Fleet(ctx context.Context) (time.Time, []Detail, []string) {
	now := m.opts.Now().UTC()
	rows, sourceErrors := m.readAll(ctx)
	out := make([]Detail, 0, len(rows))
	for _, a := range rows {
		finalize(a)
		out = append(out, Detail{Row: a.row, RecentCycles: a.cycles, Errors: uniqueStrings(a.errors), EvidencePaths: uniqueStrings(a.evidence)})
	}
	sort.Slice(out, func(i, j int) bool { return strings.ToLower(out[i].Row.Name) < strings.ToLower(out[j].Row.Name) })
	return now, out, sourceErrors
}

func (m *Monitor) readFactory(rows map[string]*aggregate) error {
	dir := strings.TrimSpace(m.opts.FactoryRegistryDir)
	if dir == "" {
		return errors.New("factory registry: directory is not configured")
	}
	paths, err := filepath.Glob(filepath.Join(dir, "*.json"))
	if err != nil {
		return fmt.Errorf("factory registry glob: %w", err)
	}
	if len(paths) == 0 {
		return fmt.Errorf("factory registry: no records in %s", dir)
	}
	var firstErr error
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("factory record %s: %w", path, err)
			}
			continue
		}
		var body map[string]any
		if err := json.Unmarshal(raw, &body); err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("factory record %s: %w", path, err)
			}
			continue
		}
		id := stringValue(body["employee_id"])
		if id == "" {
			id = strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
		}
		name := stringValue(body["display_name"])
		if name == "" {
			name = id
		}
		a := getAggregate(rows, id, name)
		a.row.LifecycleState = stringValue(body["lifecycle_state"])
		a.row.Mission = stringValue(body["mission_ref"])
		a.row.RegistryPath = path
		if specification, ok := body["specification"].(map[string]any); ok {
			a.factorySpecRef = stringValue(specification["ref"])
		}
		if identity, ok := body["board_identity"].(map[string]any); ok {
			a.row.BoardAgentID = stringValue(identity["agent_id"])
			a.row.BoardMachineID = stringValue(identity["machine_id"])
			a.row.BoardRef = stringValue(identity["board_ref"])
		}
		if strings.HasPrefix(a.row.Mission, "mission:") {
			a.missionName = strings.TrimPrefix(a.row.Mission, "mission:")
		}
		a.row.Sources = append(a.row.Sources, path)
		if a.row.LifecycleState == "" {
			a.row.LifecycleState = "draft"
		}
	}
	return firstErr
}

func (m *Monitor) readMissions(rows map[string]*aggregate) (map[string]*aggregate, error) {
	dir := strings.TrimSpace(m.opts.AutoOrchMissionsDir)
	if dir == "" {
		return rows, errors.New("auto-orch missions: directory is not configured")
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return rows, fmt.Errorf("auto-orch missions: %w", err)
	}
	found := 0
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		missionName := entry.Name()
		missionDir := filepath.Join(dir, missionName)
		configPath := filepath.Join(missionDir, "config.yaml")
		configBytes, err := os.ReadFile(configPath)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			continue // one broken mission must not hide the other employees
		}
		found++
		config := string(configBytes)
		fields := parseFlatYAML(config)
		id := fields["employee_id"]
		if id == "" {
			id = missionName
		}
		name := missionName
		if existing := rows[id]; existing != nil && existing.row.Name != "" {
			name = existing.row.Name
		}
		a := getAggregate(rows, id, name)
		a.autoOrchMission = true
		a.missionName = missionName
		a.config = config
		a.row.Mission = missionName
		a.row.Cadence = fields["cadence"]
		a.row.ConfigPath = configPath
		if launchApproved, ok := parseBoolValue(fields["launch_approved"]); ok {
			a.row.LaunchApproved = &launchApproved
		}
		a.row.Sources = append(a.row.Sources, configPath)
		missionBytes, _ := os.ReadFile(filepath.Join(missionDir, "mission.md"))
		a.missionText = string(missionBytes)
		if len(missionBytes) > 0 {
			a.row.Sources = append(a.row.Sources, filepath.Join(missionDir, "mission.md"))
			a.row.MissionPath = filepath.Join(missionDir, "mission.md")
			a.row.MissionPurpose = missionPurpose(string(missionBytes))
		}
		statePath := filepath.Join(missionDir, "state.md")
		stateBytes, stateErr := os.ReadFile(statePath)
		if stateErr == nil {
			a.state = string(stateBytes)
			a.row.Sources = append(a.row.Sources, statePath)
			a.row.StatePath = statePath
			parseState(a, string(stateBytes))
		}
		cyclePaths, _ := filepath.Glob(filepath.Join(missionDir, "cycle-reports", "*.yaml"))
		sort.Sort(sort.Reverse(sort.StringSlice(cyclePaths)))
		for i, cyclePath := range cyclePaths {
			if i >= 10 {
				break
			}
			cycle, err := parseCycle(cyclePath)
			if err != nil {
				a.errors = append(a.errors, err.Error())
				continue
			}
			a.cycles = append(a.cycles, cycle)
			a.row.Sources = append(a.row.Sources, cyclePath)
			if i == 0 {
				a.lastCycle = cycle.CycleID
				if cycle.Outcome != "" {
					a.lastOutcome = cycle.Outcome
				}
				a.lastRunStatus = cycle.RunStatus
				a.evidence = append(a.evidence, cycle.EvidencePath)
				a.evidence = append(a.evidence, cycle.EvidencePaths...)
				a.errors = append(a.errors, cycle.Errors...)
			}
		}
		if a.lastOutcome == "" {
			a.lastOutcome = fields["last_cycle_outcome"]
		}
		if a.row.LifecycleState == "" {
			a.row.LifecycleState = "mission"
		}
		if a.row.Name == "" {
			a.row.Name = missionName
		}
	}
	if found == 0 {
		return rows, fmt.Errorf("auto-orch missions: no config.yaml missions in %s", dir)
	}
	return rows, nil
}

// readFactoryManifestos supplies a recorded remit for registry employees that
// do not have a discovered Auto-Orch mission. The registry ref is the only
// authority for this fallback; Hermes profiles have no charter source here.
func (m *Monitor) readFactoryManifestos(rows map[string]*aggregate) {
	root := factoryRepositoryRoot(m.opts.FactoryRegistryDir)
	if root == "" {
		return
	}
	for _, a := range rows {
		if a.autoOrchMission || a.profile || a.factorySpecRef == "" || a.row.MissionPurpose != "" {
			continue
		}
		path, ok := factoryManifestPath(root, a.factorySpecRef)
		if !ok {
			continue
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		a.row.MissionPath = path
		a.row.Sources = append(a.row.Sources, path)
		a.row.MissionPurpose = missionPurpose(string(raw))
	}
}

func factoryRepositoryRoot(registryDir string) string {
	dir := filepath.Clean(strings.TrimSpace(registryDir))
	if dir == "" || dir == "." {
		return ""
	}
	for current := dir; ; current = filepath.Dir(current) {
		if filepath.Base(current) == "hiring" {
			return filepath.Dir(current)
		}
		parent := filepath.Dir(current)
		if parent == current {
			break
		}
	}
	return ""
}

func factoryManifestPath(factoryRoot, ref string) (string, bool) {
	const prefix = "spec-source:factory/"
	if !strings.HasPrefix(ref, prefix) {
		return "", false
	}
	relative := filepath.Clean(filepath.FromSlash(strings.TrimPrefix(ref, prefix)))
	if relative == "." || filepath.IsAbs(relative) || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return "", false
	}
	return filepath.Join(factoryRoot, relative), true
}

const hermesDisabledNote = "Hermes employees are not shown (source disabled)."

func (m *Monitor) readHermes(rows map[string]*aggregate) (string, error) {
	path := strings.TrimSpace(m.opts.HermesComposeFile)
	if strings.EqualFold(path, "off") {
		return hermesDisabledNote, nil
	}
	if path == "" {
		return "", errors.New("hermes: compose file is not configured")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("hermes compose: %w", err)
	}
	serviceRE := regexp.MustCompile(`(?m)^  (employee-[a-z0-9-]+):\s*$`)
	matches := serviceRE.FindAllStringSubmatchIndex(string(raw), -1)
	for i, match := range matches {
		service := string(raw[match[2]:match[3]])
		start := match[1]
		end := len(raw)
		if i+1 < len(matches) {
			end = matches[i+1][0]
		}
		block := string(raw[start:end])
		id, name := hermesIdentity(service)
		a := getAggregate(rows, id, name)
		a.profile = true
		a.row.Sources = append(a.row.Sources, path)
		if service == "employee-chief-of-staff" {
			a.row.LifecycleState = "running"
		} else if a.row.LifecycleState == "" {
			a.row.LifecycleState = "staffed-later"
		}
		if strings.Contains(block, "profiles: [\"staffed-later\"]") {
			a.profile = true
		}
		if a.missionName == "" {
			a.missionName = service
		}
	}
	text, err := m.opts.DockerPS(context.Background())
	if err != nil {
		return "", fmt.Errorf("docker ps: %w", err)
	}
	for _, line := range strings.Split(text, "\n") {
		parts := strings.SplitN(strings.TrimSpace(line), "\t", 2)
		if len(parts) != 2 || !strings.HasPrefix(parts[0], "employee-") {
			continue
		}
		id, name := hermesIdentity(parts[0])
		a := getAggregate(rows, id, name)
		a.container = true
		a.containerStatus = parts[1]
		a.row.Uptime = strings.TrimSpace(strings.TrimPrefix(parts[1], "Up "))
		a.row.Sources = append(a.row.Sources, "docker ps")
	}
	return "", nil
}

func hermesIdentity(service string) (string, string) {
	slug := strings.TrimPrefix(service, "employee-")
	switch slug {
	case "chief-of-staff":
		return "hermes-chief-of-staff", "Jarvis (Chief of Staff)"
	case "jobsearch":
		return "hermes-jobsearch", "Hermes Jobsearch"
	case "linux-utilities":
		return "hermes-linux-utilities", "Hermes Linux Utilities"
	case "data-migration":
		return "hermes-data-migration", "Hermes Data Migration"
	case "marketing":
		return "hermes-marketing", "Hermes Marketing"
	case "tviq":
		return "hermes-tviq", "Hermes TVIQ"
	case "ops-alerts":
		return "hermes-ops-alerts", "Hermes Ops Alerts"
	default:
		return "hermes-" + slug, "Hermes " + strings.Title(strings.ReplaceAll(slug, "-", " "))
	}
}

func getAggregate(rows map[string]*aggregate, id, name string) *aggregate {
	if id == "" {
		id = name
	}
	if a := rows[id]; a != nil {
		if a.row.Name == "" {
			a.row.Name = name
		}
		return a
	}
	a := &aggregate{row: Row{EmployeeID: id, Name: name}}
	rows[id] = a
	return a
}

func parseState(a *aggregate, text string) {
	fields := parseFlatYAML(text)
	// A state file keeps every pause record it has ever written, so a flat
	// last-key-wins read answers with whichever block happens to be last in the
	// file. For linux-utilities that was a resolved pause from August, which
	// reported a currently paused employee as running. Pause is therefore read
	// as delimited records and decided by the newest recorded pause date.
	if pause, ok := latestPause(text); ok {
		a.paused = pause.paused
		a.pauseReason = pause.reason
		a.actor = pause.actor
		a.pausedAt = pause.at
		a.row.PauseConflict = pause.conflict
	} else {
		a.paused = strings.EqualFold(fields["paused"], "true")
		a.pauseReason = fields["reason"]
		a.actor = fields["actor"]
		a.pausedAt = fields["paused_at"]
	}
	a.lastCycleEnd = fields["last_cycle_end"]
	a.nonDelivering = fields["consecutive_non_delivering_cycles"]
	a.loopState = fields["loop_state"]
	a.row.LoopState = a.loopState
	a.valueExhausted = strings.EqualFold(fields["value_exhausted"], "true") || strings.EqualFold(a.loopState, "value_exhausted")
	if value := fields["last_cycle_outcome"]; value != "" {
		a.lastOutcome = value
	}
	if value := fields["last_cycle_report"]; value != "" {
		a.lastCycle = value
	}
	if value := fields["last_execute_run_status"]; value != "" {
		a.lastRunStatus = value
	}
}

func finalize(a *aggregate) {
	if a.row.Name == "" {
		a.row.Name = a.row.EmployeeID
	}
	a.row.PauseReason = a.pauseReason
	a.row.Paused = a.paused
	a.row.PauseActor = a.actor
	a.row.PauseReasonText = a.pauseReason
	a.row.PausedAt = a.pausedAt
	a.row.LastCycleEnd = a.lastCycleEnd
	a.row.LastCycleReport = a.lastCycle
	a.row.NonDeliveringCycles = a.nonDelivering
	if a.actor != "" && a.pauseReason != "" {
		a.row.PauseReason = a.actor + ": " + a.pauseReason
	}
	if a.valueExhausted {
		a.row.LastOutcome = "value_exhausted"
	} else {
		a.row.LastOutcome = normalizeOutcome(a.lastOutcome)
	}
	a.row.ScheduleRunnable = len(a.cronLines) > 0 && !a.paused
	if len(a.cronLines) > 0 {
		a.row.NextFire = a.cronLines[0].next.Format(time.RFC3339)
	}
	if a.paused {
		a.row.Status = "paused"
		a.row.CurrentStage = "Auto-Orch mission"
		a.row.Activity = "Schedule paused"
		if a.pauseReason != "" {
			a.row.Activity = a.pauseReason
		}
	} else if a.container {
		a.row.Status = "working"
		a.row.Activity = "Hermes container running (" + a.containerStatus + ")"
		a.row.CurrentStage = "Hermes gateway"
	} else if a.valueExhausted {
		a.row.Status = "parked"
		a.row.Activity = "No valuable work left"
		a.row.CurrentStage = "Auto-Orch loop halted"
	} else if a.row.LifecycleState == "draft" || a.row.LifecycleState == "staffed-later" {
		a.row.Status = "offline"
		a.row.Activity = "Dormant profile / staffed later"
		a.row.CurrentStage = "Not commissioned"
	} else if a.lastRunStatus == "RUNNING" {
		a.row.Status = "working"
		a.row.CurrentStage = "Auto-Orch execute"
	} else if a.missionName != "" {
		a.row.Status = "idle"
		a.row.CurrentStage = "Auto-Orch mission"
		if a.lastOutcome != "" {
			a.row.Activity = "Last cycle " + a.lastOutcome
		}
	} else {
		a.row.Status = "offline"
		a.row.CurrentStage = "No live source"
	}
	if a.row.Status == "paused" {
		a.row.ScheduleRunnable = false
	}
	if a.row.NextFire == "" && a.row.Status == "paused" {
		a.row.NextFire = "non-runnable (paused)"
	}
	if a.row.NextFire == "" && a.row.Status == "offline" {
		a.row.NextFire = "none"
	}
	a.row.Sources = uniqueStrings(a.row.Sources)
	if a.row.CurrentStage == "" {
		a.row.CurrentStage = "Unknown"
	}
}

// missionPurpose quotes the charter's own remit section and nothing else.
//
// The headings are deliberately checked in priority order rather than document
// order. A list/table lead is only a fallback: a later recognised section with
// a paragraph is preferable, while a document with no recognised section still
// has no purpose to report.
func missionPurpose(text string) string {
	const headings = "purpose\x00mission purpose\x00objective\x00goal\x00mission objective\x00mission\x00charter\x00primary mission\x00why this employee exists"
	priority := strings.Split(headings, "\x00")
	lines := strings.Split(text, "\n")
	sections := make(map[string][]string)
	for i, line := range lines {
		heading := missionHeading(line)
		if heading == "" {
			continue
		}
		end := i + 1
		for end < len(lines) && !missionSectionBoundary(lines[end]) {
			end++
		}
		for _, candidate := range lines[i+1 : end] {
			candidate = strings.TrimSpace(candidate)
			if candidate != "" {
				sections[heading] = append(sections[heading], candidate)
				break
			}
		}
	}
	var fallback string
	for _, heading := range priority {
		for _, candidate := range sections[heading] {
			if startsMarkdownList(candidate) || startsMarkdownTable(candidate) {
				if fallback == "" {
					fallback = candidate
				}
				continue
			}
			return candidate
		}
	}
	return fallback
}

func missionHeading(line string) string {
	heading := strings.ToLower(strings.TrimSpace(strings.Trim(strings.TrimSpace(line), "#*_ ")))
	switch heading {
	case "purpose", "mission purpose", "objective", "goal", "mission objective", "mission", "charter", "primary mission", "why this employee exists":
		return heading
	default:
		return ""
	}
}

func missionSectionBoundary(line string) bool {
	trimmed := strings.TrimSpace(line)
	return strings.HasPrefix(trimmed, "#") || missionHeading(trimmed) != ""
}

func startsMarkdownList(line string) bool {
	if len(line) >= 2 && (line[0] == '-' || line[0] == '*' || line[0] == '+') && line[1] == ' ' {
		return true
	}
	for i := 0; i < len(line) && line[i] >= '0' && line[i] <= '9'; i++ {
		if i+1 < len(line) && (line[i+1] == '.' || line[i+1] == ')') && i+2 < len(line) && line[i+2] == ' ' {
			return true
		}
	}
	return false
}

func startsMarkdownTable(line string) bool {
	return strings.HasPrefix(line, "|")
}

type pauseRecord struct {
	paused   bool
	at       string
	actor    string
	reason   string
	conflict bool
}

// latestPause reads the delimited pause records a state file accumulates and
// returns the one with the newest recorded date.
//
// A record with no readable date cannot be ordered, so it never displaces a
// dated one. If the newest date is carried by two records that disagree, the
// result is marked conflicting rather than resolved by file order: nothing here
// is entitled to pick a winner between two equally current contradictions.
func latestPause(text string) (pauseRecord, bool) {
	const open, close = "<!-- auto-orch pause v1 -->", "<!-- end pause -->"
	var best pauseRecord
	var bestAt time.Time
	found, haveDate := false, false
	rest := text
	for {
		start := strings.Index(rest, open)
		if start < 0 {
			break
		}
		rest = rest[start+len(open):]
		end := strings.Index(rest, close)
		body := rest
		if end >= 0 {
			body = rest[:end]
			rest = rest[end+len(close):]
		} else {
			rest = ""
		}
		fields := parseFlatYAML(body)
		if _, ok := fields["paused"]; !ok {
			continue
		}
		record := pauseRecord{
			paused: strings.EqualFold(fields["paused"], "true"),
			at:     fields["paused_at"],
			actor:  fields["actor"],
			reason: fields["reason"],
		}
		at, err := time.Parse(time.RFC3339, strings.TrimSpace(record.at))
		dated := err == nil
		switch {
		case !found:
			best, bestAt, haveDate, found = record, at, dated, true
		case dated && !haveDate:
			best, bestAt, haveDate = record, at, true
		case dated && haveDate && at.After(bestAt):
			best, bestAt = record, at
		case dated && haveDate && at.Equal(bestAt) && record.paused != best.paused:
			best.conflict = true
		}
	}
	return best, found
}

func normalizeOutcome(value string) string {
	value = strings.TrimSpace(strings.Trim(value, "'\""))
	if value == "" || strings.EqualFold(value, "unknown") || value == "none" {
		return ""
	}
	return value
}

func parseCycle(path string) (Cycle, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Cycle{}, err
	}
	fields := parseFlatYAML(string(raw))
	base := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	cycle := Cycle{
		CycleID:      base,
		Path:         path,
		Outcome:      fields["cycle_outcome"],
		RunID:        fields["execute_run_id"],
		RunStatus:    fields["execute_run_status"],
		RunPassed:    fields["run_passed"],
		Title:        fields["evaluate_summary.value_assessment.selected_item_title"],
		ValueStatus:  fields["evaluate_summary.value_assessment.status"],
		ValueReason:  fields["evaluate_summary.value_assessment.reason"],
		CycleEnd:     fields["cycle_end"],
		EvidencePath: fields["evidence_path"],
	}
	if cycle.Outcome == "" {
		cycle.Outcome = fields["last_cycle_outcome"]
	}
	if cycle.EvidencePath == "" {
		cycle.EvidencePath = fields["evaluate_summary.evidence_path"]
	}
	for key, value := range fields {
		if (strings.HasSuffix(key, "_path") || strings.HasSuffix(key, "_dir")) && strings.HasPrefix(value, "/") {
			cycle.EvidencePaths = append(cycle.EvidencePaths, value)
		}
	}
	cycle.EvidencePaths = uniqueStrings(cycle.EvidencePaths)
	for _, line := range strings.Split(string(raw), "\n") {
		trim := strings.TrimSpace(line)
		if index := strings.Index(trim, "error:"); index >= 0 {
			value := strings.TrimSpace(strings.TrimPrefix(trim[index+len("error:"):], "-"))
			if value != "" && !strings.EqualFold(strings.Trim(value, "'\""), "null") {
				cycle.Errors = append(cycle.Errors, value)
			}
		}
		if strings.HasPrefix(trim, "- ") && (strings.Contains(trim, "validation") || strings.Contains(trim, "failed")) {
			cycle.Errors = append(cycle.Errors, strings.TrimSpace(strings.TrimPrefix(trim, "- ")))
		}
	}
	cycle.Errors = uniqueStrings(cycle.Errors)
	return cycle, nil
}

func parseFlatYAML(text string) map[string]string {
	result := map[string]string{}
	var parents []struct {
		indent int
		key    string
	}
	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), "\r")
		trim := strings.TrimSpace(line)
		if trim == "" || strings.HasPrefix(trim, "#") || strings.HasPrefix(trim, "-") {
			continue
		}
		colon := strings.IndexByte(trim, ':')
		if colon <= 0 {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " \t"))
		key := strings.TrimSpace(trim[:colon])
		value := strings.TrimSpace(trim[colon+1:])
		for len(parents) > 0 && indent <= parents[len(parents)-1].indent {
			parents = parents[:len(parents)-1]
		}
		// Each stacked parent already holds its own fully qualified path, so
		// prefixing with every ancestor repeated the outer segments and made
		// any key three levels deep unreachable
		// (evaluate_summary.value_assessment.selected_item_title became
		// evaluate_summary.value_assessment.evaluate_summary.selected_item_title).
		// Two-level keys are unaffected: one parent prefix is the same result.
		full := key
		if len(parents) > 0 {
			full = parents[len(parents)-1].key + "." + full
		}
		if value == "" {
			parents = append(parents, struct {
				indent int
				key    string
			}{indent, full})
			continue
		}
		result[full] = strings.Trim(strings.TrimSpace(value), "'\"")
	}
	return result
}

func stringValue(value any) string {
	if s, ok := value.(string); ok {
		return strings.TrimSpace(s)
	}
	return ""
}
func parseBoolValue(value string) (bool, bool) {
	parsed, err := strconv.ParseBool(strings.TrimSpace(value))
	return parsed, err == nil
}
func uniqueStrings(values []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" && !seen[value] {
			seen[value] = true
			out = append(out, value)
		}
	}
	return out
}

func readCrontab(ctx context.Context) (string, error) {
	commandCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	output, err := exec.CommandContext(commandCtx, "crontab", "-l").CombinedOutput()
	if err != nil {
		return string(output), err
	}
	return string(output), nil
}
func readDockerPS(ctx context.Context) (string, error) {
	commandCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	output, err := exec.CommandContext(commandCtx, "docker", "ps", "--format", "{{.Names}}\t{{.Status}}").CombinedOutput()
	if err != nil {
		return string(output), err
	}
	return string(output), nil
}

type cronEntry struct{ next time.Time }

func applyCron(rows map[string]*aggregate, text string, now time.Time) {
	for _, line := range strings.Split(text, "\n") {
		trim := strings.TrimSpace(line)
		if trim == "" || strings.HasPrefix(trim, "#") {
			continue
		}
		fields := strings.Fields(trim)
		if len(fields) < 2 {
			continue
		}
		mission, matchedOn, exact := bestCronMatch(rows, trim)
		if mission == "" {
			continue
		}
		var next time.Time
		var ok bool
		if strings.HasPrefix(fields[0], "@") {
			next, ok = nextSpecialCron(fields[0], now)
		} else {
			if len(fields) < 6 {
				continue
			}
			next, ok = nextCron(fields[:5], now)
		}
		if !ok {
			continue
		}
		rows[mission].cronLines = append(rows[mission].cronLines, cronEntry{next: next})
		// A substring hit and a named CLI argument are not the same claim. Only
		// the second is a binding; the first is recorded as what it matched on
		// so a reader can judge it instead of being handed a schedule.
		if rows[mission].row.CronMatchedOn == "" {
			rows[mission].row.CronMatchedOn = matchedOn
		}
		if exact {
			rows[mission].row.CronExactBinding = true
		}
	}
	for _, a := range rows {
		sort.Slice(a.cronLines, func(i, j int) bool { return a.cronLines[i].next.Before(a.cronLines[j].next) })
	}
}

// bestCronMatch also reports whether the line names the mission as an argument
// to the Auto-Orch CLI. "loop <mission>" and "--mission <mission>" are
// bindings; a lock file or a log path that merely contains the name is not.
func bestCronMatch(rows map[string]*aggregate, line string) (string, string, bool) {
	bestID, bestValue, bestScore, bestExact := "", "", 0, false
	for id, a := range rows {
		if a.profile {
			continue
		}
		candidates := []struct {
			value string
			score int
		}{
			{a.missionName, 5},
			{strings.TrimPrefix(a.row.Mission, "mission:"), 4},
			{id, 3},
		}
		for _, candidate := range candidates {
			value := strings.TrimSpace(candidate.value)
			if value == "" {
				continue
			}
			if cronNamesMission(line, value) && candidate.score+10 > bestScore {
				bestID, bestValue, bestScore, bestExact = id, value, candidate.score+10, true
				continue
			}
			if strings.Contains(line, value) && candidate.score > bestScore {
				bestID, bestValue, bestScore, bestExact = id, value, candidate.score, false
			}
			normalized := strings.ReplaceAll(value, "-", "_")
			if normalized != value && strings.Contains(line, normalized) && candidate.score-1 > bestScore {
				bestID, bestValue, bestScore, bestExact = id, normalized, candidate.score-1, false
			}
		}
	}
	return bestID, bestValue, bestExact
}

func cronNamesMission(line, mission string) bool {
	for _, prefix := range []string{"loop ", "--mission ", "--mission="} {
		at := 0
		for {
			index := strings.Index(line[at:], prefix+mission)
			if index < 0 {
				break
			}
			end := at + index + len(prefix) + len(mission)
			if end == len(line) || line[end] == ' ' || line[end] == '"' || line[end] == '\'' {
				return true
			}
			at = end
		}
	}
	return false
}

func nextSpecialCron(spec string, now time.Time) (time.Time, bool) {
	switch spec {
	case "@hourly":
		return now.Truncate(time.Hour).Add(time.Hour), true
	case "@daily", "@midnight":
		start := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, now.Location())
		return start.AddDate(0, 0, 1), true
	case "@weekly":
		start := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, now.Location())
		days := (7 - int(start.Weekday())) % 7
		if days == 0 {
			days = 7
		}
		return start.AddDate(0, 0, days), true
	case "@monthly":
		start := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, now.Location())
		return start.AddDate(0, 1, 0), true
	default:
		return time.Time{}, false
	}
}

func nextCron(fields []string, now time.Time) (time.Time, bool) {
	if len(fields) != 5 {
		return time.Time{}, false
	}
	sets := make([]map[int]bool, 5)
	limits := [][2]int{{0, 59}, {0, 23}, {1, 31}, {1, 12}, {0, 6}}
	for i, field := range fields {
		parsed, ok := parseCronField(field, limits[i][0], limits[i][1])
		if !ok {
			return time.Time{}, false
		}
		sets[i] = parsed
	}
	domWildcard := fields[2] == "*"
	dowWildcard := fields[4] == "*"
	candidate := now.Truncate(time.Minute).Add(time.Minute)
	for range 366 * 24 * 60 {
		minute, hour, day := candidate.Minute(), candidate.Hour(), candidate.Day()
		month, weekday := int(candidate.Month()), int(candidate.Weekday())
		dayMatch := sets[2][day]
		weekdayMatch := sets[4][weekday]
		dateMatch := dayMatch && weekdayMatch
		if !domWildcard && !dowWildcard {
			dateMatch = dayMatch || weekdayMatch
		}
		if sets[0][minute] && sets[1][hour] && sets[3][month] && dateMatch {
			return candidate, true
		}
		candidate = candidate.Add(time.Minute)
	}
	return time.Time{}, false
}

func parseCronField(field string, min, max int) (map[int]bool, bool) {
	out := map[int]bool{}
	for _, part := range strings.Split(field, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			return nil, false
		}
		step := 1
		if strings.Contains(part, "/") {
			pieces := strings.SplitN(part, "/", 2)
			var err error
			step, err = strconv.Atoi(pieces[1])
			if err != nil || step <= 0 {
				return nil, false
			}
			part = pieces[0]
		}
		start, end := min, max
		if part != "*" {
			if strings.Contains(part, "-") {
				pieces := strings.SplitN(part, "-", 2)
				var err error
				start, err = strconv.Atoi(pieces[0])
				if err != nil {
					return nil, false
				}
				end, err = strconv.Atoi(pieces[1])
				if err != nil {
					return nil, false
				}
			} else {
				value, err := strconv.Atoi(part)
				if err != nil {
					return nil, false
				}
				start, end = value, value
			}
		}
		if start < min || end > max || start > end {
			return nil, false
		}
		for value := start; value <= end; value += step {
			out[value] = true
		}
	}
	return out, true
}
