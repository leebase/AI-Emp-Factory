// Human operator session seam. The browser authenticates with the HttpOnly
// same-origin cookie issued by the existing LocalAuthProvider login flow and
// never holds a machine bearer secret. Machine identities keep their bearer
// seam untouched but may not reach human administrative operations.
package httpapi

import (
	"errors"
	"net/http"
	"net/url"
	"strings"

	"agent-board/internal/auth"
	"agent-board/internal/domain"
)

var (
	errCrossOriginSession = errors.New("cross-origin session mutation refused")

	errMachineHumanAdmin = errors.New("human administrative operations require an authenticated human session")
)

// sessionView is what a signed-in browser is told about itself. It carries no
// credential of any kind: no session token, no machine secret, no password.
// The dashboard renders sign-in/sign-out state and honest permission messages
// from these fields alone.
type sessionView struct {
	Authenticated bool     `json:"authenticated"`
	ActorType     string   `json:"actor_type"`
	ActorID       string   `json:"actor_id"`
	DisplayName   string   `json:"display_name,omitempty"`
	Role          string   `json:"role,omitempty"`
	Roles         []string `json:"roles,omitempty"`
	AuthProvider  string   `json:"auth_provider,omitempty"`
	CanAct        bool     `json:"can_act"`
	LoginPath     string   `json:"login_path"`
	LogoutPath    string   `json:"logout_path"`
}

func newSessionView(identity domain.AuthIdentity) sessionView {
	view := sessionView{
		Authenticated: true,
		ActorType:     identity.ActorType,
		ActorID:       identity.ActorID,
		DisplayName:   identity.DisplayName,
		Role:          identity.Role,
		AuthProvider:  identity.AuthProvider,
		LoginPath:     "/auth/login",
		LogoutPath:    "/auth/logout",
	}
	view.Roles = append([]string(nil), identity.Roles...)
	view.CanAct = identity.ActorType == "human" && auth.RoleAtLeast(identity.Role, auth.RoleOperator)
	return view
}

// sessionInfo answers "who am I, and what may I do?" for the dashboard. The
// surrounding middleware has already rejected anonymous and invalid or expired
// credentials with 401, so reaching this handler means the caller is
// authenticated right now.
func (s *Server) sessionInfo(w http.ResponseWriter, r *http.Request) {
	// The answer is derived from this caller's own credential, so it must never
	// be stored or replayed for a different one: no shared or browser cache may
	// hand one operator's identity to the next, and any cache that does key on
	// the request must key on the credential that produced it.
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Vary", "Cookie, Authorization")
	identity, ok := authIdentity(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, auth.ErrNoCredentials)
		return
	}
	writeJSON(w, http.StatusOK, newSessionView(identity))
}

// isHumanAdminRoute classifies the operations that only an authenticated human
// may perform. Machine bearer identities authenticate with an empty effective
// Role and therefore bypass the human role hierarchy by design; without an
// explicit route rule, a machine configured with an "admin" role string would
// depend on each handler remembering to re-check the actor type. Every machine
// identity is refused here — legacy, participant and observation producer
// alike — before any handler runs.
func isHumanAdminRoute(r *http.Request) bool {
	if strings.HasPrefix(r.URL.Path, "/auth/admin/") {
		return true
	}
	if r.Method != http.MethodPost {
		return false
	}
	// POST /api/review-tasks/{mission}/{run}/{gate}/{action} is the human review
	// disposition. Its read seam (one segment shorter) stays machine-readable.
	if strings.HasPrefix(r.URL.Path, "/api/review-tasks/") {
		return len(strings.Split(strings.Trim(r.URL.Path, "/"), "/")) == 6
	}
	return false
}

// denyMachineHumanAdmin refuses a non-human identity on a human administrative
// route and reports whether the request was refused.
func (s *Server) denyMachineHumanAdmin(w http.ResponseWriter, r *http.Request, identity domain.AuthIdentity) bool {
	if !isHumanAdminRoute(r) || identity.ActorType == "human" {
		return false
	}
	writeError(w, http.StatusForbidden, errMachineHumanAdmin)
	return true
}

// enforceSameOriginMutation protects cookie-authenticated state changes,
// including the login and logout seams themselves, from cross-origin browser
// requests. Three properties matter:
//
//  1. The machine seam is untouched. A request carrying an Authorization header
//     returns immediately, so the CLI, workers and observation producers keep
//     working from any origin and with no extra header.
//  2. A request with no Origin at all is allowed. Browsers always attach an
//     Origin to a cross-origin mutation, so its absence means the caller is not
//     a cross-site browser — this is what keeps the existing curl/form provider
//     login recipe working.
//  3. An Origin that is present must match the request host exactly. An opaque
//     "null" origin (sandboxed iframe, some redirect chains) is refused rather
//     than treated as absent.
func enforceSameOriginMutation(r *http.Request) error {
	switch r.Method {
	case http.MethodGet, http.MethodHead, http.MethodOptions:
		return nil
	}
	if auth.HasBearerCredential(r) {
		return nil
	}
	if strings.EqualFold(strings.TrimSpace(r.Header.Get("Sec-Fetch-Site")), "cross-site") {
		return errCrossOriginSession
	}
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	if origin == "" {
		return nil
	}
	if strings.EqualFold(origin, "null") {
		return errCrossOriginSession
	}
	parsed, err := url.Parse(origin)
	if err != nil || parsed.Host == "" {
		return errCrossOriginSession
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return errCrossOriginSession
	}
	if !strings.EqualFold(parsed.Host, r.Host) {
		return errCrossOriginSession
	}
	return nil
}
