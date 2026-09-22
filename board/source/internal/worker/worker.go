package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"agent-board/internal/config"
	"agent-board/internal/domain"
	"agent-board/internal/store"
)

type options struct {
	URL              string
	Token            string
	AgentID          string
	MachineID        string
	Kind             string
	Role             string
	Capabilities     []string
	Command          string
	WorkDir          string
	EvidenceDir      string
	ReviewTargetType string
	ReviewTargetID   string
	LeaseSeconds     int
	IdleSeconds      int
	ErrorSeconds     int
	MaxTasks         int
}

type client struct {
	baseURL string
	token   string
	http    *http.Client
}

var newHTTPClient = func() *http.Client {
	return http.DefaultClient
}

const maxAuthenticationFailures = 3

type httpStatusError struct {
	Method string
	Path   string
	Status int
	Body   string
}

func (e *httpStatusError) Error() string {
	return fmt.Sprintf("%s %s returned %d: %s", e.Method, e.Path, e.Status, e.Body)
}

func Run(ctx context.Context, cfg config.Config, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: agent-board worker once|run --agent ID --machine ID --capability CAP --command COMMAND")
	}
	switch args[0] {
	case "once":
		opts, err := parseOptions(cfg, "worker once", args[1:])
		if err != nil {
			return err
		}
		return runOnce(ctx, opts, stdout)
	case "run":
		opts, err := parseOptions(cfg, "worker run", args[1:])
		if err != nil {
			return err
		}
		return runLoop(ctx, opts, stdout)
	default:
		return errors.New("usage: agent-board worker once|run --agent ID --machine ID --capability CAP --command COMMAND")
	}
}

func parseOptions(cfg config.Config, name string, args []string) (options, error) {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	url := fs.String("url", firstNonEmpty(os.Getenv("AGENT_BOARD_URL"), "http://127.0.0.1:8787"), "agent-board HTTP URL")
	machineSecretFile := fs.String("machine-secret-file", cfg.MachineSecretFile, "path to a mode-0600 machine API secret file")
	token := fs.String("token", "", "API bearer token")
	agentID := fs.String("agent", os.Getenv("AGENT_BOARD_WORKER_AGENT"), "agent id")
	machineID := fs.String("machine", os.Getenv("AGENT_BOARD_WORKER_MACHINE"), "machine id")
	kind := fs.String("kind", firstNonEmpty(os.Getenv("AGENT_BOARD_WORKER_KIND"), "shell"), "agent kind")
	role := fs.String("role", firstNonEmpty(os.Getenv("AGENT_BOARD_WORKER_ROLE"), "worker"), "agent role")
	command := fs.String("command", os.Getenv("AGENT_BOARD_WORKER_COMMAND"), "shell command to run for a claimed task")
	workDir := fs.String("work-dir", os.Getenv("AGENT_BOARD_WORKER_WORK_DIR"), "command working directory")
	evidenceDir := fs.String("evidence-dir", firstNonEmpty(os.Getenv("AGENT_BOARD_WORKER_EVIDENCE_DIR"), os.TempDir()), "directory for evidence files")
	reviewTargetType := fs.String("review-target-type", os.Getenv("AGENT_BOARD_WORKER_REVIEW_TARGET_TYPE"), "review target actor type")
	reviewTargetID := fs.String("review-target", os.Getenv("AGENT_BOARD_WORKER_REVIEW_TARGET"), "review target actor id")
	leaseSeconds := fs.Int("lease-seconds", envInt("AGENT_BOARD_WORKER_LEASE_SECONDS", domain.DefaultLeaseSeconds), "lease seconds")
	idleSeconds := fs.Int("idle-seconds", envInt("AGENT_BOARD_WORKER_IDLE_SECONDS", 5), "seconds to sleep when no task is eligible")
	errorSeconds := fs.Int("error-seconds", envInt("AGENT_BOARD_WORKER_ERROR_SECONDS", 10), "seconds to sleep after a worker cycle error")
	maxTasks := fs.Int("max-tasks", envInt("AGENT_BOARD_WORKER_MAX_TASKS", 0), "stop after this many claimed tasks; 0 runs until interrupted")
	var capabilities repeatFlag
	for _, cap := range splitCSV(os.Getenv("AGENT_BOARD_WORKER_CAPABILITIES")) {
		capabilities = append(capabilities, cap)
	}
	fs.Var(&capabilities, "capability", "worker capability")
	if err := fs.Parse(args); err != nil {
		return options{}, err
	}
	if *token == "" && *machineSecretFile != "" {
		secret, err := config.ReadSecretFile(*machineSecretFile)
		if err != nil {
			return options{}, err
		}
		*token = secret
	}
	if *machineID == "" {
		if host, err := os.Hostname(); err == nil {
			*machineID = host
		}
	}
	if strings.TrimSpace(*token) == "" {
		return options{}, errors.New("--token or --machine-secret-file is required")
	}
	if *agentID == "" || *machineID == "" {
		return options{}, errors.New("--agent and --machine are required")
	}
	if len(capabilities) == 0 {
		return options{}, errors.New("at least one --capability is required")
	}
	if *command == "" {
		return options{}, errors.New("--command is required")
	}
	if (*reviewTargetType == "") != (*reviewTargetID == "") {
		return options{}, errors.New("--review-target-type and --review-target must be provided together")
	}
	if *leaseSeconds == 0 {
		*leaseSeconds = domain.DefaultLeaseSeconds
	}
	if *idleSeconds < 1 {
		*idleSeconds = 1
	}
	if *errorSeconds < 1 {
		*errorSeconds = 1
	}
	return options{
		URL:              strings.TrimRight(*url, "/"),
		Token:            *token,
		AgentID:          *agentID,
		MachineID:        *machineID,
		Kind:             *kind,
		Role:             *role,
		Capabilities:     capabilities,
		Command:          *command,
		WorkDir:          *workDir,
		EvidenceDir:      *evidenceDir,
		ReviewTargetType: *reviewTargetType,
		ReviewTargetID:   *reviewTargetID,
		LeaseSeconds:     *leaseSeconds,
		IdleSeconds:      *idleSeconds,
		ErrorSeconds:     *errorSeconds,
		MaxTasks:         *maxTasks,
	}, nil
}

