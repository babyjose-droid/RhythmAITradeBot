import { describe, expect, it } from "vitest";

import {
  checkTone,
  decisionBadge,
  describeRule,
  freshnessTone,
  orderBadge,
  riskOutcomeTone,
  thesisTone,
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
});
