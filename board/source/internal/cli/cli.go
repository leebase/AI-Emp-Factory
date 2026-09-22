package cli

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"text/tabwriter"

	"agent-board/internal/auth"
	"agent-board/internal/domain"
	"agent-board/internal/roster"
	"agent-board/internal/services"
	"agent-board/internal/store"
)

func Run(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		usage(stdout)
		return nil
	}
	switch args[0] {
	case "auth":
		return RunAuth(args[1:], stdout)
	case "db":
		return runDB(ctx, b, args[1:], stdout)
	case "machine":
		return runMachine(ctx, b, args[1:], stdout)
	case "agent":
		return runAgent(ctx, b, args[1:], stdout)
	case "token":
		return runToken(ctx, b, args[1:], stdout)
	case "machines":
		return runMachines(ctx, b, stdout)
	case "agents":
		return runAgents(ctx, b, stdout)
	case "task":
		return runTask(ctx, b, args[1:], stdout)
	case "message":
		return runMessage(ctx, b, args[1:], stdout)
	case "artifact":
		return runArtifact(ctx, b, args[1:], stdout)
	case "poll":
		return runPoll(ctx, b, args[1:], stdout)
	case "lease":
		return runLease(ctx, b, args[1:], stdout)
	case "stale":
		return runStale(ctx, b, args[1:], stdout)
	case "status":
		return runStatus(ctx, b, stdout)
	case "snapshot":
		return runSnapshot(ctx, b, stdout)
	case "project-state":
		return runProjectState(ctx, b, args[1:], stdout)
	case "metrics":
		return runMetrics(ctx, b, stdout)
	case "queue":
		return printTasks(ctx, b, domain.TaskStatusReady, "", stdout)
	case "running":
		return printTasks(ctx, b, domain.TaskStatusClaimed, "", stdout)
	case "inbox":
		return runInbox(ctx, b, args[1:], stdout)
	case "events":
		return runEvents(ctx, b, args[1:], stdout)
	case "roster":
		return runRoster(ctx, b, args[1:], stdout)
	case "active-state":
		return runActiveState(ctx, b, args[1:], stdout)
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func RunAuth(args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: agent-board auth hash-password|generate-machine-secret")
	}
	switch args[0] {
	case "hash-password":
		if len(args) != 1 {
			return errors.New("usage: printf '%s\\n' PASSWORD | agent-board auth hash-password")
		}
		input, err := io.ReadAll(os.Stdin)
		if err != nil {
			return err
		}
		password := strings.TrimSuffix(strings.TrimSuffix(string(input), "\n"), "\r")
		hash, err := auth.HashPassword(password)
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(stdout, hash)
		return err
	case "generate-machine-secret":
		if len(args) != 1 {
			return errors.New("usage: agent-board auth generate-machine-secret")
		}
		secret, err := auth.GenerateMachineSecret()
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(stdout, secret)
		return err
	default:
		return fmt.Errorf("unknown auth command %q", args[0])
	}
}

func runMachines(ctx context.Context, b *services.Board, stdout io.Writer) error {
	machines, err := b.ListMachines(ctx)
	if err != nil {
		return err
	}
	return printJSON(stdout, machines)
}

func runAgents(ctx context.Context, b *services.Board, stdout io.Writer) error {
	agents, err := b.ListAgents(ctx)
	if err != nil {
		return err
	}
	return printJSON(stdout, agents)
}

func runDB(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: agent-board db init|backup")
	}
	switch args[0] {
	case "init":
		if err := b.Migrate(ctx); err != nil {
			return err
		}
		fmt.Fprintln(stdout, "database initialized")
		return nil
	case "backup":
		fs := flag.NewFlagSet("db backup", flag.ContinueOnError)
		out := fs.String("out", "", "backup output path")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if *out == "" {
			return errors.New("usage: agent-board db backup --out PATH")
		}
		if err := b.Backup(ctx, *out); err != nil {
			return err
		}
		fmt.Fprintf(stdout, "backup written to %s\n", *out)
		return nil
	default:
		return fmt.Errorf("unknown db command %q", args[0])
	}
}