func runOnce(ctx context.Context, opts options, stdout io.Writer) error {
	_, err := runCycle(ctx, opts, stdout)
	return err
}

func runLoop(ctx context.Context, opts options, stdout io.Writer) error {
	completed := 0
	authFailures := 0
	for {
		if err := ctx.Err(); err != nil {
			return nil
		}
		claimed, err := runCycle(ctx, opts, stdout)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			if isRegistrationOrClaimAuthenticationError(err) {
				authFailures++
				if authFailures >= maxAuthenticationFailures {
					return fmt.Errorf("worker authentication failed after %d attempts; verify the API bearer token: %w", authFailures, err)
				}
				fmt.Fprintf(stdout, "worker authentication attempt %d/%d failed: %v\n", authFailures, maxAuthenticationFailures, err)
				if !sleepContext(ctx, time.Duration(opts.ErrorSeconds)*time.Second) {
					return nil
				}
				continue
			}
			authFailures = 0
			fmt.Fprintf(stdout, "worker cycle failed: %v\n", err)
			if !sleepContext(ctx, time.Duration(opts.ErrorSeconds)*time.Second) {
				return nil
			}
			continue
		}
		authFailures = 0
		if claimed {
			completed++
			if opts.MaxTasks > 0 && completed >= opts.MaxTasks {
				fmt.Fprintf(stdout, "worker stopped after %d task(s)\n", completed)
				return nil
			}
			continue
		}
		fmt.Fprintf(stdout, "idle; sleeping %ds\n", opts.IdleSeconds)
		if !sleepContext(ctx, time.Duration(opts.IdleSeconds)*time.Second) {
			return nil
		}
	}
}

func isRegistrationOrClaimAuthenticationError(err error) bool {
	var statusErr *httpStatusError
	if !errors.As(err, &statusErr) || (statusErr.Status != http.StatusUnauthorized && statusErr.Status != http.StatusForbidden) {
		return false
	}
	return statusErr.Path == "/machines/register" || statusErr.Path == "/agents/register" || statusErr.Path == "/poll"
}

