/**
 * Visual grammar (PRODUCT_REQUIREMENTS §6). A candidate, an approved order, a broker-accepted
 * order and a fill must never look alike. Every badge carries a distinct label, colour and
 * border style, so the distinction survives colour-blindness and greyscale printing.
 */

export type BadgeKind =
  | "candidate"
  | "rejected"
  | "approved"
  | "at_broker"
  | "partial"
  | "filled"
  | "closed"
  | "attention";

export interface BadgeSpec {
  kind: BadgeKind;
  label: string;
  title: string;
}

const ORDER_DISPLAY: Record<string, BadgeSpec> = {
  APPROVED_NOT_SENT: {
    kind: "approved",
    label: "APPROVED — not sent",
    title: "Passed checks; not yet at the (paper) venue",
  },
  AT_BROKER_NOT_FILLED: {
    kind: "at_broker",
    label: "AT BROKER — not filled",
    title: "Accepted by the PAPER venue; nothing has executed",
  },
  PARTIALLY_FILLED: {
    kind: "partial",
    label: "PARTIAL FILL",
    title: "Some quantity executed (simulated)",
  },
  FILLED: { kind: "filled", label: "FILLED", title: "Executed (simulated PAPER fill)" },
  CLOSED_WITHOUT_FILL: {
    kind: "closed",
    label: "CLOSED — no fill",
    title: "Cancelled, expired or rejected without execution",
  },
  NEEDS_ATTENTION: {
    kind: "attention",
    label: "UNKNOWN — check",
    title: "State uncertain; reconciliation required",
  },
};

export function orderBadge(display: string): BadgeSpec {
  return (
    ORDER_DISPLAY[display] ?? {
      kind: "attention",
      label: `UNRECOGNISED (${display})`,
      title: "Unrecognised order display state",
    }
  );
}

export function decisionBadge(decision: string): BadgeSpec {
  if (decision === "REJECTED_BY_RISK") {
    return { kind: "rejected", label: "IDEA — blocked by risk", title: "Candidate rejected by the risk gate" };
  }
  if (decision === "REQUIRES_REVIEW") {
    return { kind: "attention", label: "IDEA — needs review", title: "Candidate requires human review" };
  }
  return {
    kind: "candidate",
    label: `IDEA — ${decision.replace(/_CANDIDATE$/, "").replace(/_/g, " ")}`,
    title: "A recommendation only. Not an order, not a fill.",
  };
}

export type FreshnessTone = "ok" | "closed" | "stale";

export function freshnessTone(status: string): FreshnessTone {
  if (status === "FRESH") return "ok";
  if (status === "SESSION_CLOSED") return "closed";
  return "stale"; // STALE, MISSING, INVALID, ERROR and anything unexpected: fail closed
}

export type CheckTone = "ok" | "reduce" | "fail" | "skip";

/** Risk-check status -> tone. Anything unexpected is shown as a failure (fail closed). */
export function checkTone(status: string): CheckTone {
  if (status === "PASS") return "ok";
  if (status === "REDUCE") return "reduce";
  if (status === "SKIP") return "skip";
  return "fail";
}

export function riskOutcomeTone(outcome: string): CheckTone {
  if (outcome === "ACCEPT") return "ok";
  if (outcome === "REDUCE") return "reduce";
  return "fail"; // BLOCK, REVIEW and anything unexpected
}

/** Thesis status -> tone. Invalidated stands out; unknown statuses fail closed. */
export function thesisTone(status: string): "ok" | "reduce" | "fail" | "skip" {
  if (status === "OPEN") return "ok";
  if (status === "PENDING_ENTRY") return "skip";
  if (status === "CLOSED") return "skip";
  if (status === "INVALIDATED") return "reduce";
  return "fail";
}

/** Human summary of an invalidation rule recorded with a thesis. */
export function describeRule(rule: Record<string, unknown>): string {
  switch (rule.rule) {
    case "close_below_sma":
      return `close below SMA${String(rule.window)}`;
    case "stop":
      return `stop at ₹${String(rule.price)}`;
    case "max_holding_sessions":
      return `held ${String(rule.value)} sessions`;
    case "exit_before_event_days":
      return `known event within ${String(rule.value)} day(s)`;
    case "roe_below":
      return `ROE below ${pct(rule.value)}`;
    case "ocf_ttm_not_positive":
      return "operating cash flow (TTM) not positive";
    case "eps_growth_below":
      return `EPS growth below ${pct(rule.value)}`;
    default:
      return JSON.stringify(rule);
  }
}

function pct(v: unknown): string {
  const n = Number(v);
  return Number.isFinite(n) ? `${(n * 100).toFixed(0)}%` : String(v);
}

/** Where a weight sits relative to its allocation band. */
export function bandPosition(weight: number, min: number, max: number): "below" | "within" | "above" {
  if (weight < min) return "below";
  if (weight > max) return "above";
  return "within";
}

/**
 * Research verdicts (Phase 7). An assessment is advisory: it can only keep a rule-generated
 * candidate or hold it back. Anything that is not a valid CONFIRM is shown as held, so an
 * error or malformed reply never looks like an approval.
 */
export function verdictLabel(status: string, action: string | null): { tone: "ok" | "reduce" | "fail" | "skip"; label: string } {
  if (status !== "VALID") {
    const why: Record<string, string> = {
      INVALID: "invalid reply — held for review",
      TIMEOUT: "timed out — held for review",
      ERROR: "provider error — held for review",
      BUDGET_EXCEEDED: "budget exhausted — held for review",
      CYCLE_LIMIT: "cycle limit — held for review",
    };
    return { tone: "fail", label: why[status] ?? `unrecognised (${status}) — held for review` };
  }
  switch (action) {
    case "CONFIRM":
      return { tone: "ok", label: "confirms candidate" };
    case "HOLD":
      return { tone: "reduce", label: "downgraded to HOLD" };
    case "NO_ACTION":
      return { tone: "reduce", label: "downgraded to NO ACTION" };
    case "REQUIRES_REVIEW":
      return { tone: "skip", label: "asks for human review" };
    default:
      return { tone: "fail", label: `unrecognised verdict (${String(action)}) — held` };
  }
}

/** Approval states (Phase 8). Only APPROVED produced a PAPER order; FAILED means the risk
 * re-check at approval time did not accept it, so nothing was sent. */
export function approvalTone(status: string): "ok" | "reduce" | "fail" | "skip" {
  switch (status) {
    case "PENDING":
      return "reduce";
    case "APPROVED":
      return "ok";
    case "REJECTED":
    case "EXPIRED":
      return "skip";
    default:
      return "fail";
  }
}

export function severityTone(severity: string): "ok" | "reduce" | "fail" | "skip" {
  return severity === "critical" ? "fail" : severity === "warning" ? "reduce" : "skip";
}

/** Which screens' mutating controls a role may use (the API enforces the same rules). */
export function canAct(role: string | undefined, action: "own" | "approve"): boolean {
  if (role === "owner") return true;
  return action === "approve" && role === "approver";
}