func runMachine(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 || args[0] != "register" {
		return errors.New("usage: agent-board machine register --id ID [--capability CAP]")
	}
	fs := flag.NewFlagSet("machine register", flag.ContinueOnError)
	id := fs.String("id", "", "machine id")
	hostname := fs.String("hostname", "", "hostname")
	osName := fs.String("os", "", "os")
	arch := fs.String("arch", "", "arch")
	var caps repeatFlag
	fs.Var(&caps, "capability", "capability")
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	if *id == "" {
		return errors.New("--id is required")
	}
	machine, err := b.RegisterMachine(ctx, store.RegisterMachineInput{
		ID:           *id,
		Hostname:     fallback(*hostname, hostName()),
		OS:           *osName,
		Arch:         *arch,
		Capabilities: caps,
	})
	if err != nil {
		return err
	}
	return printJSON(stdout, machine)
}

func runAgent(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 || args[0] != "register" {
		return errors.New("usage: agent-board agent register --id ID --machine MACHINE [--capability CAP]")
	}
	fs := flag.NewFlagSet("agent register", flag.ContinueOnError)
	id := fs.String("id", "", "agent id")
	machine := fs.String("machine", "", "machine id")
	kind := fs.String("kind", "shell", "agent kind")
	role := fs.String("role", "producer", "agent role")
	var caps repeatFlag
	fs.Var(&caps, "capability", "capability")
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	if *id == "" || *machine == "" {
		return errors.New("--id and --machine are required")
	}
	agent, err := b.RegisterAgent(ctx, store.RegisterAgentInput{
		ID:           *id,
		MachineID:    *machine,
		Kind:         *kind,
		Role:         *role,
		Capabilities: caps,
	})
	if err != nil {
		return err
	}
	return printJSON(stdout, agent)
}

func runToken(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: agent-board token create|revoke")
	}
	switch args[0] {
	case "create":
		fs := flag.NewFlagSet("token create", flag.ContinueOnError)
		agentID := fs.String("agent", "", "agent id")
		rawToken := fs.String("token", "", "token value; generated when omitted")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if *agentID == "" {
			return errors.New("usage: agent-board token create --agent AGENT_ID [--token TOKEN]")
		}
		if *rawToken == "" {
			generated, err := generateToken()
			if err != nil {
				return err
			}
			*rawToken = generated
		}
		record, err := b.CreateAPIToken(ctx, store.CreateAPITokenInput{
			AgentID: *agentID,
			Token:   *rawToken,
		})
		if err != nil {
			return err
		}
		return printJSON(stdout, map[string]any{
			"token":  *rawToken,
			"record": record,
		})
	case "revoke":
		fs := flag.NewFlagSet("token revoke", flag.ContinueOnError)
		rawToken := fs.String("token", "", "token value to revoke")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if *rawToken == "" {
			return errors.New("usage: agent-board token revoke --token TOKEN")
		}
		record, err := b.RevokeAPIToken(ctx, *rawToken)
		if err != nil {
			return err
		}
		return printJSON(stdout, record)
	default:
		return fmt.Errorf("unknown token command %q", args[0])
	}
}