func sleepContext(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func runCycle(ctx context.Context, opts options, stdout io.Writer) (bool, error) {
	c := client{baseURL: opts.URL, token: opts.Token, http: newHTTPClient()}
	if _, err := c.post(ctx, "/machines/register", store.RegisterMachineInput{
		ID:           opts.MachineID,
		Hostname:     opts.MachineID,
		OS:           runtime.GOOS,
		Arch:         runtime.GOARCH,
		Capabilities: opts.Capabilities,
	}, nil); err != nil {
		return false, err
	}
	if _, err := c.post(ctx, "/agents/register", store.RegisterAgentInput{
		ID:           opts.AgentID,
		MachineID:    opts.MachineID,
		Kind:         opts.Kind,
		Role:         opts.Role,
		Capabilities: opts.Capabilities,
	}, nil); err != nil {
		return false, err
	}

	var poll struct {
		Task  *domain.Task  `json:"task"`
		Lease *domain.Lease `json:"lease"`
	}
	if _, err := c.post(ctx, "/poll", store.PollInput{
		AgentID:      opts.AgentID,
		MachineID:    opts.MachineID,
		Capabilities: opts.Capabilities,
		LeaseSeconds: opts.LeaseSeconds,
	}, &poll); err != nil {
		return false, err
	}
	if poll.Task == nil {
		fmt.Fprintln(stdout, "no eligible task")
		return false, nil
	}
	if poll.Lease == nil {
		return true, errors.New("poll response did not include lease")
	}

	stopRenew := startLeaseRenewer(ctx, c, poll.Lease.ID, opts.LeaseSeconds)
	result, err := executeTask(ctx, opts, *poll.Task, *poll.Lease)
	if renewErr := stopRenew(); renewErr != nil && err == nil {
		err = renewErr
	}
	if result.EvidencePath != "" {
		artifact, artifactErr := c.uploadArtifact(ctx, result.EvidencePath, store.AddArtifactInput{
			TaskID:  poll.Task.ID,
			AgentID: opts.AgentID,
			Kind:    "report",
			Summary: result.UploadSummary(),
		})
		if artifactErr == nil {
			result.ArtifactLocation = artifact.PathOrURL
		} else {
			_, artifactErr = c.post(ctx, "/artifacts", store.AddArtifactInput{
				TaskID:    poll.Task.ID,
				AgentID:   opts.AgentID,
				Kind:      "report",
				PathOrURL: result.EvidencePath,
				Summary:   result.Summary(),
			}, nil)
			if artifactErr == nil {
				result.ArtifactLocation = result.EvidencePath
			}
		}
		if artifactErr != nil && err == nil {
			err = artifactErr
		}
	}
	if err != nil {
		_, failErr := c.post(ctx, "/tasks/"+poll.Task.ID+"/fail", taskActionRequest{
			ActorType: "agent",
			ActorID:   opts.AgentID,
			LeaseID:   poll.Lease.ID,
			Reason:    result.Summary(),
		}, nil)
		if failErr != nil {
			return true, fmt.Errorf("%w; also failed to mark task failed: %v", err, failErr)
		}
		return true, err
	}
	if _, err := c.post(ctx, "/tasks/"+poll.Task.ID+"/review", taskActionRequest{
		ActorType:  "agent",
		ActorID:    opts.AgentID,
		LeaseID:    poll.Lease.ID,
		Reason:     result.Summary(),
		AssignType: opts.ReviewTargetType,
		AssignTo:   opts.ReviewTargetID,
	}, nil); err != nil {
		return true, err
	}
	fmt.Fprintf(stdout, "task %s submitted for review; evidence %s\n", poll.Task.ID, result.EvidenceLocation())
	return true, nil
}

type taskActionRequest struct {
	ActorType  string `json:"actor_type"`
	ActorID    string `json:"actor_id"`
	LeaseID    string `json:"lease_id"`
	Reason     string `json:"reason"`
	AssignType string `json:"assign_type,omitempty"`
	AssignTo   string `json:"assign_to,omitempty"`
}

type commandResult struct {
	EvidencePath     string
	ArtifactLocation string
	ExitCode         int
}

func (r commandResult) Summary() string {
	location := r.EvidenceLocation()
	if r.ExitCode == 0 {
		return "worker completed; evidence attached: " + location
	}
	return fmt.Sprintf("worker failed with exit code %d; evidence attached: %s", r.ExitCode, location)
}

func (r commandResult) UploadSummary() string {
	if r.ExitCode == 0 {
		return "worker completed; evidence uploaded"
	}
	return fmt.Sprintf("worker failed with exit code %d; evidence uploaded", r.ExitCode)
}

func (r commandResult) EvidenceLocation() string {
	if r.ArtifactLocation != "" {
		return r.ArtifactLocation
	}
	return r.EvidencePath
}

func executeTask(ctx context.Context, opts options, task domain.Task, lease domain.Lease) (commandResult, error) {
	if err := os.MkdirAll(opts.EvidenceDir, 0o755); err != nil {
		return commandResult{}, err
	}
	stamp := time.Now().UnixNano()
	contextPath := filepath.Join(opts.EvidenceDir, fmt.Sprintf("agent-board-%s-%d-context.md", sanitize(task.ID), stamp))
	evidencePath := filepath.Join(opts.EvidenceDir, fmt.Sprintf("agent-board-%s-%d.md", sanitize(task.ID), stamp))
	if err := writeContext(contextPath, task, lease); err != nil {
		return commandResult{}, err
	}
	cmd := exec.CommandContext(ctx, "sh", "-c", opts.Command)
	if opts.WorkDir != "" {
		cmd.Dir = opts.WorkDir
	}
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	cmd.Env = append(os.Environ(),
		"AGENT_BOARD_TASK_ID="+task.ID,
		"AGENT_BOARD_TASK_TITLE="+task.Title,
		"AGENT_BOARD_TASK_INSTRUCTIONS="+task.Instructions,
		"AGENT_BOARD_TASK_MISSION="+task.Mission,
		"AGENT_BOARD_LEASE_ID="+lease.ID,
		"AGENT_BOARD_AGENT_ID="+opts.AgentID,
		"AGENT_BOARD_MACHINE_ID="+opts.MachineID,
		"AGENT_BOARD_CONTEXT_FILE="+contextPath,
		"AGENT_BOARD_EVIDENCE_FILE="+evidencePath,
	)
	err := cmd.Run()
	result := commandResult{EvidencePath: evidencePath, ExitCode: exitCode(err)}
	if writeErr := writeEvidence(evidencePath, contextPath, opts, task, lease, result.ExitCode, stdout.String(), stderr.String()); writeErr != nil {
		return result, writeErr
	}
	if err != nil {
		return result, err
	}
	return result, nil
}

func writeContext(path string, task domain.Task, lease domain.Lease) error {
	var out strings.Builder
	fmt.Fprintf(&out, "# agent-board Task Context\n\n")
	fmt.Fprintf(&out, "- Task: %s\n", task.ID)
	fmt.Fprintf(&out, "- Display: #%d\n", task.DisplayNumber)
	fmt.Fprintf(&out, "- Title: %s\n", task.Title)
	fmt.Fprintf(&out, "- Status: %s\n", task.Status)
	fmt.Fprintf(&out, "- Priority: %s\n", task.Priority)
	fmt.Fprintf(&out, "- Lease: %s\n", lease.ID)
	fmt.Fprintf(&out, "- Required capabilities: %s\n", strings.Join(task.RequiredCapabilities, ", "))
	fmt.Fprintf(&out, "- Machine constraints: %s\n\n", strings.Join(task.MachineConstraints, ", "))
	if task.Mission != "" {
		fmt.Fprintf(&out, "## Mission\n\n%s\n\n", task.Mission)
	}
	if task.Instructions != "" {
		fmt.Fprintf(&out, "## Instructions\n\n%s\n", task.Instructions)
	}
	return os.WriteFile(path, []byte(out.String()), 0o644)
}

func startLeaseRenewer(ctx context.Context, c client, leaseID string, leaseSeconds int) func() error {
	if leaseSeconds <= 0 {
		leaseSeconds = domain.DefaultLeaseSeconds
	}
	interval := time.Duration(leaseSeconds) * time.Second / 3
	if interval < 250*time.Millisecond {
		interval = 250 * time.Millisecond
	}
	if interval > time.Minute {
		interval = time.Minute
	}
	renewCtx, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-renewCtx.Done():
				done <- nil
				return
			case <-ticker.C:
				var lease domain.Lease
				if _, err := c.post(renewCtx, "/leases/"+leaseID+"/renew", map[string]int{"lease_seconds": leaseSeconds}, &lease); err != nil {
					if renewCtx.Err() != nil {
						done <- nil
						return
					}
					done <- err
					return
				}
			}
		}
	}()
	return func() error {
		cancel()
		return <-done
	}
}

