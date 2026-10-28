/**
 * ChatInput component.
 *
 * A self-expanding textarea with an integrated send button. Users type their
 * message here and submit via Enter (without Shift) or by clicking the send
 * button. The textarea grows up to a maximum height before scrolling, keeping
 * the input compact for short messages and spacious for longer ones.
 *
 * Props:
 *   onSend: Callback invoked with the message text when the user submits.
 *   isLoading: While a turn is in flight the textarea stays editable and
 *     reports `aria-busy`, and the send button is held down so a turn cannot be
 *     queued on top of the one already streaming.
 */

import { useRef, useEffect, KeyboardEvent, useState } from "react";
import { Send } from "lucide-react";

interface ChatInputProps {
  autoFocus?: boolean;
  /** Callback fired when the user submits a message. */
  onSend: (message: string) => void;
  /** Whether the backend is currently processing the message. */
  isLoading: boolean;
}

export default function ChatInput({
  onSend,
  isLoading,
  autoFocus,
}: ChatInputProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  /**
   * Whether the composer held focus when the current turn began.
   *
   * Focus is restored from what was there rather than granted anew, so a
   * composer that mounts mid-stream -- or was never focused at all -- never has
   * the caret pulled out from under wherever the reader put it.
   */
  const heldFocusRef = useRef(false);

  // Auto-resize the textarea to fit content, up to a max height.
  useEffect(() => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = "auto";
      textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
    }
  }, [value]);

  // A finished turn hands the keyboard back: the reader typed the question here
  // and is about to type the follow-up, so the composer they were in is where
  // the caret belongs.
  useEffect(() => {
    if (isLoading) {
      heldFocusRef.current = document.activeElement === textareaRef.current;
      return;
    }
    if (heldFocusRef.current) {
      heldFocusRef.current = false;
      textareaRef.current?.focus();
    }
  }, [isLoading]);

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleSubmit = () => {
    if (value.trim() && !isLoading) {
      onSend(value);
      setValue("");
    }
  };

  return (
    <div
      id="chat-input-wrapper"
      className="relative flex items-end w-full bg-insurance-surface border border-insurance-border rounded-2xl shadow-subtle focus-within:border-insurance-info focus-within:ring-4 focus-within:ring-insurance-info/10 transition-all duration-200 p-2"
      data-testid="chat-input-wrapper"
    >
      <textarea
        id="chat-textarea"
        ref={textareaRef}
        rows={1}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Ask about coverage, claims, deductibles, or policy details..."
        onKeyDown={handleKeyDown}
        aria-busy={isLoading}
        className="w-full max-h-[200px] bg-transparent text-insurance-ink placeholder-insurance-ink-tertiary resize-none outline-none py-3 px-4 overflow-y-auto"
        data-testid="chat-textarea"
        autoFocus={autoFocus}
        aria-label="Chat message input"
      />
      <button
        id="send-button"
        onClick={handleSubmit}
        disabled={isLoading || !value.trim()}
        className="absolute right-2 bottom-2 p-2 bg-insurance-accent text-white rounded-xl hover:bg-insurance-info-hover disabled:bg-insurance-border disabled:text-insurance-ink-tertiary transition-all duration-200 flex-shrink-0 shadow-subtle disabled:shadow-none"
        data-testid="send-button"
        aria-label="Send message"
      >
        {isLoading ? (
          <div
            id="send-loading-spinner"
            className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin"
            data-testid="send-loading-spinner"
          ></div>
        ) : (
          <Send className="w-5 h-5" data-testid="send-icon" />
        )}
      </button>
    </div>
  );
}