func runTask(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: agent-board task create|list|show")
	}
	switch args[0] {
	case "create":
		fs := flag.NewFlagSet("task create", flag.ContinueOnError)
		title := fs.String("title", "", "task title")
		mission := fs.String("mission", "", "mission")
		priority := fs.String("priority", "P2", "priority")
		instructions := fs.String("instructions", "", "instructions")
		actor := fs.String("actor", "lee", "creator id")
		actorType := fs.String("actor-type", "human", "creator actor type")
		parent := fs.String("parent", "", "parent task id")
		assignedAgent := fs.String("assigned-agent", "", "assigned agent id")
		project := fs.String("project", "", "project slug")
		var required repeatFlag
		var machines repeatFlag
		fs.Var(&required, "required-capability", "required capability")
		fs.Var(&machines, "machine", "allowed machine id")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		task, err := b.CreateTask(ctx, store.CreateTaskInput{
			Title:                *title,
			Mission:              *mission,
			Priority:             *priority,
			AssignedAgentID:      *assignedAgent,
			RequiredCapabilities: required,
			MachineConstraints:   machines,
			Instructions:         *instructions,
			ParentTaskID:         *parent,
			ProjectID:            *project,
			ActorType:            *actorType,
			ActorID:              *actor,
		})
		if err != nil {
			return err
		}
		return printJSON(stdout, task)
	case "list":
		fs := flag.NewFlagSet("task list", flag.ContinueOnError)
		status := fs.String("status", "", "status")
		project := fs.String("project", "", "project slug")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		return printTasks(ctx, b, *status, *project, stdout)
	case "show":
		if len(args) < 2 {
			return errors.New("usage: agent-board task show TASK_ID")
		}
		task, err := b.GetTask(ctx, args[1])
		if err != nil {
			return err
		}
		return printJSON(stdout, task)
	case "tree":
		if len(args) < 2 {
			return errors.New("usage: agent-board task tree TASK_ID")
		}
		tree, err := b.GetTaskTree(ctx, args[1])
		if err != nil {
			return err
		}
		return printJSON(stdout, tree)
	case "review":
		return runTaskAction(ctx, b, args[1:], stdout, "task review", b.SubmitTaskForReview)
	case "approve":
		return runTaskAction(ctx, b, args[1:], stdout, "task approve", func(ctx context.Context, input services.TaskActionInput) (domain.Task, error) {
			return b.ApproveTask(ctx, input, false)
		})
	case "send-back":
		return runTaskAction(ctx, b, args[1:], stdout, "task send-back", b.SendTaskBack)
	case "complete":
		return runTaskAction(ctx, b, args[1:], stdout, "task complete", b.CompleteTask)
	case "block":
		return runTaskAction(ctx, b, args[1:], stdout, "task block", b.BlockTask)
	case "fail":
		return runTaskAction(ctx, b, args[1:], stdout, "task fail", b.FailTask)
	case "cancel":
		return runTaskAction(ctx, b, args[1:], stdout, "task cancel", b.CancelTask)
	default:
		return fmt.Errorf("unknown task command %q", args[0])
	}
}

func runTaskAction(ctx context.Context, b *services.Board, args []string, stdout io.Writer, usageName string, action func(context.Context, services.TaskActionInput) (domain.Task, error)) error {
	input, err := parseTaskActionArgs(args)
	if err != nil {
		return err
	}
	if input.TaskID == "" {
		return fmt.Errorf("usage: agent-board %s TASK_ID [--actor AGENT_ID] [--actor-type TYPE] [--lease LEASE_ID] [--reason TEXT] [--assign-type TYPE] [--assign-to ID]", usageName)
	}
	task, err := action(ctx, input)
	if err != nil {
		return err
	}
	return printJSON(stdout, task)
}

func parseTaskActionArgs(args []string) (services.TaskActionInput, error) {
	input := services.TaskActionInput{ActorType: "agent"}
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch {
		case arg == "--actor":
			i++
			if i >= len(args) {
				return services.TaskActionInput{}, errors.New("--actor requires a value")
			}
			input.ActorID = args[i]
		case strings.HasPrefix(arg, "--actor="):
			input.ActorID = strings.TrimPrefix(arg, "--actor=")
		case arg == "--actor-type":
			i++
			if i >= len(args) {
				return services.TaskActionInput{}, errors.New("--actor-type requires a value")
			}
			input.ActorType = args[i]
		case strings.HasPrefix(arg, "--actor-type="):
			input.ActorType = strings.TrimPrefix(arg, "--actor-type=")
		case arg == "--lease":
			i++
			if i >= len(args) {
				return services.TaskActionInput{}, errors.New("--lease requires a value")
			}
			input.LeaseID = args[i]
		case strings.HasPrefix(arg, "--lease="):
			input.LeaseID = strings.TrimPrefix(arg, "--lease=")
		case arg == "--reason":
			i++
			if i >= len(args) {
				return services.TaskActionInput{}, errors.New("--reason requires a value")
			}
			input.Reason = args[i]
		case strings.HasPrefix(arg, "--reason="):
			input.Reason = strings.TrimPrefix(arg, "--reason=")
		case arg == "--assign-type":
			i++
			if i >= len(args) {
				return services.TaskActionInput{}, errors.New("--assign-type requires a value")
			}
			input.AssignType = args[i]
		case strings.HasPrefix(arg, "--assign-type="):
			input.AssignType = strings.TrimPrefix(arg, "--assign-type=")
		case arg == "--assign-to":
			i++
			if i >= len(args) {
				return services.TaskActionInput{}, errors.New("--assign-to requires a value")
			}
			input.AssignTo = args[i]
		case strings.HasPrefix(arg, "--assign-to="):
			input.AssignTo = strings.TrimPrefix(arg, "--assign-to=")
		case strings.HasPrefix(arg, "-"):
			return services.TaskActionInput{}, fmt.Errorf("unknown task action flag %q", arg)
		default:
			if input.TaskID != "" {
				return services.TaskActionInput{}, errors.New("task action accepts one TASK_ID")
			}
			input.TaskID = arg
		}
	}
	return input, nil
}