func writeEvidence(path string, contextPath string, opts options, task domain.Task, lease domain.Lease, exitCode int, stdout string, stderr string) error {
	var out strings.Builder
	fmt.Fprintf(&out, "# agent-board Worker Evidence\n\n")
	fmt.Fprintf(&out, "- Task: %s\n", task.ID)
	fmt.Fprintf(&out, "- Title: %s\n", task.Title)
	fmt.Fprintf(&out, "- Agent: %s\n", opts.AgentID)
	fmt.Fprintf(&out, "- Machine: %s\n", opts.MachineID)
	fmt.Fprintf(&out, "- Lease: %s\n", lease.ID)
	fmt.Fprintf(&out, "- Context file: %s\n", contextPath)
	fmt.Fprintf(&out, "- Exit code: %d\n", exitCode)
	fmt.Fprintf(&out, "- Completed at: %s\n\n", time.Now().UTC().Format(time.RFC3339))
	if task.Instructions != "" {
		fmt.Fprintf(&out, "## Instructions\n\n%s\n\n", task.Instructions)
	}
	fmt.Fprintf(&out, "## Stdout\n\n```text\n%s\n```\n\n", stdout)
	fmt.Fprintf(&out, "## Stderr\n\n```text\n%s\n```\n", stderr)
	return os.WriteFile(path, []byte(out.String()), 0o644)
}

