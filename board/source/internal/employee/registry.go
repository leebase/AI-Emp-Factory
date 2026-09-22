package employee

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

const DeploymentSchemaVersion = "ai-employee-deployment/1.0"

var (
	identifierPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*$`)
	referencePattern  = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9+.-]*(?::[A-Za-z0-9][A-Za-z0-9._/-]*)?$`)
	digestPattern     = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
)

type Record struct {
	EmployeeID     string `json:"employee_id"`
	TenantID       string `json:"tenant_id"`
	DisplayName    string `json:"display_name"`
	LifecycleState string `json:"lifecycle_state"`
	MissionRef     string `json:"mission_ref"`
	WorkspaceRef   string `json:"workspace_ref"`
	BoardRef       string `json:"board_ref"`
	RecordDigest   string `json:"record_digest"`

	// Authoritative participation binding. These are projected only from the
	// deployment record's additive board_identity fields. Legacy records that
	// contain only board_ref remain loadable, but leave these fields empty and
	// therefore can never authorize a participant.
	ParticipantPrincipal string `json:"participant_principal,omitempty"`
	AgentID              string `json:"agent_id,omitempty"`
	MachineID            string `json:"machine_id,omitempty"`
}

type Registry struct {
	records map[string]Record
}

func Load(dir string) (*Registry, error) {
	dir = strings.TrimSpace(dir)
	if dir == "" {
		return nil, errors.New("employee registry directory is empty")
	}
	info, err := os.Stat(dir)
	if err != nil {
		return nil, fmt.Errorf("employee registry: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("employee registry is not a directory: %s", dir)
	}
	paths, err := filepath.Glob(filepath.Join(dir, "*.json"))
	if err != nil {
		return nil, fmt.Errorf("employee registry glob: %w", err)
	}
	if len(paths) == 0 {
		return nil, fmt.Errorf("employee registry contains no records: %s", dir)
	}
	records := make(map[string]Record, len(paths))
	for _, path := range paths {
		record, err := loadRecord(path)
		if err != nil {
			return nil, err
		}
		if _, exists := records[record.EmployeeID]; exists {
			return nil, fmt.Errorf("employee registry contains duplicate employee_id: %s", record.EmployeeID)
		}
		records[record.EmployeeID] = record
	}
	return &Registry{records: records}, nil
}

func loadRecord(path string) (Record, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Record{}, fmt.Errorf("employee record %s: %w", path, err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		return Record{}, fmt.Errorf("employee record %s is malformed JSON: %w", path, err)
	}
	if err := validateRecord(body, path); err != nil {
		return Record{}, err
	}
	var canonicalBuffer bytes.Buffer
	encoder := json.NewEncoder(&canonicalBuffer)
	// The Factory canonical writer (scripts/employee_registry.py, _write_canonical_json)
	// emits compact sorted-key JSON, but the records committed by Factory d126a4f
	// (2026-09-17, Architect commissioning) are sorted-key two-space pretty JSON.
	// Both real forms must load; any other whitespace or key order is still rejected
	// 2026-09-17 assignment-grant updates. Accept both exact Factory forms;
	// loading never rewrites the source record.
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(body); err != nil {
		return Record{}, fmt.Errorf("employee record %s cannot be canonicalized: %w", path, err)
	}
	canonical := bytes.TrimSuffix(canonicalBuffer.Bytes(), []byte{'\n'})
	if !bytes.Equal(raw, canonical) {
		var prettyBuffer bytes.Buffer
		prettyEncoder := json.NewEncoder(&prettyBuffer)
		prettyEncoder.SetEscapeHTML(false)
		prettyEncoder.SetIndent("", "  ")
		if err := prettyEncoder.Encode(body); err != nil {
			return Record{}, fmt.Errorf("employee record %s cannot be canonicalized: %w", path, err)
		}
		pretty := prettyBuffer.Bytes()
		prettyWithoutNewline := bytes.TrimSuffix(pretty, []byte{'\n'})
		if !bytes.Equal(raw, pretty) && !bytes.Equal(raw, prettyWithoutNewline) {
			return Record{}, fmt.Errorf("employee record %s is not canonical JSON", path)
		}
	}
	workspace := body["workspace_bindings"].([]any)[0].(map[string]any)
	board := body["board_identity"].(map[string]any)
	principal := strings.TrimSpace(stringValue(board["principal_ref"]))
	agentID := strings.TrimSpace(stringValue(board["agent_id"]))
	machineID := strings.TrimSpace(stringValue(board["machine_id"]))
	digest := sha256.Sum256(raw)
	return Record{
		EmployeeID:           body["employee_id"].(string),
		TenantID:             body["tenant_id"].(string),
		DisplayName:          body["display_name"].(string),
		LifecycleState:       body["lifecycle_state"].(string),
		MissionRef:           body["mission_ref"].(string),
		WorkspaceRef:         workspace["workspace_ref"].(string),
		BoardRef:             board["board_ref"].(string),
		RecordDigest:         "sha256:" + hex.EncodeToString(digest[:]),
		ParticipantPrincipal: principal,
		AgentID:              agentID,
		MachineID:            machineID,
	}, nil
}

func stringValue(value any) string {
	text, _ := value.(string)
	return text
}

func validateRecord(body map[string]any, path string) error {
	required := map[string]bool{
		"schema_version": true, "employee_id": true, "tenant_id": true, "display_name": true,
		"specification": true, "lifecycle_state": true, "mission_ref": true,
		"workspace_bindings": true, "delivery_profile_refs": true, "board_identity": true,
		"commissioning_lineage": true, "principal_refs": true, "credential_scope_refs": true,
		"created_by": true, "created_at": true, "updated_by": true, "updated_at": true,
	}
	if err := strictKeysOptional(body, required, map[string]bool{"assignment_workspace_refs": true}, "deployment record "+path); err != nil {
		return err
	}
	if body["schema_version"] != DeploymentSchemaVersion {
		return fmt.Errorf("employee record %s has unsupported schema version: %v", path, body["schema_version"])
	}
	employeeID, err := stringField(body, "employee_id")
	if err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	if !identifierPattern.MatchString(employeeID) {
		return fmt.Errorf("employee record %s has malformed employee_id", path)
	}
	if filepath.Base(path) != employeeID+".json" {
		return fmt.Errorf("employee record %s filename does not match employee_id %s", path, employeeID)
	}
	tenantID, err := stringField(body, "tenant_id")
	if err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	if !identifierPattern.MatchString(tenantID) || isTenantPlaceholder(tenantID) {
		return fmt.Errorf("employee record %s has malformed tenant_id", path)
	}
	if _, err := stringField(body, "display_name"); err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	state, err := stringField(body, "lifecycle_state")
	if err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	if !map[string]bool{"draft": true, "commissioning": true, "pilot": true, "scheduled": true, "paused": true, "retired": true, "archived": true}[state] {
		return fmt.Errorf("employee record %s has unsupported lifecycle_state: %s", path, state)
	}
	if _, err := referenceField(body, "mission_ref"); err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	if err := validateSpecification(body["specification"], path); err != nil {
		return err
	}
	if err := validateWorkspaces(body["workspace_bindings"], path); err != nil {
		return err
	}
	if assignmentRefs, ok := body["assignment_workspace_refs"]; ok {
		if err := validateAssignmentWorkspaceRefs(assignmentRefs, path); err != nil {
			return err
		}
	}
	if err := validateProfiles(body["delivery_profile_refs"], path); err != nil {
		return err
	}
	if err := validateBoardIdentity(body["board_identity"], path); err != nil {
		return err
	}
	if err := validateLineage(body["commissioning_lineage"], path); err != nil {
		return err
	}
	for _, field := range []string{"principal_refs", "credential_scope_refs"} {
		// principal_refs is non-empty in the canonical contract; credential
		// scope refs may legitimately be empty (an installation holding no
		// credential), so only the former requires a non-empty array.
		if err := validateReferenceArray(body[field], field, path, field == "credential_scope_refs"); err != nil {
			return err
		}
	}
	for _, field := range []string{"created_by", "updated_by"} {
		if _, err := referenceField(body, field); err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
	}
	for _, field := range []string{"created_at", "updated_at"} {
		value, err := stringField(body, field)
		if err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
		if _, err := time.Parse(time.RFC3339Nano, value); err != nil {
			return fmt.Errorf("employee record %s has malformed %s", path, field)
		}
	}
	return nil
}

func validateSpecification(value any, path string) error {
	object, ok := value.(map[string]any)
	if !ok {
		return fmt.Errorf("employee record %s specification must be an object", path)
	}
	if err := strictKeys(object, map[string]bool{"schema_version": true, "ref": true, "digest": true}, "specification"); err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	if object["schema_version"] != "ai-employee-spec/1.0" {
		return fmt.Errorf("employee record %s has unsupported specification schema version", path)
	}
	if _, err := referenceField(object, "ref"); err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	digest, err := stringField(object, "digest")
	if err != nil || !digestPattern.MatchString(digest) {
		return fmt.Errorf("employee record %s has malformed specification digest", path)
	}
	return nil
}

func validateWorkspaces(value any, path string) error {
	items, ok := value.([]any)
	if !ok || len(items) == 0 {
		return fmt.Errorf("employee record %s workspace_bindings must be a non-empty array", path)
	}
	seenBindings := map[string]bool{}
	seenWorkspaces := map[string]bool{}
	for _, item := range items {
		object, ok := item.(map[string]any)
		if !ok {
			return fmt.Errorf("employee record %s has malformed workspace binding", path)
		}
		if err := strictKeys(object, map[string]bool{"binding_ref": true, "workspace_ref": true}, "workspace binding"); err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
		binding, err := referenceField(object, "binding_ref")
		if err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
		workspace, err := referenceField(object, "workspace_ref")
		if err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
		if seenBindings[binding] || seenWorkspaces[workspace] {
			return fmt.Errorf("employee record %s contains duplicate workspace binding", path)
		}
		seenBindings[binding] = true
		seenWorkspaces[workspace] = true
	}
	return nil
}

func validateAssignmentWorkspaceRefs(value any, path string) error {
	items, ok := value.([]any)
	if !ok {
		return fmt.Errorf("employee record %s assignment_workspace_refs must be an array", path)
	}
	required := map[string]bool{
		"binding_ref": true, "capability": true, "grant_ref": true,
		"workspace_ref": true, "granted_at": true, "granted_by": true,
	}
	optional := map[string]bool{"root": true}
	for index, item := range items {
		object, ok := item.(map[string]any)
		if !ok {
			return fmt.Errorf("employee record %s assignment_workspace_refs[%d] must be an object", path, index)
		}
		field := fmt.Sprintf("assignment_workspace_refs[%d]", index)
		if err := strictKeysOptional(object, required, optional, field); err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
		for name := range required {
			if _, err := stringField(object, name); err != nil {
				return fmt.Errorf("employee record %s assignment_workspace_refs[%d].%s must be a non-empty string", path, index, name)
			}
		}
		if root, exists := object["root"]; exists {
			if _, ok := root.(string); !ok {
				return fmt.Errorf("employee record %s assignment_workspace_refs[%d].root must be a string", path, index)
			}
		}
	}
	return nil
}

func validateProfiles(value any, path string) error {
	// The canonical contract permits an empty delivery_profile_refs array for
	// a non-crew-routed (interactively operated) installation, so an empty
	// array is valid here; only a non-array is malformed.
	items, ok := value.([]any)
	if !ok {
		return fmt.Errorf("employee record %s delivery_profile_refs must be an array", path)
	}
	seen := map[string]bool{}
	for _, item := range items {
		object, ok := item.(map[string]any)
		if !ok {
			return fmt.Errorf("employee record %s has malformed delivery profile", path)
		}
		if err := strictKeys(object, map[string]bool{"profile_ref": true, "schema_version": true}, "delivery profile"); err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
		profile, err := referenceField(object, "profile_ref")
		if err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
		if seen[profile] {
			return fmt.Errorf("employee record %s contains duplicate delivery profile", path)
		}
		seen[profile] = true
		if _, err := stringField(object, "schema_version"); err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
	}
	return nil
}

func validateLineage(value any, path string) error {
	object, ok := value.(map[string]any)
	if !ok {
		return fmt.Errorf("employee record %s commissioning_lineage must be an object", path)
	}
	if err := strictKeys(object, map[string]bool{"lineage_ref": true, "source_ref": true, "commissioned_by": true, "commissioned_at": true}, "commissioning lineage"); err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	for _, field := range []string{"lineage_ref", "source_ref", "commissioned_by"} {
		if _, err := referenceField(object, field); err != nil {
			return fmt.Errorf("employee record %s: %w", path, err)
		}
	}
	commissionedAt, err := stringField(object, "commissioned_at")
	if err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	if _, err := time.Parse(time.RFC3339Nano, commissionedAt); err != nil {
		return fmt.Errorf("employee record %s has malformed commissioned_at", path)
	}
	return nil
}

func validateReferenceArray(value any, field, path string, allowEmpty bool) error {
	items, ok := value.([]any)
	if !ok || (!allowEmpty && len(items) == 0) {
		return fmt.Errorf("employee record %s %s must be a non-empty array", path, field)
	}
	seen := map[string]bool{}
	for _, item := range items {
		text, ok := item.(string)
		if !ok || !referencePattern.MatchString(text) || seen[text] {
			return fmt.Errorf("employee record %s has malformed or duplicate %s", path, field)
		}
		seen[text] = true
	}
	return nil
}

// validateBoardIdentity accepts exactly the legacy {board_ref} object or the
// additive participation binding fields principal_ref/agent_id/machine_id as
// a complete trio. Legacy records remain loadable; a record without the
// additive fields simply carries no participant binding and can never
// authorize one.
func validateBoardIdentity(value any, path string) error {
	object, ok := value.(map[string]any)
	if !ok {
		return fmt.Errorf("employee record %s board_identity must be an object", path)
	}
	allowed := map[string]bool{"board_ref": true, "principal_ref": true, "agent_id": true, "machine_id": true}
	for key := range object {
		if !allowed[key] {
			return fmt.Errorf("employee record %s: board_identity has unknown field: %s", path, key)
		}
	}
	if _, err := referenceField(object, "board_ref"); err != nil {
		return fmt.Errorf("employee record %s: %w", path, err)
	}
	participationFields := []string{"principal_ref", "agent_id", "machine_id"}
	present := 0
	for _, field := range participationFields {
		raw, exists := object[field]
		if !exists {
			continue
		}
		present++
		text, ok := raw.(string)
		if !ok || strings.TrimSpace(text) == "" {
			return fmt.Errorf("employee record %s: board_identity.%s must be a non-empty string", path, field)
		}
		if !referencePattern.MatchString(text) {
			return fmt.Errorf("employee record %s: board_identity.%s is malformed", path, field)
		}
	}
	if present != 0 && present != len(participationFields) {
		return fmt.Errorf("employee record %s: board_identity participation fields must include principal_ref, agent_id, and machine_id together", path)
	}
	return nil
}

func strictKeys(body map[string]any, required map[string]bool, field string) error {
	return strictKeysOptional(body, required, nil, field)
}

func strictKeysOptional(body map[string]any, required, optional map[string]bool, field string) error {
	for key := range body {
		if !required[key] && !optional[key] {
			return fmt.Errorf("%s has unknown field: %s", field, key)
		}
	}
	for key := range required {
		if _, ok := body[key]; !ok {
			return fmt.Errorf("%s is missing required field: %s", field, key)
		}
	}
	return nil
}

func stringField(body map[string]any, field string) (string, error) {
	value, ok := body[field].(string)
	if !ok || strings.TrimSpace(value) == "" {
		return "", fmt.Errorf("%s must be a non-empty string", field)
	}
	return value, nil
}

func referenceField(body map[string]any, field string) (string, error) {
	value, err := stringField(body, field)
	if err != nil {
		return "", err
	}
	if !referencePattern.MatchString(value) {
		return "", fmt.Errorf("%s is malformed", field)
	}
	return value, nil
}

func isTenantPlaceholder(value string) bool {
	switch strings.ToLower(value) {
	case "future", "pending", "placeholder", "unknown", "tbd", "todo":
		return true
	default:
		return false
	}
}

// Active reports whether the installation lifecycle authorizes direct Board
// participation. Draft, commissioning, paused, retired, and archived
// installations are inactive and must fail closed.
func (r Record) Active() bool {
	switch strings.ToLower(strings.TrimSpace(r.LifecycleState)) {
	case "pilot", "scheduled":
		return true
	default:
		return false
	}
}

func (r *Registry) Lookup(employeeID string) (Record, bool) {
	if r == nil {
		return Record{}, false
	}
	record, ok := r.records[strings.TrimSpace(employeeID)]
	return record, ok
}

func (r *Registry) List() []Record {
	if r == nil {
		return nil
	}
	result := make([]Record, 0, len(r.records))
	for _, record := range r.records {
		result = append(result, record)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].EmployeeID < result[j].EmployeeID })
	return result
}