func runMessage(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: agent-board message send|ack|list")
	}
	switch args[0] {
	case "send":
		fs := flag.NewFlagSet("message send", flag.ContinueOnError)
		taskID := fs.String("task", "", "task id")
		fromType := fs.String("from-type", "agent", "sender actor type")
		fromID := fs.String("from", "", "sender actor id")
		toType := fs.String("to-type", "agent", "recipient actor type")
		toID := fs.String("to", "", "recipient actor id")
		kind := fs.String("kind", "request", "message kind")
		body := fs.String("body", "", "message body")
		requiresAck := fs.Bool("requires-ack", false, "requires acknowledgement")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		msg, err := b.CreateMessage(ctx, store.CreateMessageInput{
			TaskID:        *taskID,
			FromActorType: *fromType,
			FromActorID:   *fromID,
			ToActorType:   *toType,
			ToActorID:     *toID,
			Kind:          *kind,
			Body:          *body,
			RequiresAck:   *requiresAck,
		})
		if err != nil {
			return err
		}
		return printJSON(stdout, msg)
	case "ack":
		messageID, actor, actorType, err := parseMessageAckArgs(args[1:])
		if err != nil {
			return err
		}
		if messageID == "" {
			return errors.New("usage: agent-board message ack MESSAGE_ID [--actor ID] [--actor-type TYPE (default agent)]")
		}
		msg, err := b.AcknowledgeMessage(ctx, messageID, actorType, actor)
		if err != nil {
			return err
		}
		return printJSON(stdout, msg)
	case "list":
		fs := flag.NewFlagSet("message list", flag.ContinueOnError)
		taskID := fs.String("task", "", "task id")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if *taskID == "" {
			return errors.New("usage: agent-board message list --task TASK_ID")
		}
		messages, err := b.ListTaskMessages(ctx, *taskID)
		if err != nil {
			return err
		}
		return printJSON(stdout, messages)
	default:
		return fmt.Errorf("unknown message command %q", args[0])
	}
}

func parseMessageAckArgs(args []string) (string, string, string, error) {
	var messageID string
	var actor string
	var actorType string // Empty preserves the store's legacy agent default.
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch {
		case arg == "--actor":
			i++
			if i >= len(args) {
				return "", "", "", errors.New("--actor requires a value")
			}
			actor = args[i]
		case strings.HasPrefix(arg, "--actor="):
			actor = strings.TrimPrefix(arg, "--actor=")
		case arg == "--actor-type":
			i++
			if i >= len(args) {
				return "", "", "", errors.New("--actor-type requires a value")
			}
			actorType = args[i]
		case strings.HasPrefix(arg, "--actor-type="):
			actorType = strings.TrimPrefix(arg, "--actor-type=")
		case strings.HasPrefix(arg, "-"):
			return "", "", "", fmt.Errorf("unknown message ack flag %q", arg)
		default:
			if messageID != "" {
				return "", "", "", errors.New("usage: agent-board message ack MESSAGE_ID [--actor ID] [--actor-type TYPE (default agent)]")
			}
			messageID = arg
		}
	}
	return messageID, actor, actorType, nil
}

func runArtifact(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: agent-board artifact add|list")
	}
	switch args[0] {
	case "add":
		fs := flag.NewFlagSet("artifact add", flag.ContinueOnError)
		taskID := fs.String("task", "", "task id")
		agentID := fs.String("agent", "", "agent id")
		kind := fs.String("kind", "file", "artifact kind")
		path := fs.String("path", "", "path or URL")
		summary := fs.String("summary", "", "summary")
		hash := fs.String("hash", "", "hash")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		artifact, err := b.AddArtifact(ctx, store.AddArtifactInput{
			TaskID:    *taskID,
			AgentID:   *agentID,
			Kind:      *kind,
			PathOrURL: *path,
			Summary:   *summary,
			Hash:      *hash,
		})
		if err != nil {
			return err
		}
		return printJSON(stdout, artifact)
	case "list":
		fs := flag.NewFlagSet("artifact list", flag.ContinueOnError)
		taskID := fs.String("task", "", "task id")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if *taskID == "" {
			return errors.New("usage: agent-board artifact list --task TASK_ID")
		}
		artifacts, err := b.ListArtifacts(ctx, *taskID)
		if err != nil {
			return err
		}
		return printJSON(stdout, artifacts)
	default:
		return fmt.Errorf("unknown artifact command %q", args[0])
	}
}

