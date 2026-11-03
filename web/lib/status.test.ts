import { describe, expect, it } from "vitest";

import {
  checkTone,
  decisionBadge,
  bandPosition,
  describeRule,
  freshnessTone,
  orderBadge,
  riskOutcomeTone,
  thesisTone,
  verdictLabel,
  approvalTone,
  severityTone,
  canAct,
} from "./status";

describe("visual grammar", () => {
  it("keeps candidate, approved, at-broker and fill visually distinct", () => {
    const specs = [
      decisionBadge("BUY_CANDIDATE"),
      orderBadge("APPROVED_NOT_SENT"),
      orderBadge("AT_BROKER_NOT_FILLED"),
      orderBadge("FILLED"),
    ];
    expect(new Set(specs.map((s) => s.kind)).size).toBe(4);
    expect(new Set(specs.map((s) => s.label)).size).toBe(4);
  });

  it("labels candidates as ideas, never as orders", () => {
    const b = decisionBadge("BUY_CANDIDATE");
    expect(b.label).toBe("IDEA — BUY");
    expect(b.title).toContain("Not an order");
    expect(decisionBadge("REJECTED_BY_RISK").kind).toBe("rejected");
  });

  it("treats unknown order states as needing attention", () => {
    expect(orderBadge("SOMETHING_NEW").kind).toBe("attention");
  });

  it("fails closed on freshness", () => {
    expect(freshnessTone("FRESH")).toBe("ok");
    expect(freshnessTone("SESSION_CLOSED")).toBe("closed");
    for (const s of ["STALE", "MISSING", "INVALID", "ERROR", "weird"]) {
      expect(freshnessTone(s)).toBe("stale");
    }
  });

  it("fails closed on risk outcomes and check statuses", () => {
    expect(riskOutcomeTone("ACCEPT")).toBe("ok");
    expect(riskOutcomeTone("REDUCE")).toBe("reduce");
    expect(riskOutcomeTone("BLOCK")).toBe("fail");
    expect(riskOutcomeTone("REVIEW")).toBe("fail");
    expect(riskOutcomeTone("???")).toBe("fail");
    expect(checkTone("SKIP")).toBe("skip");
    expect(checkTone("weird")).toBe("fail");
  });

  it("shows thesis states and rules plainly, failing closed on unknowns", () => {
    expect(thesisTone("OPEN")).toBe("ok");
    expect(thesisTone("INVALIDATED")).toBe("reduce");
    expect(thesisTone("WEIRD")).toBe("fail");
    expect(describeRule({ rule: "close_below_sma", window: 50 })).toBe("close below SMA50");
    expect(describeRule({ rule: "stop", price: "120.30" })).toBe("stop at ₹120.30");
  });

  it("describes long-term rules and band positions", () => {
    expect(describeRule({ rule: "roe_below", value: "0.10" })).toBe("ROE below 10%");
    expect(describeRule({ rule: "ocf_ttm_not_positive" })).toBe("operating cash flow (TTM) not positive");
    expect(bandPosition(0.02, 0.05, 0.15)).toBe("below");
    expect(bandPosition(0.1, 0.05, 0.15)).toBe("within");
    expect(bandPosition(0.2, 0.05, 0.15)).toBe("above");
  });

  it("never shows a research failure or downgrade as an approval", () => {
    expect(verdictLabel("VALID", "CONFIRM")).toEqual({ tone: "ok", label: "confirms candidate" });
    expect(verdictLabel("VALID", "HOLD").tone).toBe("reduce");
    expect(verdictLabel("INVALID", null).label).toContain("held for review");
    expect(verdictLabel("WEIRD", "CONFIRM").tone).toBe("fail");
    expect(verdictLabel("VALID", "STRONG_BUY").tone).toBe("fail");
  });

  it("shows approval and notification states without implying an order", () => {
    expect(approvalTone("APPROVED")).toBe("ok");
    expect(approvalTone("PENDING")).toBe("reduce");
    expect(approvalTone("FAILED")).toBe("fail");
    expect(approvalTone("WEIRD")).toBe("fail");
    expect(severityTone("critical")).toBe("fail");
    expect(canAct("viewer", "approve")).toBe(false);
    expect(canAct("approver", "approve")).toBe(true);
    expect(canAct("approver", "own")).toBe(false);
    expect(canAct(undefined, "own")).toBe(false);
  });
});
