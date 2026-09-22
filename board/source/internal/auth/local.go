package auth

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"agent-board/internal/domain"
	"agent-board/internal/operations"
)

const (
	ProviderLocal = "local"
	localCookie   = "agent_board_local_session"
)

// SessionCookieName is the HttpOnly, same-origin browser session cookie issued
// by POST /auth/login. It is exported so the HTTP layer and its security tests
// can reason about the human session seam without duplicating the literal. The
// cookie carries a signed session envelope, never a machine bearer secret.
const SessionCookieName = localCookie

// HasBearerCredential reports whether a request presents an Authorization
// header. Machine clients (CLI, workers, producers) always do; browsers on the
// session seam never do. Same-origin enforcement uses this to leave the machine
// seam untouched.
func HasBearerCredential(r *http.Request) bool {
	return strings.TrimSpace(r.Header.Get("Authorization")) != ""
}

type User struct {
	DisplayName  string   `json:"display_name"`
	Roles        []string `json:"roles"`
	Enabled      bool     `json:"enabled"`
	PasswordHash string   `json:"password_hash"`
}

type Machine struct {
	Name      string   `json:"name"`
	Roles     []string `json:"roles"`
	Enabled   bool     `json:"enabled"`
	APISecret string   `json:"api_secret"`

	// IngestOwner is an optional single closed-owner production grant. When
	// non-empty, this machine may publish owner-scoped observation envelopes
	// only for this owner through the operations ingestion seam, and is
	// restricted to that write seam. ADR-001 gives each producer credential
	// exactly one owner. It never grants generic operator write privileges, and
	// the reserved board owner may not be granted.
	IngestOwner string `json:"ingest_owner,omitempty"`

	// Reporter is an optional single mission-scoped Auto-Orch reporting grant.
	// A reporter has no participant, manager, or ingest-owner authority; its
	// effective role must be exactly RoleReporter.
	Reporter *ReporterGrant `json:"reporter,omitempty"`

	// Participant marks a least-privilege v2 principal. When set, the principal
	// is only accepted if its Factory employee/board binding verifies against
	// the trusted registry (see ParticipantBinder). EmployeeID, BoardRef and
	// AgentID are then required; MachineID is the distinct physical host and
	// RecordDigest pins the commissioned DeploymentRecord revision so a changed
	// or removed record fails closed as stale.
	Participant  bool   `json:"participant,omitempty"`
	EmployeeID   string `json:"employee_id,omitempty"`
	BoardRef     string `json:"board_ref,omitempty"`
	AgentID      string `json:"agent_id,omitempty"`
	MachineID    string `json:"machine_id,omitempty"`
	RecordDigest string `json:"record_digest,omitempty"`

	// Immutable approved grant for the bound Board agent. Required for a
	// participant; registration and claim derive from these, never from the
	// caller.
	AgentKind         string   `json:"agent_kind,omitempty"`
	AgentRole         string   `json:"agent_role,omitempty"`
	AgentCapabilities []string `json:"agent_capabilities,omitempty"`
}

// ReporterGrant identifies the one mission and employee for which a machine
// may publish cycle reports. Both values are validated and normalized before
// the local-auth state becomes active.
type ReporterGrant struct {
	Mission    string `json:"mission"`
	EmployeeID string `json:"employee_id"`
}

// ParticipantBinder verifies an authoritative Factory employee/board binding
// against the trusted deployment registry. A non-nil error fails the
// authentication closed; it must never be treated as an anonymous or legacy
// fallback. Implementations must reject unknown, inactive, mismatched, or stale
// (record-digest-changed) bindings.
type ParticipantBinder interface {
	VerifyParticipantBinding(principalID, employeeID, boardRef, recordDigest, agentID, machineID string) error
}

type FileConfig struct {
	SessionSecret string             `json:"session_secret"`
	Users         map[string]User    `json:"users"`
	Machines      map[string]Machine `json:"machines"`
}

