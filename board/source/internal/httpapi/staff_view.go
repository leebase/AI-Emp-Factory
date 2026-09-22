package httpapi

import (
	"net/http"

	"agent-board/internal/staff"
)

// staffView is the authenticated read-only staff view.
//
// It is opt-in per deployment: with the flag off the route answers 404 and
// nothing is read, so enabling it on one private-LAN preview host cannot turn
// it on anywhere else. It is deliberately NOT in the public-read allow list
// that the older /api/employee-roster route sits in, so it needs a session
// identity even where anonymous reads are permitted.
//
// The response is a narrow assembled summary. Mission documents, configuration
// bodies and state files are never forwarded: staff.Build copies named fields
// only, and the client sends no path of its own.
func (s *Server) staffView(w http.ResponseWriter, r *http.Request) {
	if !s.cfg.StaffViewEnabled {
		writeError(w, http.StatusNotFound, errStaffViewDisabled)
		return
	}
	operatorID, err := s.cfg.EffectiveStaffOperatorID()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err)
		return
	}
	observedAt, fleet, sourceNotes, err := s.board.EmployeeFleet(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err)
		return
	}
	// A relationship file that cannot be read is a disclosed absence of
	// relationships, not a failed staff read and not an empty organisation.
	book, bookErr := staff.LoadBook(s.cfg.StaffRelationshipsFile)
	// The native Board source is optional and separate from the board's own
	// store. An absent or unreadable source is disclosed on the view; it is
	// never silently substituted with the deployment's own database.
	boardRead := staff.ReadBoardForOperator(r.Context(), s.cfg.StaffBoardDBPath, fleet, operatorID)
	view := staff.Build(observedAt, fleet, sourceNotes, book, boardRead)
	if bookErr != nil {
		view.RelationshipNotes = append(view.RelationshipNotes, bookErr.Error())
	}
	writeJSON(w, http.StatusOK, view)
}

type staffViewDisabledError struct{}

func (staffViewDisabledError) Error() string {
	return "the staff view is not enabled on this deployment"
}

var errStaffViewDisabled = staffViewDisabledError{}
