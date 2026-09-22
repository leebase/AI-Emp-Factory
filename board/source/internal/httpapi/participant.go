package httpapi

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"agent-board/internal/auth"
	"agent-board/internal/domain"
)

// isParticipant reports whether the request identity is a verified,
// least-privilege v2 participant. It is deliberately strict: both the
// Participant marker and the explicit participant role must be present, so a
// legacy machine or agent identity can never be treated as a participant.
func isParticipant(identity domain.AuthIdentity) bool {
	return identity.Participant && identity.Role == auth.RoleParticipant
}

// participantActor is the server-derived actor that a participant acts as:
// its verified Board agent id, never a caller-supplied value.
func participantActor(identity domain.AuthIdentity) (string, string) {
	return "agent", identity.AgentID
}

var errParticipantForbidden = errors.New("participant identity is not authorized for this operation")

// participantRouteAllowed is a default-deny allowance list. Any route a
// participant does not strictly need is forbidden here even before a handler
// runs, so a forgotten per-handler check cannot leak a manager/operator
// operation (task create/assign/cancel/approve/send-back, machine registration,
// auth reload, global reads, orchestrator writes, and so on).
func participantRouteAllowed(r *http.Request) bool {
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	switch r.Method {
	case http.MethodGet, http.MethodHead:
		switch {
		case r.URL.Path == "/tasks":
			return true
		case len(parts) == 2 && parts[0] == "tasks":
			return true
		case len(parts) == 3 && parts[0] == "tasks" && (parts[2] == "tree" || parts[2] == "events" || parts[2] == "messages" || parts[2] == "artifacts"):
			return true
		case r.URL.Path == "/inbox":
			return true
		case len(parts) == 3 && parts[0] == "artifacts" && parts[2] == "download":
			return true
		}
	case http.MethodPost:
		switch {
		case r.URL.Path == "/agents/register" || r.URL.Path == "/poll" || r.URL.Path == "/messages" || r.URL.Path == "/artifacts" || r.URL.Path == "/artifacts/upload":
			return true
		case len(parts) == 3 && parts[0] == "tasks" && (parts[2] == "review" || parts[2] == "complete" || parts[2] == "block" || parts[2] == "fail"):
			return true
		case len(parts) == 3 && parts[0] == "messages" && parts[2] == "ack":
			return true
		case len(parts) == 3 && parts[0] == "leases" && parts[2] == "renew":
			return true
		}
	}
	return false
}

// participantOwnsTask reports whether the participant's bound Board agent is
// the assigned owner of the task. Unassigned, another agent's, and unknown
// tasks all fail closed.
func (s *Server) participantOwnsTask(ctx context.Context, identity domain.AuthIdentity, taskID string) (domain.Task, bool) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return domain.Task{}, false
	}
	task, err := s.board.GetTask(ctx, taskID)
	if err != nil {
		return domain.Task{}, false
	}
	if task.AssignedAgentID == "" || task.AssignedAgentID != identity.AgentID {
		return domain.Task{}, false
	}
	return task, true
}

// treeFullyOwned reports whether every node in the task tree, including nested
// descendants, is assigned to the bound agent. A single unowned node denies the
// whole tree so a participant never receives another identity's task data.
func treeFullyOwned(tree domain.TaskTree, agentID string) bool {
	if tree.Task.AssignedAgentID == "" || tree.Task.AssignedAgentID != agentID {
		return false
	}
	for _, child := range tree.Children {
		if !treeFullyOwned(child, agentID) {
			return false
		}
	}
	return true
}

// filterParticipantTasks keeps only tasks assigned to the participant's bound
// Board agent.
func filterParticipantTasks(tasks []domain.Task, identity domain.AuthIdentity) []domain.Task {
	filtered := tasks[:0]
	for _, task := range tasks {
		if task.AssignedAgentID != "" && task.AssignedAgentID == identity.AgentID {
			filtered = append(filtered, task)
		}
	}
	return filtered
}