type Config struct {
	SessionSecret []byte
	Users         map[string]User
	Machines      map[string]Machine
	SessionTTL    time.Duration
	SecureCookies bool
	// Binder optionally overrides the participant binding verifier derived from
	// the resolver. Tests and embedders may supply one explicitly.
	Binder ParticipantBinder
}

type localAuthState struct {
	users         map[string]User
	machines      map[string]Machine
	sessionSecret []byte
}

type LocalAuthProvider struct {
	state         atomic.Pointer[localAuthState]
	resolver      MachineTokenResolver
	binder        ParticipantBinder
	sessionTTL    time.Duration
	secureCookies bool
	now           func() time.Time
	configPath    string
	reloadMu      sync.Mutex
}

func NewLocalAuthProvider(cfg Config, resolver MachineTokenResolver) (*LocalAuthProvider, error) {
	if cfg.SessionTTL <= 0 {
		cfg.SessionTTL = 2 * time.Hour
	}
	state, err := newLocalAuthState(cfg)
	if err != nil {
		return nil, err
	}
	provider := &LocalAuthProvider{resolver: resolver, sessionTTL: cfg.SessionTTL, secureCookies: cfg.SecureCookies, now: time.Now}
	// The Board service that resolves legacy per-agent API tokens also owns the
	// authoritative employee registry, so it doubles as the binding verifier
	// when it implements ParticipantBinder. If it does not, participant
	// credentials fail closed rather than silently authenticating.
	if binder, ok := resolver.(ParticipantBinder); ok {
		provider.binder = binder
	}
	if cfg.Binder != nil {
		provider.binder = cfg.Binder
	}
	provider.state.Store(state)
	return provider, nil
}

