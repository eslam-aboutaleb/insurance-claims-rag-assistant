import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import ClaimConfirmCard from "@/components/ClaimConfirmCard";
import {
  ApiError,
  ClaimCancelUnavailableError,
  cancelClaimSubmission,
  confirmClaim,
  lookupConfirmedClaim,
} from "@/lib/api";

vi.mock("@/context/AuthContext", () => ({
  useAuth: () => ({
    token: "fake-token",
    userId: "user-123",
    status: "authenticated",
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    confirmClaim: vi.fn(),
    cancelClaimSubmission: vi.fn(),
    lookupConfirmedClaim: vi.fn(),
  };
});

const TOKEN = "8f3c2a54-1c1e-4f6a-9d3b-7b2c1a4e5f60";

const CLAIM_DATA = {
  policy_number: "POL-1092",
  claim_type: "Water Damage",
  amount: 3500,
  description: "A pipe burst flooded the kitchen yesterday",
};

const CONFIRMED_RECEIPT = {
  kind: "claim_confirmation_receipt" as const,
  claimId: "CLM-AB12CD34",
  status: "Submitted",
  message: "Your claim CLM-AB12CD34 has been successfully submitted.",
};

/** A token that is still alive. */
function futureExpiry(minutes = 15): string {
  return new Date(Date.now() + minutes * 60_000).toISOString();
}

/** A token the backend would already refuse. */
function pastExpiry(): string {
  return new Date(Date.now() - 60_000).toISOString();
}

function renderCard(
  overrides: Partial<React.ComponentProps<typeof ClaimConfirmCard>> = {},
) {
  const onConfirmed = vi.fn();
  const onCancelled = vi.fn();
  const view = render(
    <ClaimConfirmCard
      claimData={CLAIM_DATA}
      token={TOKEN}
      expiresAt={futureExpiry()}
      onConfirmed={onConfirmed}
      onCancelled={onCancelled}
      {...overrides}
    />,
  );
  return { onConfirmed, onCancelled, unmount: view.unmount };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ClaimConfirmCard", () => {
  it("renders the prepared fields for review", () => {
    renderCard();

    const card = screen.getByTestId("claim-confirm-card");
    expect(card).toHaveTextContent("Confirm your claim");
    expect(card).toHaveTextContent("POL-1092");
    expect(card).toHaveTextContent("Water Damage");
    expect(card).toHaveTextContent(
      "A pipe burst flooded the kitchen yesterday",
    );
  });

  it("formats money as whole dollars, and keeps a fractional amount instead of rounding it", () => {
    const { unmount } = renderCard();
    expect(screen.getByText("$3,500")).toBeInTheDocument();
    unmount();

    renderCard({ claimData: { ...CLAIM_DATA, amount: 3500.5 } });
    expect(screen.getByText("$3,500.5")).toBeInTheDocument();
  });

  it("shows how long the confirmation token stays valid", () => {
    renderCard({ expiresAt: futureExpiry(10) });

    expect(screen.getByTestId("claim-confirm-countdown")).toHaveTextContent(
      /\d+m \d\ds left/,
    );
    expect(screen.getByTestId("confirm-claim-button")).toBeEnabled();
  });

  it("disables Confirm once the token has expired and offers to re-ask", () => {
    renderCard({ expiresAt: pastExpiry() });

    expect(screen.getByTestId("confirm-claim-button")).toBeDisabled();
    expect(screen.getByTestId("claim-confirm-countdown")).toHaveTextContent(
      "Expired",
    );
    expect(screen.getByTestId("claim-confirm-expired-note")).toHaveTextContent(
      /ask the assistant to prepare your claim again/i,
    );
  });

  it("warns about a water damage amount above the policy cap without blocking", () => {
    renderCard({ claimData: { ...CLAIM_DATA, amount: 30000 } });

    expect(screen.getByTestId("claim-confirm-note")).toHaveTextContent(
      "$25,000",
    );
    // Non-blocking: the claim may still be worth filing.
    expect(screen.getByTestId("confirm-claim-button")).toBeEnabled();
  });

  it("warns about a description the claim form would reject", () => {
    renderCard({ claimData: { ...CLAIM_DATA, description: "Pipes" } });

    expect(screen.getByTestId("claim-confirm-note")).toHaveTextContent(
      /shorter than the claim form requires/i,
    );
    expect(screen.getByTestId("confirm-claim-button")).toBeEnabled();
  });

  it("keeps Confirm disabled while a confirmation is in flight", async () => {
    let release: (response: unknown) => void = () => {};
    vi.mocked(confirmClaim).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );

    const { onConfirmed } = renderCard();

    fireEvent.click(screen.getByTestId("confirm-claim-button"));
    expect(screen.getByTestId("confirm-claim-button")).toBeDisabled();

    release({
      success: true,
      claim_id: CONFIRMED_RECEIPT.claimId,
      status: CONFIRMED_RECEIPT.status,
      message: CONFIRMED_RECEIPT.message,
    });

    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    // The card still resolves on its own when no chat hook owns it.
    expect(screen.getByTestId("claim-receipt")).toHaveTextContent(
      CONFIRMED_RECEIPT.claimId,
    );
  });

  it("reports the claim identifier and status once confirmed", async () => {
    vi.mocked(confirmClaim).mockResolvedValue({
      success: true,
      claim_id: CONFIRMED_RECEIPT.claimId,
      status: "Submitted",
      message: CONFIRMED_RECEIPT.message,
    });

    const { onConfirmed } = renderCard();
    fireEvent.click(screen.getByTestId("confirm-claim-button"));

    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(confirmClaim).toHaveBeenCalledWith("fake-token", TOKEN);
    expect(onConfirmed).toHaveBeenCalledWith(CONFIRMED_RECEIPT);

    const receipt = screen.getByTestId("claim-receipt");
    expect(receipt).toHaveTextContent(CONFIRMED_RECEIPT.claimId);
    expect(receipt).toHaveTextContent("Submitted");
    expect(receipt).toHaveTextContent(CONFIRMED_RECEIPT.message);
  });

  it("surfaces a refused submission without spending the token", async () => {
    vi.mocked(confirmClaim).mockResolvedValue({
      success: false,
      message: "Policy POL-1092 already has an identical pending claim.",
    });

    const { onConfirmed } = renderCard();
    fireEvent.click(screen.getByTestId("confirm-claim-button"));

    await waitFor(() =>
      expect(screen.getByTestId("claim-confirm-error")).toHaveTextContent(
        /identical pending claim/i,
      ),
    );
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(screen.getByTestId("confirm-claim-button")).toBeEnabled();
  });

  it("keeps the card interactive and retries with the same token after a network failure", async () => {
    vi.mocked(confirmClaim).mockRejectedValueOnce(
      new ApiError(
        "We could not reach the server, so your claim has not been filed yet.",
        0,
        "NETWORK_ERROR",
      ),
    );

    const { onConfirmed } = renderCard();
    fireEvent.click(screen.getByTestId("confirm-claim-button"));

    await waitFor(() =>
      expect(screen.getByTestId("claim-confirm-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("confirm-claim-button")).toBeEnabled();
    expect(onConfirmed).not.toHaveBeenCalled();

    vi.mocked(confirmClaim).mockResolvedValue({
      success: true,
      claim_id: CONFIRMED_RECEIPT.claimId,
      status: "Submitted",
      message: CONFIRMED_RECEIPT.message,
    });
    fireEvent.click(screen.getByTestId("confirm-claim-button"));

    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    // The token survived the failure, so the retry is the retry the user asked for.
    expect(vi.mocked(confirmClaim).mock.calls).toHaveLength(2);
    expect(vi.mocked(confirmClaim).mock.calls[1][1]).toBe(TOKEN);
  });

  it("surfaces a spent token and offers to re-ask, naming the multi-device case", async () => {
    vi.mocked(confirmClaim).mockRejectedValue(
      new ApiError(
        "Invalid, expired, or already used confirmation token.",
        400,
      ),
    );

    const { onConfirmed } = renderCard();
    fireEvent.click(screen.getByTestId("confirm-claim-button"));

    const error = await screen.findByTestId("claim-confirm-error");
    expect(error).toHaveTextContent(
      "Invalid, expired, or already used confirmation token.",
    );
    expect(error).toHaveTextContent(
      /already confirmed this claim in another window/i,
    );
    expect(error).toHaveTextContent(/prepare the claim again/i);
    expect(onConfirmed).not.toHaveBeenCalled();
  });

  it("separates an expired token from a spent one", async () => {
    vi.mocked(confirmClaim).mockRejectedValue(
      new ApiError("Confirmation token has expired.", 400),
    );

    renderCard();
    fireEvent.click(screen.getByTestId("confirm-claim-button"));

    const error = await screen.findByTestId("claim-confirm-error");
    expect(error).toHaveTextContent("Confirmation token has expired.");
    expect(error).toHaveTextContent(/no longer valid/i);
    expect(error).not.toHaveTextContent(/another window/i);
  });

  it("treats a 404 cancel as an unavailable endpoint and reports the expiry", async () => {
    vi.mocked(cancelClaimSubmission).mockRejectedValue(
      new ClaimCancelUnavailableError(
        "Cancelling is not available on this deployment, so the prepared claim was left as it is. " +
          "It expires on its own within 15 minutes, and no claim is filed unless you confirm.",
      ),
    );

    const { onCancelled } = renderCard();
    fireEvent.click(screen.getByTestId("cancel-claim-button"));

    await waitFor(() => expect(onCancelled).toHaveBeenCalledTimes(1));
    expect(cancelClaimSubmission).toHaveBeenCalledWith("fake-token", TOKEN);
    expect(onCancelled).toHaveBeenCalledWith({
      kind: "claim_cancellation_note",
      cancelled: false,
      message: expect.stringContaining("15 minutes"),
    });
    expect(screen.getByTestId("claim-cancellation-note")).toHaveTextContent(
      /15 minutes/,
    );
  });

  it("reports a cancellation the backend accepted", async () => {
    vi.mocked(cancelClaimSubmission).mockResolvedValue({
      cancelled: true,
      message: "The prepared claim was cancelled. No claim was filed.",
    });

    const { onCancelled } = renderCard();
    fireEvent.click(screen.getByTestId("cancel-claim-button"));

    await waitFor(() => expect(onCancelled).toHaveBeenCalledTimes(1));
    expect(onCancelled).toHaveBeenCalledWith({
      kind: "claim_cancellation_note",
      cancelled: true,
      message: "The prepared claim was cancelled. No claim was filed.",
    });
    expect(screen.getByTestId("claim-cancellation-note")).toHaveTextContent(
      "Preparation cancelled",
    );
  });

  it("keeps the card open when a cancellation cannot be carried out", async () => {
    vi.mocked(cancelClaimSubmission).mockRejectedValue(
      new ApiError("Confirmation token has expired.", 400),
    );

    const { onCancelled } = renderCard();
    fireEvent.click(screen.getByTestId("cancel-claim-button"));

    await waitFor(() =>
      expect(screen.getByTestId("claim-confirm-error")).toBeInTheDocument(),
    );
    expect(onCancelled).not.toHaveBeenCalled();
    expect(screen.getByTestId("cancel-claim-button")).toBeEnabled();
  });
});

describe("ClaimConfirmCard receipt", () => {
  beforeEach(() => {
    vi.mocked(confirmClaim).mockResolvedValue({
      success: true,
      claim_id: CONFIRMED_RECEIPT.claimId,
      status: "Submitted",
      message: CONFIRMED_RECEIPT.message,
    });
  });

  it("reads the filed claim back when the receipt link is opened", async () => {
    vi.mocked(lookupConfirmedClaim).mockResolvedValue({
      claim_id: CONFIRMED_RECEIPT.claimId,
      policy_number: "POL-1092",
      claim_type: "Water Damage",
      status: "Submitted",
      amount: "3500.00",
    });

    renderCard();
    fireEvent.click(screen.getByTestId("confirm-claim-button"));
    fireEvent.click(await screen.findByTestId("claim-receipt-link"));

    await waitFor(() =>
      expect(screen.getByTestId("claim-receipt")).toHaveTextContent("POL-1092"),
    );
    expect(lookupConfirmedClaim).toHaveBeenCalledWith(
      "fake-token",
      CONFIRMED_RECEIPT.claimId,
    );
  });

  it("degrades to the receipt it already has when the claim cannot be read back", async () => {
    vi.mocked(lookupConfirmedClaim).mockRejectedValue(
      new ApiError("Claim not found", 404),
    );

    renderCard();
    fireEvent.click(screen.getByTestId("confirm-claim-button"));
    fireEvent.click(await screen.findByTestId("claim-receipt-link"));

    await waitFor(() =>
      expect(screen.getByTestId("claim-receipt")).toHaveTextContent(
        /could not be loaded right now/i,
      ),
    );
    // The claim exists whether or not its detail can be read again.
    expect(screen.getByTestId("claim-receipt")).toHaveTextContent(
      CONFIRMED_RECEIPT.claimId,
    );
  });
});