func (c client) post(ctx context.Context, path string, input any, output any) ([]byte, error) {
	body, err := json.Marshal(input)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpStatusError{Method: http.MethodPost, Path: path, Status: resp.StatusCode, Body: strings.TrimSpace(string(data))}
	}
	if output != nil {
		if err := json.Unmarshal(data, output); err != nil {
			return nil, err
		}
	}
	return data, nil
}

func (c client) uploadArtifact(ctx context.Context, path string, input store.AddArtifactInput) (domain.Artifact, error) {
	file, err := os.Open(path)
	if err != nil {
		return domain.Artifact{}, err
	}
	defer file.Close()

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	for key, value := range map[string]string{
		"task_id":  input.TaskID,
		"agent_id": input.AgentID,
		"kind":     input.Kind,
		"summary":  input.Summary,
	} {
		if value != "" {
			if err := writer.WriteField(key, value); err != nil {
				return domain.Artifact{}, err
			}
		}
	}
	part, err := writer.CreateFormFile("file", filepath.Base(path))
	if err != nil {
		return domain.Artifact{}, err
	}
	if _, err := io.Copy(part, file); err != nil {
		return domain.Artifact{}, err
	}
	if err := writer.Close(); err != nil {
		return domain.Artifact{}, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/artifacts/upload", &body)
	if err != nil {
		return domain.Artifact{}, err
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return domain.Artifact{}, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return domain.Artifact{}, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return domain.Artifact{}, fmt.Errorf("POST /artifacts/upload returned %d: %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	var artifact domain.Artifact
	if err := json.Unmarshal(data, &artifact); err != nil {
		return domain.Artifact{}, err
	}
	return artifact, nil
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return exitErr.ExitCode()
	}
	return 1
}

func sanitize(value string) string {
	var out strings.Builder
	for _, r := range value {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' {
			out.WriteRune(r)
			continue
		}
		out.WriteByte('-')
	}
	if out.Len() == 0 {
		return "task"
	}
	return out.String()
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func splitCSV(value string) []string {
	var out []string
	for _, item := range strings.Split(value, ",") {
		item = strings.TrimSpace(item)
		if item != "" {
			out = append(out, item)
		}
	}
	return out
}

func envInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	n, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return n
}

type repeatFlag []string

func (r *repeatFlag) String() string {
	return strings.Join(*r, ",")
}

func (r *repeatFlag) Set(value string) error {
	*r = append(*r, value)
	return nil
}