func LoadFile(path string, sessionTTL time.Duration, secureCookies bool, resolver MachineTokenResolver) (*LocalAuthProvider, error) {
	cleanPath := filepath.Clean(path)
	file, err := os.OpenFile(cleanPath, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("open local auth file %q: %w", path, err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, fmt.Errorf("stat local auth file %q: %w", path, err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		return nil, fmt.Errorf("local auth file %q must be a regular mode-0600 file", path)
	}
	var fileConfig FileConfig
	if err := decodeAuthFile(file, &fileConfig); err != nil {
		return nil, fmt.Errorf("decode local auth file %q: %w", path, err)
	}
	secret, err := decodeSessionSecret(fileConfig.SessionSecret)
	if err != nil {
		return nil, err
	}
	provider, err := NewLocalAuthProvider(Config{SessionSecret: secret, Users: fileConfig.Users, Machines: fileConfig.Machines, SessionTTL: sessionTTL, SecureCookies: secureCookies}, resolver)
	if err != nil {
		return nil, err
	}
	provider.configPath = cleanPath
	return provider, nil
}

func newLocalAuthState(cfg Config) (*localAuthState, error) {
	if len(cfg.SessionSecret) < 32 {
		return nil, fmt.Errorf("local auth session secret must be at least 32 bytes")
	}
	users := cloneUsers(cfg.Users)
	machines := cloneMachines(cfg.Machines)
	for username, user := range users {
		if strings.TrimSpace(username) == "" || user.PasswordHash == "" || len(user.Roles) == 0 {
			return nil, fmt.Errorf("invalid local user %q", username)
		}
		if effective := EffectiveRole(user.Roles); effective == "" {
			return nil, fmt.Errorf("local user %q has no recognized authorization role", username)
		}
		if !user.Enabled {
			continue
		}
		if err := validatePasswordHash(user.PasswordHash); err != nil {
			return nil, fmt.Errorf("local user %q password_hash: %w", username, err)
		}
	}
	for machineID, machine := range machines {
		if strings.TrimSpace(machineID) == "" || strings.TrimSpace(machine.Name) == "" || strings.TrimSpace(machine.APISecret) == "" || len(machine.Roles) == 0 {
			return nil, fmt.Errorf("invalid local machine %q", machineID)
		}
		if machine.Participant {
			if !containsRole(machine.Roles, RoleParticipant) {
				return nil, fmt.Errorf("participant machine %q requires the participant role", machineID)
			}
			if strings.TrimSpace(machine.IngestOwner) != "" {
				return nil, fmt.Errorf("participant machine %q cannot claim an ingest owner grant", machineID)
			}
			// A participant principal without a complete, pinned binding is a
			// misconfiguration and must refuse startup rather than fall back to
			// the broad legacy-machine behavior. The machine id and immutable
			// approved grant are mandatory.
			if strings.TrimSpace(machine.EmployeeID) == "" || strings.TrimSpace(machine.BoardRef) == "" || strings.TrimSpace(machine.AgentID) == "" || strings.TrimSpace(machine.MachineID) == "" || strings.TrimSpace(machine.RecordDigest) == "" {
				return nil, fmt.Errorf("participant machine %q requires employee_id, board_ref, agent_id, machine_id, and record_digest", machineID)
			}
			if strings.TrimSpace(machine.AgentKind) == "" || strings.TrimSpace(machine.AgentRole) == "" || len(machine.AgentCapabilities) == 0 {
				return nil, fmt.Errorf("participant machine %q requires agent_kind, agent_role, and agent_capabilities", machineID)
			}
		}
		if machine.Reporter != nil || containsRole(machine.Roles, RoleReporter) {
			if machine.Participant {
				return nil, fmt.Errorf("reporter machine %q cannot also be a participant", machineID)
			}
			if strings.TrimSpace(machine.IngestOwner) != "" {
				return nil, fmt.Errorf("reporter machine %q cannot claim an ingest owner grant", machineID)
			}
			if machine.Reporter == nil {
				return nil, fmt.Errorf("reporter machine %q requires a reporter grant", machineID)
			}
			if len(machine.Roles) != 1 || machine.Roles[0] != RoleReporter {
				return nil, fmt.Errorf("reporter machine %q requires exactly the reporter role", machineID)
			}
			grant, err := validateReporterGrant(machineID, *machine.Reporter)
			if err != nil {
				return nil, err
			}
			machine.Reporter = &grant
			machines[machineID] = machine
		}
		if strings.TrimSpace(machine.IngestOwner) != "" {
			normalized, err := validateIngestOwner(machineID, machine.IngestOwner)
			if err != nil {
				return nil, err
			}
			machine.IngestOwner = normalized
			machines[machineID] = machine
		}
	}
	// A participant bearer secret must identify exactly one configured machine.
	// If it collides with a legacy machine, map iteration could otherwise return
	// the broad legacy identity before the participant binding is checked.
	secretOwners := make(map[string]string, len(machines))
	for machineID, machine := range machines {
		if !machine.Enabled {
			continue
		}
		if previousID, exists := secretOwners[machine.APISecret]; exists {
			previous := machines[previousID]
			// A producer or participant credential must be unambiguous. Plain
			// legacy machine-to-machine sharing is left unchanged, but any
			// collision that involves a restricted identity fails startup
			// deterministically regardless of map order.
			if restrictedCredential(machine) || restrictedCredential(previous) {
				return nil, fmt.Errorf("API secret is shared by enabled machines %q and %q; restricted credentials must be unique", previousID, machineID)
			}
		}
		secretOwners[machine.APISecret] = machineID
	}
	return &localAuthState{users: users, machines: machines, sessionSecret: append([]byte(nil), cfg.SessionSecret...)}, nil
}

// decodeAuthFile strictly decodes a local auth file. Unknown fields are
// rejected so the retired plural `ingest_owners` grant cannot be silently
// ignored (ADR-001 grants exactly one owner per credential), and a trailing
// second JSON document is refused.
func decodeAuthFile(file io.Reader, fileConfig *FileConfig) error {
	decoder := json.NewDecoder(io.LimitReader(file, 2<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(fileConfig); err != nil {
		return err
	}
	if err := decoder.Decode(new(json.RawMessage)); err != io.EOF {
		return fmt.Errorf("unexpected trailing data after local auth file")
	}
	return nil
}

func decodeSessionSecret(encoded string) ([]byte, error) {
	secret, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encoded))
	if err != nil || len(secret) < 32 {
		return nil, fmt.Errorf("local auth session_secret must be base64 encoding of at least 32 bytes")
	}
	return secret, nil
}

func cloneUsers(source map[string]User) map[string]User {
	users := make(map[string]User, len(source))
	for username, user := range source {
		user.Roles = append([]string(nil), user.Roles...)
		users[username] = user
	}
	return users
}

func cloneMachines(source map[string]Machine) map[string]Machine {
	machines := make(map[string]Machine, len(source))
	for machineID, machine := range source {
		machine.Roles = append([]string(nil), machine.Roles...)
		machine.AgentCapabilities = append([]string(nil), machine.AgentCapabilities...)
		if machine.Reporter != nil {
			grant := *machine.Reporter
			machine.Reporter = &grant
		}
		machines[machineID] = machine
	}
	return machines
}

// restrictedCredential reports whether an enabled machine's bearer secret must
// be unique because it carries a restricted identity (a verified participant,
// reporter, or observation producer).
func restrictedCredential(machine Machine) bool {
	return machine.Participant || machine.Reporter != nil || strings.TrimSpace(machine.IngestOwner) != ""
}

// validateReporterGrant rejects an incomplete mission-scoped reporting grant
// before it can authenticate any request.
func validateReporterGrant(machineID string, raw ReporterGrant) (ReporterGrant, error) {
	mission := strings.TrimSpace(raw.Mission)
	employeeID := strings.TrimSpace(raw.EmployeeID)
	if mission == "" {
		return ReporterGrant{}, fmt.Errorf("machine %q reporter grant requires mission", machineID)
	}
	if employeeID == "" {
		return ReporterGrant{}, fmt.Errorf("machine %q reporter grant requires employee_id", machineID)
	}
	return ReporterGrant{Mission: mission, EmployeeID: employeeID}, nil
}

// validateIngestOwner rejects an invalid configured owner grant at startup. The
// grant is exactly one closed native owner; the reserved board owner is never
// grantable.
func validateIngestOwner(machineID, raw string) (string, error) {
	owner := strings.TrimSpace(raw)
	if owner == "" {
		return "", nil
	}
	if !operations.IsValidSourceOwner(owner) {
		return "", fmt.Errorf("machine %q has invalid ingest owner %q", machineID, raw)
	}
	if operations.IsBoardOwner(owner) {
		return "", fmt.Errorf("machine %q cannot claim the reserved board ingest owner", machineID)
	}
	return owner, nil
}

func containsRole(roles []string, wanted string) bool {
	for _, role := range roles {
		if strings.TrimSpace(role) == wanted {
			return true
		}
	}
	return false
}

// Reload validates a fresh complete auth file before atomically publishing it.
// A malformed or unreadable replacement leaves the currently active identity
// snapshot untouched.
func (p *LocalAuthProvider) Reload() error {
	if p.configPath == "" {
		return fmt.Errorf("local auth provider is not file-backed")
	}
	p.reloadMu.Lock()
	defer p.reloadMu.Unlock()

	file, err := os.OpenFile(p.configPath, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("open local auth file %q: %w", p.configPath, err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return fmt.Errorf("stat local auth file %q: %w", p.configPath, err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		return fmt.Errorf("local auth file %q must be a regular mode-0600 file", p.configPath)
	}
	var fileConfig FileConfig
	if err := decodeAuthFile(file, &fileConfig); err != nil {
		return fmt.Errorf("decode local auth file %q: %w", p.configPath, err)
	}
	secret, err := decodeSessionSecret(fileConfig.SessionSecret)
	if err != nil {
		return err
	}
	state, err := newLocalAuthState(Config{SessionSecret: secret, Users: fileConfig.Users, Machines: fileConfig.Machines})
	if err != nil {
		return err
	}
	p.state.Store(state)
	return nil
}

func GenerateMachineSecret() (string, error) {
	secret := make([]byte, 32)
	if _, err := rand.Read(secret); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(secret), nil
}

func (p *LocalAuthProvider) Name() string { return ProviderLocal }

func (p *LocalAuthProvider) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /auth/login", p.loginForm)
	mux.HandleFunc("POST /auth/login", p.login)
	mux.HandleFunc("POST /auth/logout", p.logout)
}

func (p *LocalAuthProvider) AuthenticateRequest(_ context.Context, r *http.Request) (domain.AuthIdentity, error) {
	state := p.state.Load()
	if authorization := strings.TrimSpace(r.Header.Get("Authorization")); authorization != "" {
		const prefix = "Bearer "
		if !strings.HasPrefix(authorization, prefix) || strings.TrimSpace(strings.TrimPrefix(authorization, prefix)) == "" {
			return domain.AuthIdentity{}, ErrInvalidCredentials
		}
		raw := strings.TrimSpace(strings.TrimPrefix(authorization, prefix))
		for machineID, machine := range state.machines {
			if !machine.Enabled || subtle.ConstantTimeCompare([]byte(raw), []byte(machine.APISecret)) != 1 {
				continue
			}
			identity := domain.AuthIdentity{
				ActorType: "machine", ActorID: machineID,
				Roles: append([]string(nil), machine.Roles...), AuthProvider: ProviderLocal,
				IngestOwner: machine.IngestOwner,
			}
			if machine.Reporter != nil {
				return reporterIdentity(machineID, machine), nil
			}
			if machine.Participant {
				return participantIdentity(p.binder, machineID, machine)
			}
			return identity, nil
		}
		if p.resolver != nil {
			identity, err := p.resolver.ResolveAPIToken(r.Context(), raw)
			if err == nil {
				identity.AuthProvider = ProviderLocal
				if len(identity.Roles) == 0 {
					identity.Roles = []string{"machine"}
				}
				return identity, nil
			}
		}
		return domain.AuthIdentity{}, ErrInvalidCredentials
	}
	cookie, err := r.Cookie(localCookie)
	if err != nil {
		return domain.AuthIdentity{}, ErrNoCredentials
	}
	session, err := decodeSession(state.sessionSecret, cookie.Value, p.now())
	if err != nil {
		return domain.AuthIdentity{}, ErrInvalidCredentials
	}
	user, ok := state.users[session.Username]
	if !ok || !user.Enabled {
		return domain.AuthIdentity{}, ErrInvalidCredentials
	}
	return domain.AuthIdentity{ActorType: "human", ActorID: session.Username, DisplayName: user.DisplayName, Role: EffectiveRole(user.Roles), Roles: append([]string(nil), user.Roles...), AuthProvider: ProviderLocal}, nil
}

func reporterIdentity(machineID string, machine Machine) domain.AuthIdentity {
	return domain.AuthIdentity{
		ActorType: "machine", ActorID: machineID,
		Role: RoleReporter, Roles: []string{RoleReporter}, AuthProvider: ProviderLocal,
		Reporter: true, ReporterMission: machine.Reporter.Mission, ReporterEmployeeID: machine.Reporter.EmployeeID,
	}
}

// participantIdentity builds a least-privilege participant identity only after
// the configured binding verifies against the trusted registry. A missing
// verifier or any verification failure is an invalid credential: it must never
// fall through to a legacy machine identity or resolved API token.
func participantIdentity(binder ParticipantBinder, machineID string, machine Machine) (domain.AuthIdentity, error) {
	if binder == nil {
		return domain.AuthIdentity{}, ErrInvalidCredentials
	}
	if err := binder.VerifyParticipantBinding(machineID, machine.EmployeeID, machine.BoardRef, machine.RecordDigest, machine.AgentID, strings.TrimSpace(machine.MachineID)); err != nil {
		return domain.AuthIdentity{}, ErrInvalidCredentials
	}
	return domain.AuthIdentity{
		ActorType:         "machine",
		ActorID:           machineID,
		Role:              RoleParticipant,
		Roles:             []string{RoleParticipant},
		AuthProvider:      ProviderLocal,
		Participant:       true,
		PrincipalID:       machineID,
		EmployeeID:        machine.EmployeeID,
		BoardRef:          machine.BoardRef,
		AgentID:           machine.AgentID,
		MachineID:         strings.TrimSpace(machine.MachineID),
		AgentKind:         machine.AgentKind,
		AgentRole:         machine.AgentRole,
		AgentCapabilities: append([]string(nil), machine.AgentCapabilities...),
	}, nil
}

func (p *LocalAuthProvider) loginForm(w http.ResponseWriter, r *http.Request) {
	returnTo := safeReturnPath(r.URL.Query().Get("return_to"))
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, fmt.Sprintf(localLoginHTML, template.HTMLEscapeString(returnTo)))
}

func (p *LocalAuthProvider) login(w http.ResponseWriter, r *http.Request) {
	username, password, returnTo, err := loginFields(r)
	if err != nil {
		http.Error(w, "invalid login request", http.StatusBadRequest)
		return
	}
	state := p.state.Load()
	user, ok := state.users[username]
	if !ok || !user.Enabled || !VerifyPassword(password, user.PasswordHash) {
		http.Error(w, "invalid username or password", http.StatusUnauthorized)
		return
	}
	encoded, err := encodeSession(state.sessionSecret, localSession{Username: username, Expiry: p.now().Add(p.sessionTTL)})
	if err != nil {
		http.Error(w, "login failed", http.StatusInternalServerError)
		return
	}
	http.SetCookie(w, &http.Cookie{Name: localCookie, Value: encoded, Path: "/", HttpOnly: true, Secure: p.secureCookies, SameSite: http.SameSiteLaxMode, MaxAge: int(p.sessionTTL.Seconds())})
	http.Redirect(w, r, returnTo, http.StatusFound)
}

func (p *LocalAuthProvider) logout(w http.ResponseWriter, _ *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: localCookie, Value: "", Path: "/", HttpOnly: true, Secure: p.secureCookies, SameSite: http.SameSiteLaxMode, MaxAge: -1})
	w.WriteHeader(http.StatusNoContent)
}