func runPoll(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("poll", flag.ContinueOnError)
	agent := fs.String("agent", "", "agent id")
	machine := fs.String("machine", "", "machine id")
	leaseSeconds := fs.Int("lease-seconds", domain.DefaultLeaseSeconds, "lease seconds")
	var caps repeatFlag
	fs.Var(&caps, "capability", "capability")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *agent == "" || *machine == "" {
		return errors.New("--agent and --machine are required")
	}
	claimed, err := b.PollAndClaim(ctx, store.PollInput{
		AgentID:      *agent,
		MachineID:    *machine,
		Capabilities: caps,
		LeaseSeconds: *leaseSeconds,
	})
	if errors.Is(err, store.ErrNoTask) {
		fmt.Fprintln(stdout, "no eligible task")
		return nil
	}
	if err != nil {
		return err
	}
	return printJSON(stdout, claimed)
}

func runLease(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 || args[0] != "renew" {
		return errors.New("usage: agent-board lease renew LEASE_ID [--lease-seconds N]")
	}
	fs := flag.NewFlagSet("lease renew", flag.ContinueOnError)
	leaseSeconds := fs.Int("lease-seconds", domain.DefaultLeaseSeconds, "lease seconds")
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return errors.New("usage: agent-board lease renew LEASE_ID [--lease-seconds N]")
	}
	lease, err := b.RenewLease(ctx, fs.Arg(0), *leaseSeconds)
	if err != nil {
		return err
	}
	return printJSON(stdout, lease)
}

func runStale(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) == 0 {
		leases, err := b.ListStaleLeases(ctx)
		if err != nil {
			return err
		}
		return printJSON(stdout, leases)
	}
	if args[0] != "expire" {
		return errors.New("usage: agent-board stale [expire]")
	}
	count, err := b.ExpireStaleLeases(ctx)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "expired %d stale leases\n", count)
	return nil
}

func runStatus(ctx context.Context, b *services.Board, stdout io.Writer) error {
	status, err := b.Status(ctx)
	if err != nil {
		return err
	}
	return printJSON(stdout, status)
}

func runSnapshot(ctx context.Context, b *services.Board, stdout io.Writer) error {
	snapshot, err := b.Snapshot(ctx)
	if err != nil {
		return err
	}
	return printJSON(stdout, snapshot)
}

func runProjectState(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("project-state", flag.ContinueOnError)
	project := fs.String("project", "", "project slug")
	if err := fs.Parse(args); err != nil {
		return err
	}
	states, err := b.ProjectStates(ctx, *project)
	if err != nil {
		return err
	}
	return printJSON(stdout, states)
}

func runMetrics(ctx context.Context, b *services.Board, stdout io.Writer) error {
	snapshot, err := b.Snapshot(ctx)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "agent_board_tasks_ready_total %d\n", snapshot.Status.ReadyTasks)
	fmt.Fprintf(stdout, "agent_board_tasks_running_total %d\n", snapshot.Status.ClaimedTasks)
	fmt.Fprintf(stdout, "agent_board_tasks_waiting_total %d\n", snapshot.Status.WaitingTasks)
	fmt.Fprintf(stdout, "agent_board_tasks_blocked_total %d\n", snapshot.Status.BlockedTasks)
	fmt.Fprintf(stdout, "agent_board_tasks_review_total %d\n", snapshot.Status.ReviewTasks)
	fmt.Fprintf(stdout, "agent_board_tasks_failed_total %d\n", snapshot.Status.FailedTasks)
	fmt.Fprintf(stdout, "agent_board_tasks_done_total %d\n", snapshot.Status.DoneTasks)
	for _, item := range snapshot.ReadyByPriority {
		fmt.Fprintf(stdout, "agent_board_tasks_ready_by_priority_total{priority=%q} %d\n", item.Label, item.Count)
	}
	fmt.Fprintf(stdout, "agent_board_agents_online_total %d\n", snapshot.Status.OnlineAgents)
	fmt.Fprintf(stdout, "agent_board_machines_online_total %d\n", snapshot.Status.OnlineMachines)
	fmt.Fprintf(stdout, "agent_board_leases_active_total %d\n", len(snapshot.ActiveLeases))
	fmt.Fprintf(stdout, "agent_board_leases_stale_total %d\n", len(snapshot.StaleLeases))
	return nil
}

