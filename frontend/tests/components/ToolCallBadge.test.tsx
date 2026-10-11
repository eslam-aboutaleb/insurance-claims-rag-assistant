import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import ToolCallBadge from "@/components/ToolCallBadge";

const CLAIM_STATUS_RESULT = {
  found: true,
  claim_id: "CLM-8821",
  policy_number: "POL-1092",
  claim_type: "Water Damage",
  status: "Submitted",
  amount: 1500,
  citation: "Claim CLM-8821 in the OmniCare claims system (policy POL-1092).",
};

function renderStatusCard(result: unknown, tool = "get_claim_status") {
  render(
    <ToolCallBadge
      toolCalls={[{ name: tool, arguments: { claim_id: "CLM-8821" }, result }]}
    />,
  );
}

describe("ToolCallBadge claim status card", () => {
  it("shows the fields the backend actually returns", () => {
    renderStatusCard(CLAIM_STATUS_RESULT);

    expect(screen.getByText("CLM-8821")).toBeInTheDocument();
    expect(screen.getByText("Water Damage")).toBeInTheDocument();
    expect(screen.getByText("$1,500")).toBeInTheDocument();
    expect(screen.getByText("POL-1092")).toBeInTheDocument();
    expect(screen.getByText("Submitted")).toBeInTheDocument();
  });

  it("never leaks the removed filing_date field", () => {
    // The backend has no filing_date on a claim, so rendering one produced
    // "Invalid Date". Nothing may bring it back.
    renderStatusCard(CLAIM_STATUS_RESULT);

    expect(screen.queryByText("Filed On")).not.toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/i)).not.toBeInTheDocument();
  });

  it("falls back to a neutral colour for a status that is not in the map", () => {
    renderStatusCard({ ...CLAIM_STATUS_RESULT, status: "Escalated" });

    expect(screen.getByText("Escalated")).toBeInTheDocument();
    expect(screen.getByTestId("claim-status-pill")).toBeInTheDocument();
    expect(
      document.querySelector('[data-testid="claim-status-pill"] > div')
        ?.className,
    ).toContain("bg-insurance-ink-tertiary");
  });

  it("renders a decimal-string amount without losing precision formatting", () => {
    renderStatusCard({ ...CLAIM_STATUS_RESULT, amount: "25000.5" });

    expect(screen.getByText("$25,000.5")).toBeInTheDocument();
  });

  it("shows the lookup failure instead of a claim card", () => {
    renderStatusCard({
      found: false,
      error: "No claim found with ID 'CLM-0000'.",
    });

    expect(screen.getByText("Claim Not Found")).toBeInTheDocument();
    expect(
      screen.getByText("No claim found with ID 'CLM-0000'."),
    ).toBeInTheDocument();
    expect(screen.queryByText("CLM-8821")).not.toBeInTheDocument();
  });

  it("renders a submitted claim as a plain details element", () => {
    // The claim submission card was unreachable: submit_claim now streams its
    // result through the generic tool trace instead.
    renderStatusCard(
      {
        success: true,
        confirmation_id: "CLM-4242",
        status: "Submitted",
        policy_number: "POL-1092",
        claim_type: "Water Damage",
        amount: 1500,
        message: "Your claim CLM-4242 has been submitted.",
      },
      "submit_claim",
    );

    expect(screen.getByText("submit_claim")).toBeInTheDocument();
    expect(
      screen.queryByText("Claim Submitted Successfully"),
    ).not.toBeInTheDocument();
  });
});
