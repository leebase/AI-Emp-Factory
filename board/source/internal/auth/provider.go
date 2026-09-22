// Package auth contains the provider-neutral authentication seam used by the
// Agent Board. Authorization consumes domain.AuthIdentity and never knows
// whether the identity came from local credentials or a future enterprise
// provider.
package auth

import (
	"context"
	"errors"
	"net/http"

	"agent-board/internal/domain"
)

var (
	ErrNoCredentials      = errors.New("no credentials")
	ErrInvalidCredentials = errors.New("invalid credentials")
)

// AuthenticationProvider authenticates both human sessions and machine
// bearer credentials. A provider returns ErrNoCredentials when a request has
// no credential at all; an invalid or disabled credential is an error and
// must never fall through to anonymous/public-read handling. Future
// implementations may be EntraOIDCProvider, GenericOIDCProvider,
// AuthentikProvider, or KeycloakProvider; none is part of this deployment.
type AuthenticationProvider interface {
	Name() string
	RegisterRoutes(*http.ServeMux)
	AuthenticateRequest(context.Context, *http.Request) (domain.AuthIdentity, error)
}

// ReloadableAuthenticationProvider is an optional capability for providers
// whose configuration can be refreshed without restarting the Board process.
// Authorization does not depend on this capability; it is an operational
// lifecycle seam for local providers and future providers may implement it
// according to their own configuration semantics.
type ReloadableAuthenticationProvider interface {
	AuthenticationProvider
	Reload() error
}

// MachineTokenResolver is the compatibility seam for existing hashed
// per-agent API tokens. New deployments should use LocalAuthProvider's
// mode-0600 machine configuration; this resolver lets already-issued Board
// tokens continue to work while callers migrate.
type MachineTokenResolver interface {
	ResolveAPIToken(context.Context, string) (domain.AuthIdentity, error)
}
