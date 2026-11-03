"""Claude research service (Phase 7): advisory assessments of rule-generated candidates.

Nothing in this package can create, size or send an order. It has no import path to the
broker, order, execution, paper or risk modules (enforced by import-linter). Its output is a
schema-validated assessment that can only *confirm* a candidate or *downgrade* it to
HOLD / NO_ACTION / REQUIRES_REVIEW.
"""
