"""The employee harness — rank 2 of the build order.

Not an employee. The contract every employee implements, the frozen envelope
every artifact carries across an edge, and the doubles that let employee N be
built against fake neighbours instead of live ones.

`docs/workforce-build-order.md` calls this a precondition for building E3, not
a follow-on: without it, building employee N+1 requires standing up employee N
live, which is the stall predicted at roughly employee two.
"""

from . import doubles
from .assignment import (
    ASSIGNMENT_SCHEMA_VERSION,
    AUTHORITY_GRANT_SCHEMA_VERSION,
    AUTHORITY_STATUS_SCHEMA_VERSION,
    AssignmentEnvelope,
    AssignmentError,
    AssignmentInputReference,
    AuthorityGrant,
    AuthorityStatus,
    validate_authority_grant,
    validate_authority_status,
)
from .contract import Employee, EmployeeSpec, InputSpec, Outcome, RunContext
from .deployment import (
    DEPLOYMENT_SCHEMA_VERSION,
    EMPLOYEE_SPEC_SCHEMA_VERSION,
    TRANSITION_SCHEMA_VERSION,
    BoardIdentity,
    CommissioningLineage,
    DeliveryProfileReference,
    DeploymentError,
    DeploymentRecord,
    EmployeeSpecReference,
    LifecycleState,
    LifecycleTransition,
    WorkspaceBinding,
    deployment_digest_of,
    employee_spec_body,
    employee_spec_digest,
    is_legal_transition,
    validate_deployment,
)
from .envelope import (
    AWAITING_HUMAN,
    Artifact,
    ArtifactStatus,
    DegradationBehavior,
    Determination,
    Envelope,
    EnvelopeError,
    Flag,
    FlagSeverity,
    InputRef,
    Producer,
    RefusalRecord,
    digest_of,
    validate,
)

__all__ = [
    "DEPLOYMENT_SCHEMA_VERSION",
    "EMPLOYEE_SPEC_SCHEMA_VERSION",
    "ASSIGNMENT_SCHEMA_VERSION",
    "AUTHORITY_GRANT_SCHEMA_VERSION",
    "AUTHORITY_STATUS_SCHEMA_VERSION",
    "AssignmentEnvelope",
    "AssignmentError",
    "AssignmentInputReference",
    "AuthorityGrant",
    "AuthorityStatus",
    "BoardIdentity",
    "CommissioningLineage",
    "DeliveryProfileReference",
    "DeploymentError",
    "DeploymentRecord",
    "EmployeeSpecReference",
    "LifecycleState",
    "LifecycleTransition",
    "WorkspaceBinding",
    "deployment_digest_of",
    "employee_spec_body",
    "employee_spec_digest",
    "is_legal_transition",
    "validate_deployment",
    "validate_authority_grant",
    "validate_authority_status",
    "AWAITING_HUMAN",
    "Artifact",
    "ArtifactStatus",
    "DegradationBehavior",
    "Determination",
    "Employee",
    "EmployeeSpec",
    "Envelope",
    "EnvelopeError",
    "Flag",
    "FlagSeverity",
    "InputRef",
    "InputSpec",
    "Outcome",
    "Producer",
    "RefusalRecord",
    "RunContext",
    "digest_of",
    "doubles",
    "validate",
]
