package auth

const (
	RoleViewer   = "viewer"
	RoleReviewer = "reviewer"
	RoleOperator = "operator"
	RoleAdmin    = "admin"

	// RoleParticipant is the effective authorization role for a least-privilege
	// v2 participant principal. It is intentionally absent from roleRank: it is
	// not a human role and must never satisfy an operator/viewer threshold via
	// RoleAtLeast. Participant authorization is enforced by its own explicit
	// resource-scope policy, not by the human role hierarchy.
	RoleParticipant = "participant"

	// RoleReporter is the effective role for a mission-scoped Auto-Orch
	// reporter. Reporters are governed by an explicit resource-scope policy,
	// not by the human role hierarchy.
	RoleReporter = "reporter"
)

var roleRank = map[string]int{
	RoleViewer:   1,
	RoleReviewer: 1,
	RoleOperator: 2,
	RoleAdmin:    3,
}

// EffectiveRole maps a provider's role set to the existing authorization
// hierarchy. "reviewer" is the local-provider name for the existing
// client-reviewer/viewer permission boundary.
func EffectiveRole(roles []string) string {
	best := ""
	for _, role := range roles {
		if rank, ok := roleRank[role]; ok && (best == "" || rank > roleRank[best]) {
			if role == RoleReviewer {
				best = RoleViewer
			} else {
				best = role
			}
		}
	}
	return best
}

func RoleAtLeast(role, minimum string) bool {
	have, ok := roleRank[role]
	if !ok {
		return false
	}
	need, ok := roleRank[minimum]
	if !ok {
		return false
	}
	return have >= need
}

func IsHumanRole(role string) bool {
	return role == RoleAdmin || role == RoleOperator || role == RoleViewer || role == RoleReviewer
}
