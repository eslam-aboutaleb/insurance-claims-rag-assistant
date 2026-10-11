import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import MessageBubble from "@/components/MessageBubble";
import { Message } from "@/types/chat";
import { ApiError, deleteFeedback, submitFeedback } from "@/lib/api";

vi.mock("react-hot-toast", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    submitFeedback: vi.fn(),
    deleteFeedback: vi.fn(),
  };
});

const SESSION_TOKEN = "session-token-abc";

function assistantMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    role: "assistant",
    content: "Water damage is covered for sudden events.",
    timestamp: new Date("2024-01-02T10:30:00Z"),
    ...overrides,
  };
}

function upButton(): HTMLElement {
  return screen.getByTestId("feedback-up-m1");
}

function downButton(): HTMLElement {
  return screen.getByTestId("feedback-down-m1");
}

/** A submit call that stays pending until the test settles it. */
function pendingSubmit(): {
  resolve: (value: unknown) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: unknown) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<unknown>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  vi.mocked(submitFeedback).mockReturnValue(promise as Promise<never>);
  return { resolve, reject };
}

describe("MessageBubble answer feedback", () => {
  beforeEach(() => {
    localStorage.setItem("omnicare_token", SESSION_TOKEN);
    vi.mocked(submitFeedback).mockResolvedValue({
      id: "f1",
      message_id: "m1",
      rating: "up",
      created_at: "2026-05-01T00:00:00Z",
    });
    vi.mocked(deleteFeedback).mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("offers a thumbs pair on an answer and keeps it keyboard-visible", () => {
    render(<MessageBubble message={assistantMessage()} />);

    const up = upButton();
    const down = downButton();
    expect(up).toHaveAttribute("type", "button");
    expect(down).toHaveAttribute("type", "button");
    // The pair sits in the same hover row as Copy, so it uses the same
    // treatment: always reachable on touch, revealed by hover on a desktop
    // pointer and by :focus-visible for the keyboard.
    expect(up.className).toContain("sm:group-hover:sm:opacity-100");
    expect(up.className).toContain("sm:focus-visible:sm:opacity-100");
    expect(up.className).toContain("focus-visible:opacity-100");
    expect(up).toHaveAttribute("aria-pressed", "false");
    expect(up.parentElement).toBe(
      screen.getByRole("button", { name: "Copy message" }).parentElement,
    );
  });

  it("does not offer feedback on the user's own turns", () => {
    render(
      <MessageBubble
        message={assistantMessage({
          id: "u1",
          role: "user",
          content: "Is flood covered?",
        })}
      />,
    );

    expect(screen.queryByTestId("feedback-up-u1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("feedback-down-u1")).not.toBeInTheDocument();
  });

  it("does not offer feedback on a message that failed entirely", () => {
    render(
      <MessageBubble
        message={assistantMessage({
          id: "e1",
          role: "error",
          content: "Something went wrong.",
        })}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("feedback-up-e1")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("activates a rating optimistically, before the server answers", async () => {
    const pending = pendingSubmit();
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(upButton());
    });

    expect(submitFeedback).toHaveBeenCalledWith(SESSION_TOKEN, "m1", "up");
    expect(upButton()).toHaveAttribute("aria-pressed", "true");
    expect(downButton()).toHaveAttribute("aria-pressed", "false");

    await act(async () => {
      pending.resolve({
        id: "f1",
        message_id: "m1",
        rating: "up",
        created_at: "2026-05-01T00:00:00Z",
      });
    });

    expect(upButton()).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("feedback-thanks-m1")).toHaveTextContent(
      "Thanks for the feedback",
    );
  });

  it("never double-submits while a rating is in flight", async () => {
    pendingSubmit();
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(upButton());
      fireEvent.click(upButton());
      fireEvent.click(downButton());
    });

    expect(submitFeedback).toHaveBeenCalledTimes(1);
    expect(upButton()).toHaveAttribute("aria-pressed", "true");
  });

  it("reverts the optimistic rating and toasts when the request fails", async () => {
    const { toast } = await import("react-hot-toast");
    const pending = pendingSubmit();
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(upButton());
    });
    expect(upButton()).toHaveAttribute("aria-pressed", "true");

    await act(async () => {
      pending.reject(
        new ApiError("This message is not available to you.", 403, "FORBIDDEN"),
      );
    });

    expect(upButton()).toHaveAttribute("aria-pressed", "false");
    expect(downButton()).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByTestId("feedback-thanks-m1")).not.toBeInTheDocument();
    // Non-blocking: the answer and its controls are untouched by the failure.
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("assistant-text-m1")).toHaveTextContent(
      "Water damage is covered for sudden events.",
    );
  });

  it("lets the user switch to the other rating", async () => {
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(upButton());
    });
    await act(async () => {
      fireEvent.click(downButton());
    });

    expect(submitFeedback).toHaveBeenNthCalledWith(
      1,
      SESSION_TOKEN,
      "m1",
      "up",
    );
    expect(submitFeedback).toHaveBeenNthCalledWith(
      2,
      SESSION_TOKEN,
      "m1",
      "down",
    );
    expect(downButton()).toHaveAttribute("aria-pressed", "true");
    // A thumbs-down opens the optional reason form.
    expect(screen.getByTestId("feedback-detail-m1")).toBeInTheDocument();
  });

  it("offers a two-field optional form after a thumbs-down and sends it", async () => {
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(downButton());
    });

    const category = screen.getByTestId(
      "feedback-category-m1",
    ) as HTMLSelectElement;
    const comment = screen.getByTestId(
      "feedback-comment-m1",
    ) as HTMLTextAreaElement;
    const submit = screen.getByTestId("feedback-submit-m1");
    // Two fields and a submit, and nothing else.
    expect(category).toBeInTheDocument();
    expect(comment).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /helpful|answer|details/i }),
    ).not.toHaveLength(0);
    expect(submit).toHaveAttribute("type", "button");
    expect(comment).toHaveAttribute("maxlength", "1000");

    await act(async () => {
      fireEvent.change(category, { target: { value: "outdated_policy" } });
      fireEvent.change(comment, {
        target: { value: "The section was superseded." },
      });
    });
    await act(async () => {
      fireEvent.click(submit);
    });

    expect(submitFeedback).toHaveBeenLastCalledWith(
      SESSION_TOKEN,
      "m1",
      "down",
      "The section was superseded.",
      "outdated_policy",
    );
    expect(screen.queryByTestId("feedback-detail-m1")).not.toBeInTheDocument();
    expect(screen.getByTestId("feedback-thanks-m1")).toBeInTheDocument();
  });

  it("keeps the rating when the optional detail fails to send", async () => {
    vi.mocked(submitFeedback).mockResolvedValueOnce({
      id: "f1",
      message_id: "m1",
      rating: "down",
      created_at: "2026-05-01T00:00:00Z",
    });
    vi.mocked(submitFeedback).mockRejectedValueOnce(
      new ApiError("We could not reach the server.", 0, "NETWORK_ERROR"),
    );
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(downButton());
    });
    await act(async () => {
      fireEvent.change(screen.getByTestId("feedback-comment-m1"), {
        target: { value: "Wrong deductible." },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("feedback-submit-m1"));
    });

    expect(downButton()).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("feedback-detail-m1")).toBeInTheDocument();
  });

  it("dismisses the optional form without a second request when it is left empty", async () => {
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(downButton());
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("feedback-submit-m1"));
    });

    expect(submitFeedback).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("feedback-detail-m1")).not.toBeInTheDocument();
    expect(screen.getByTestId("feedback-thanks-m1")).toBeInTheDocument();
  });

  it("withdraws the rating when the active thumb is clicked again", async () => {
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(upButton());
    });
    expect(screen.getByTestId("feedback-thanks-m1")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(upButton());
    });

    expect(deleteFeedback).toHaveBeenCalledWith(SESSION_TOKEN, "m1");
    expect(upButton()).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByTestId("feedback-thanks-m1")).not.toBeInTheDocument();
  });

  it("puts the rating back when the withdrawal fails", async () => {
    const { toast } = await import("react-hot-toast");
    vi.mocked(deleteFeedback).mockRejectedValue(
      new ApiError("We could not reach the server.", 0, "NETWORK_ERROR"),
    );
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(upButton());
    });
    await act(async () => {
      fireEvent.click(upButton());
    });

    expect(upButton()).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("feedback-thanks-m1")).toBeInTheDocument();
    expect(toast.error).toHaveBeenCalledTimes(1);
  });
});