func runInbox(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("inbox", flag.ContinueOnError)
	agent := fs.String("agent", "", "agent id")
	toType := fs.String("to-type", "agent", "recipient actor type")
	to := fs.String("to", "", "recipient actor id")
	if err := fs.Parse(args); err != nil {
		return err
	}
	toID := *to
	if *agent != "" {
		toID = *agent
		*toType = "agent"
	}
	if toID == "" {
		return errors.New("usage: agent-board inbox --agent AGENT_ID")
	}
	messages, err := b.ListInbox(ctx, *toType, toID)
	if err != nil {
		return err
	}
	return printJSON(stdout, messages)
}

func runEvents(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("events", flag.ContinueOnError)
	taskID := fs.String("task", "", "task id")
	if err := fs.Parse(args); err != nil {
		return err
	}
	events, err := b.ListEvents(ctx, *taskID)
	if err != nil {
		return err
	}
	return printJSON(stdout, events)
}

func runRoster(ctx context.Context, b *services.Board, args []string, stdout io.Writer) error {
	if len(args) > 1 {
		return errors.New("usage: agent-board roster [employee-id|name]")
	}
	if len(args) == 1 {
		detail, err := b.EmployeeDetail(ctx, args[0])
		if err != nil {
			return err
		}
		return printJSON(stdout, detail)
	}
	snapshot := b.EmployeeRoster(ctx)
	writer := tabwriter.NewWriter(stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(writer, "NAME\tSTATUS\tCURRENT STAGE / ACTIVITY\tNEXT FIRE\tLAST OUTCOME\tPAUSE REASON")
	for _, employee := range snapshot.Employees {
		fmt.Fprintf(writer, "%s\t%s\t%s\t%s\t%s\t%s\n", employee.Name, employee.Status, displayActivity(employee), displayNextFire(employee), employee.LastOutcome, employee.PauseReason)
	}
	if err := writer.Flush(); err != nil {
		return err
	}
	for _, sourceErr := range snapshot.SourceErrors {
		fmt.Fprintf(stdout, "source warning: %s\n", sourceErr)
	}
	return nil
}

func displayActivity(employee roster.Row) string {
	if employee.Activity == "" {
		return employee.CurrentStage
	}
	if employee.CurrentStage == "" {
		return employee.Activity
	}
	return employee.CurrentStage + ": " + employee.Activity
}

func displayNextFire(employee roster.Row) string {
	if employee.Status == "paused" && employee.NextFire != "" {
		return employee.NextFire + " [non-runnable]"
	}
	return employee.NextFire
}
func printTasks(ctx context.Context, b *services.Board, status string, project string, stdout io.Writer) error {
	tasks, err := b.ListTasks(ctx, status, project)
	if err != nil {
		return err
	}
	return printJSON(stdout, tasks)
}

func usage(stdout io.Writer) {
	fmt.Fprintln(stdout, "usage: agent-board [--db PATH] <server|auth|db|machine|agent|token|machines|agents|task|message|artifact|poll|lease|stale|status|snapshot|project-state|metrics|queue|running|inbox|events|roster|active-state>")
}

func printJSON(stdout io.Writer, value any) error {
	encoder := json.NewEncoder(stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

type repeatFlag []string

func (r *repeatFlag) String() string {
	return strings.Join(*r, ",")
}

func (r *repeatFlag) Set(value string) error {
	*r = append(*r, value)
	return nil
}

func fallback(value, defaultValue string) string {
	if value != "" {
		return value
	}
	return defaultValue
}

func hostName() string {
	name, err := os.Hostname()
	if err != nil {
		return ""
	}
	return name
}

func generateToken() (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return "ab_" + hex.EncodeToString(b[:]), nil
}
