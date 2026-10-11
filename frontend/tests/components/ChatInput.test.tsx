import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, screen, act } from "@testing-library/react";
import ChatInput from "@/components/ChatInput";

describe("ChatInput", () => {
  it("calls onSend when Enter is pressed without Shift", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} isLoading={false} />);

    const textarea = screen.getByTestId("chat-textarea");
    fireEvent.change(textarea, { target: { value: "Hello" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    expect(onSend).toHaveBeenCalledWith("Hello");
  });

  it("does not send when Shift+Enter is pressed", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} isLoading={false} />);

    const textarea = screen.getByTestId("chat-textarea");
    fireEvent.change(textarea, { target: { value: "Hello" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not send empty message", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} isLoading={false} />);

    const textarea = screen.getByTestId("chat-textarea");
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    expect(onSend).not.toHaveBeenCalled();
  });

  it("clears value after sending", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} isLoading={false} />);

    const textarea = screen.getByTestId("chat-textarea") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Hello" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    expect(textarea.value).toBe("");
  });

  it("stays editable and reports a busy turn while an answer streams", () => {
    const onSend = vi.fn();
    const { rerender } = render(
      <ChatInput onSend={onSend} isLoading={false} />,
    );

    const textarea = screen.getByTestId("chat-textarea") as HTMLTextAreaElement;
    expect(textarea).toBeEnabled();
    expect(textarea).toHaveAttribute("aria-busy", "false");

    rerender(<ChatInput onSend={onSend} isLoading={true} />);

    // Locking the composer here would stop the reader from typing the
    // follow-up they already know they want.
    expect(textarea).toBeEnabled();
    expect(textarea).toHaveAttribute("aria-busy", "true");
    fireEvent.change(textarea, { target: { value: "next question" } });
    expect(textarea.value).toBe("next question");

    // One turn at a time: the send button stays down so nothing queues behind
    // the answer already streaming.
    expect(screen.getByTestId("send-button")).toBeDisabled();
  });

  it("leaves the send button usable again once the turn is over", () => {
    const onSend = vi.fn();
    const { rerender } = render(<ChatInput onSend={onSend} isLoading={true} />);

    fireEvent.change(screen.getByTestId("chat-textarea"), {
      target: { value: "Hello" },
    });
    expect(screen.getByTestId("send-button")).toBeDisabled();

    rerender(<ChatInput onSend={onSend} isLoading={false} />);

    expect(screen.getByTestId("send-button")).toBeEnabled();
  });

  it("hands the keyboard back to the composer that had it when the turn began", () => {
    const onSend = vi.fn();
    const { rerender } = render(
      <ChatInput onSend={onSend} isLoading={false} />,
    );

    const textarea = screen.getByTestId("chat-textarea");
    textarea.focus();
    expect(textarea).toHaveFocus();

    rerender(<ChatInput onSend={onSend} isLoading={true} />);

    // The answer is on its way, so the reader looks elsewhere for a moment.
    act(() => {
      textarea.blur();
    });
    expect(textarea).not.toHaveFocus();

    rerender(<ChatInput onSend={onSend} isLoading={false} />);

    expect(textarea).toHaveFocus();
  });

  it("does not take focus when a turn the composer never had comes to an end", () => {
    const onSend = vi.fn();
    const { rerender } = render(<ChatInput onSend={onSend} isLoading={true} />);

    expect(screen.getByTestId("chat-textarea")).not.toHaveFocus();

    rerender(<ChatInput onSend={onSend} isLoading={false} />);

    expect(screen.getByTestId("chat-textarea")).not.toHaveFocus();
  });
});
