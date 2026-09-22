package services

import (
	"context"

	"agent-board/internal/operations"
)

// EmployeeBoardHistory returns the bounded, read-only capture of Board's own
// mutable records associated with one employee by native employee id.
//
// This is deliberately a separate read from the managerial projection. The
// projection is derived from immutable owner observations; these rows are
// latest-captured mutable Board records. Mixing them would let a Board record
// move a current placement, condition or accepted value, which it must never
// do. A failure here is surfaced as an error, never as an empty window.
func (b *Board) EmployeeBoardHistory(ctx context.Context, employeeID string, limit int) (operations.EmployeeBoardHistory, error) {
	if err := operations.ValidateEmployeeID(employeeID); err != nil {
		return operations.EmployeeBoardHistory{}, err
	}
	return b.store.ListEmployeeBoardHistory(ctx, employeeID, limit)
}
