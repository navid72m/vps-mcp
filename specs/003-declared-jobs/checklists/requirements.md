# Specification Quality Checklist: Declared Jobs

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-11-11
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- **Defaults instead of questions**:
  - a plan is valid for 30 min;
  - the maximum runtime is 24 h;
  - the default success marker is "ALL DONE";
  - jobs run as root by default;
  - there is one job at a time, and jobs and rehearsals are mutually exclusive;
  - status shows the last 50 progress lines;
  - the report excerpt is limited to 200 lines;
  - cancel does no automatic repair.

  These are in the Requirements and Assumptions sections, and can be changed via `/speckit-clarify`.
- **Domain terms**: Odoo, databases, services and environment values are named because they are the subject matter; no implementation technology is prescribed.
- **Public repository**: the spec uses generic names (`full_rehearsal.sh`, `shop_19_test`). The operator's real script, database and service names belong only in their local policy file.