type localSession struct {
	Username string    `json:"username"`
	Expiry   time.Time `json:"exp"`
}

const localLoginHTML = `<!doctype html><html><head><meta charset="utf-8"><title>Sign in · agent-board</title></head><body><main><h1>Sign in to agent-board</h1><form method="post" action="/auth/login"><input type="hidden" name="return_to" value="%s"><label>Username <input name="username" autocomplete="username" required></label><label>Password <input name="password" type="password" autocomplete="current-password" required></label><button type="submit">Sign in</button></form></main></body></html>`

func loginFields(r *http.Request) (string, string, string, error) {
	if strings.Contains(r.Header.Get("Content-Type"), "application/json") {
		var body struct {
			Username string `json:"username"`
			Password string `json:"password"`
			ReturnTo string `json:"return_to"`
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&body); err != nil {
			return "", "", "", err
		}
		return strings.TrimSpace(body.Username), body.Password, safeReturnPath(body.ReturnTo), nil
	}
	if err := r.ParseForm(); err != nil {
		return "", "", "", err
	}
	return strings.TrimSpace(r.Form.Get("username")), r.Form.Get("password"), safeReturnPath(r.Form.Get("return_to")), nil
}

func safeReturnPath(path string) string {
	if path == "" || path[0] != '/' || (len(path) > 1 && (path[1] == '/' || path[1] == '\\')) {
		return "/"
	}
	for _, char := range path {
		if char < 0x20 || char == 0x7f {
			return "/"
		}
	}
	return path
}
