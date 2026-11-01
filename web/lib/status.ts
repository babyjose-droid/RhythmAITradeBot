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
