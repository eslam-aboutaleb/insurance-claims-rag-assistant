import { describe, it, expect, vi, beforeEach } from "vitest";
import { ComponentProps, FormEvent } from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "@/context/AuthContext";
import { toast } from "react-hot-toast";
import Sidebar from "@/components/Sidebar";

vi.mock("react-hot-toast", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const CONVERSATIONS = [
  {
    id: "11111111-1111-1111-1111-111111111111",
    title: "Old convo about water damage",
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
  },
  {
    id: "22222222-2222-2222-2222-222222222222",
    title: "Old convo about filing a claim",
    created_at: "2024-01-02T00:00:00Z",
    updated_at: "2024-01-02T00:00:00Z",
  },
  {
    id: "33333333-3333-3333-3333-333333333333",
    title: "Old convo about policy limits",
    created_at: "2024-01-03T00:00:00Z",
    updated_at: "2024-01-03T00:00:00Z",
  },
];

const JWT = "header.payload.signature";
const WATER_ID = "11111111-1111-1111-1111-111111111111";

/** The conversations the API double serves, in the shape the client expects. */
interface ServedConversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

/** Ways the API double can be told to refuse a write. */
interface ApiBehavior {
  /** Status the PATCH answers with, when it should not succeed. */
  renameStatus?: number;
}

/**
 * Install a fetch double for the conversations API.
 *
 * The list it serves lives in the double rather than in the fixture, so a
 * rename or a delete survives the refetch that follows it exactly the way the
 * server would remember it. The spy comes back so a test can assert on the
 * requests the sidebar actually sent.
 */
function mockFetch(
  conversations: unknown[] = CONVERSATIONS,
  status = 200,
  behavior: ApiBehavior = {},
) {
  const server: ServedConversation[] = (
    conversations as ServedConversation[]
  ).map((conversation) => ({ ...conversation }));
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.includes("/api/v1/auth/me")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ user_id: "user-123" }),
      } as Response);
    }
    const [path, search = ""] = url.split("?");
    if (path.endsWith("/api/v1/chat/conversations")) {
      const query = new URLSearchParams(search).get("q") ?? "";
      const visible = query
        ? server.filter((conversation) =>
            conversation.title.toLowerCase().includes(query.toLowerCase()),
          )
        : server;
      return Promise.resolve({
        ok: status === 200,
        status,
        json: () => Promise.resolve({ conversations: visible }),
      } as Response);
    }
    const detail = url.match(/\/api\/v1\/chat\/conversations\/([^/?#]+)/);
    if (detail && init?.method === "PATCH") {
      if (behavior.renameStatus && behavior.renameStatus !== 200) {
        return Promise.resolve({
          ok: false,
          status: behavior.renameStatus,
          json: () => Promise.resolve({ detail: "That title was refused." }),
        } as Response);
      }
      const target = server.find(
        (conversation) => conversation.id === detail[1],
      );
      const { title } = JSON.parse(String(init.body)) as { title: string };
      if (target) {
        // The backend stores the trimmed title, so the refetch that follows
        // has to show what the server would have kept.
        target.title = title.trim();
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(target),
      } as Response);
    }
    if (detail && init?.method === "DELETE") {
      const index = server.findIndex(
        (conversation) => conversation.id === detail[1],
      );
      if (index >= 0) server.splice(index, 1);
      return Promise.resolve({
        ok: true,
        status: 204,
        json: () => Promise.resolve({}),
      } as Response);
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    } as Response);
  }) as unknown as typeof fetch;
  global.fetch = fetchMock;
  return { fetchMock, server };
}

/** Every fetch call the double has answered, with its method. */
type FetchMock = ReturnType<typeof vi.fn>;

function requestsFor(fetchMock: FetchMock, method: string) {
  return fetchMock.mock.calls.filter(
    ([, init]) => (init as RequestInit | undefined)?.method === method,
  );
}

function renderSidebar(props: Partial<ComponentProps<typeof Sidebar>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const onSelectChat = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Sidebar onNewChat={vi.fn()} onSelectChat={onSelectChat} {...props} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return {
    onSelectChat,
    queryClient,
    fetchMock: global.fetch as unknown as FetchMock,
  };
}

/** Reveal a row's actions the way hovering it with a pointer does. */
function revealRow(id: string) {
  fireEvent.pointerOver(screen.getByTestId(`conversation-row-${id}`));
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.setItem("omnicare_token", JWT);
  window.localStorage.setItem("omnicare_user_id", "user-123");
  mockFetch();
});

describe("Sidebar conversation history", () => {
  it("renders every conversation returned by the API, not just new ones", async () => {
    renderSidebar();

    const history = screen.getByTestId("chat-history");
    await waitFor(() => {
      expect(history.querySelectorAll("button")).toHaveLength(
        CONVERSATIONS.length,
      );
    });

    for (const conversation of CONVERSATIONS) {
      expect(screen.getByText(conversation.title)).toBeInTheDocument();
    }
  });

  it("does not show the empty-state placeholder when conversations exist", async () => {
    renderSidebar();

    await screen.findByText("Old convo about water damage");
    expect(screen.queryByText("No conversations yet")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Your conversations will appear here."),
    ).not.toBeInTheDocument();
  });

  it("keeps old conversations visible after refreshTrigger changes", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <Sidebar
            onNewChat={vi.fn()}
            onSelectChat={vi.fn()}
            refreshTrigger={0}
          />
        </AuthProvider>
      </QueryClientProvider>,
    );

    await screen.findByText("Old convo about water damage");

    rerender(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <Sidebar
            onNewChat={vi.fn()}
            onSelectChat={vi.fn()}
            refreshTrigger={1}
          />
        </AuthProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(
        screen.getByText("Old convo about water damage"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText("Old convo about filing a claim"),
    ).toBeInTheDocument();
  });

  it("calls onSelectChat with the clicked conversation id", async () => {
    const { onSelectChat } = renderSidebar();

    fireEvent.click(await screen.findByText("Old convo about filing a claim"));

    expect(onSelectChat).toHaveBeenCalledTimes(1);
    expect(onSelectChat).toHaveBeenCalledWith(
      "22222222-2222-2222-2222-222222222222",
    );
  });

  it("marks the active conversation using activeConversationId", async () => {
    renderSidebar({
      activeConversationId: "33333333-3333-3333-3333-333333333333",
    });

    const active = await screen.findByText("Old convo about policy limits");
    const activeButton = active.closest("button");
    const inactiveButton = screen
      .getByText("Old convo about water damage")
      .closest("button");

    expect(activeButton?.className).toContain("bg-insurance-border");
    expect(inactiveButton?.className).not.toContain("bg-insurance-border");
  });

  it("declares type=button on every sidebar button so clicks never submit a wrapping form", async () => {
    const onNewChat = vi.fn();
    const onLogout = vi.fn();
    renderSidebar({ onNewChat, onLogout });

    await screen.findByText("Old convo about water damage");

    const buttons = Array.from(
      document.querySelectorAll<HTMLButtonElement>(
        '[data-testid="sidebar"] button',
      ),
    );
    expect(buttons.length).toBeGreaterThanOrEqual(CONVERSATIONS.length + 1);
    for (const button of buttons) {
      expect(button.getAttribute("type")).toBe("button");
    }
  });

  it("does not submit a surrounding form when a conversation is clicked", async () => {
    const onSubmit = vi.fn((e: FormEvent) => e.preventDefault());
    const onSelectChat = vi.fn();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <form onSubmit={onSubmit}>
            <Sidebar onNewChat={vi.fn()} onSelectChat={onSelectChat} />
          </form>
        </AuthProvider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByText("Old convo about water damage"));

    expect(onSelectChat).toHaveBeenCalledWith(
      "11111111-1111-1111-1111-111111111111",
    );
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("shows the empty state only when the user truly has no conversations", async () => {
    mockFetch([]);
    renderSidebar();

    expect(await screen.findByText("No conversations yet")).toBeInTheDocument();
  });
});

describe("Sidebar conversation filter", () => {
  it("re-queries with ?q= while the filter is typed", async () => {
    const { fetchMock } = renderSidebar();

    fireEvent.change(screen.getByPlaceholderText("Search conversations"), {
      target: { value: "water" },
    });

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/v1/chat/conversations?q=water"),
        ),
      ).toBe(true);
    });
    await waitFor(() => {
      expect(
        screen.queryByText("Old convo about policy limits"),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.getByText("Old convo about water damage"),
    ).toBeInTheDocument();
  });

  it("offers a filtered empty state instead of the generic one", async () => {
    const { fetchMock } = mockFetch();
    renderSidebar();

    fireEvent.change(screen.getByPlaceholderText("Search conversations"), {
      target: { value: "zzz" },
    });

    expect(
      await screen.findByText("No conversations match"),
    ).toBeInTheDocument();
    expect(screen.queryByText("No conversations yet")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Your conversations will appear here."),
    ).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalled();
  });
});

describe("Sidebar conversation actions", () => {
  it("marks the active row with aria-current and leaves the others alone", async () => {
    renderSidebar({ activeConversationId: WATER_ID });

    const active = await screen.findByRole("button", { current: "page" });
    expect(active).toHaveTextContent("Old convo about water damage");
    expect(
      screen.getByText("Old convo about policy limits").closest("button"),
    ).not.toHaveAttribute("aria-current");
  });

  it("renames a conversation through the API and shows the new title", async () => {
    const { fetchMock } = mockFetch();
    renderSidebar();

    await screen.findByText("Old convo about water damage");
    revealRow(WATER_ID);
    fireEvent.click(screen.getByTestId(`rename-button-${WATER_ID}`));

    const input = screen.getByTestId(`rename-input-${WATER_ID}`);
    fireEvent.change(input, { target: { value: "  Kitchen flood claim  " } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);

    await waitFor(() => {
      expect(screen.getByText("Kitchen flood claim")).toBeInTheDocument();
    });

    const patches = requestsFor(fetchMock, "PATCH");
    expect(patches).toHaveLength(1);
    expect(String(patches[0][0])).toContain(
      `/api/v1/chat/conversations/${WATER_ID}`,
    );
    expect(JSON.parse(String(patches[0][1]?.body))).toEqual({
      title: "Kitchen flood claim",
    });
  });

  it("leaves the row untouched when the rename is cancelled with Escape", async () => {
    const { fetchMock } = mockFetch();
    renderSidebar();

    await screen.findByText("Old convo about water damage");
    revealRow(WATER_ID);
    fireEvent.click(screen.getByTestId(`rename-button-${WATER_ID}`));

    const input = screen.getByTestId(`rename-input-${WATER_ID}`);
    fireEvent.change(input, { target: { value: "Discarded title" } });
    fireEvent.keyDown(input, { key: "Escape" });

    await waitFor(() => {
      expect(
        screen.queryByTestId(`rename-input-${WATER_ID}`),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.getByText("Old convo about water damage"),
    ).toBeInTheDocument();
    expect(requestsFor(fetchMock, "PATCH")).toHaveLength(0);
  });

  it("keeps save disabled until the new title has content", async () => {
    mockFetch();
    renderSidebar();

    await screen.findByText("Old convo about water damage");
    revealRow(WATER_ID);
    fireEvent.click(screen.getByTestId(`rename-button-${WATER_ID}`));

    fireEvent.change(screen.getByTestId(`rename-input-${WATER_ID}`), {
      target: { value: "   " },
    });
    expect(screen.getByTestId(`rename-save-${WATER_ID}`)).toBeDisabled();

    fireEvent.change(screen.getByTestId(`rename-input-${WATER_ID}`), {
      target: { value: "Kitchen flood claim" },
    });
    expect(screen.getByTestId(`rename-save-${WATER_ID}`)).toBeEnabled();
  });

  it("reports a refused rename with a toast and keeps the old title", async () => {
    const { fetchMock } = mockFetch(CONVERSATIONS, 200, { renameStatus: 500 });
    renderSidebar();

    await screen.findByText("Old convo about water damage");
    revealRow(WATER_ID);
    fireEvent.click(screen.getByTestId(`rename-button-${WATER_ID}`));

    fireEvent.change(screen.getByTestId(`rename-input-${WATER_ID}`), {
      target: { value: "Kitchen flood claim" },
    });
    fireEvent.submit(
      screen
        .getByTestId(`rename-input-${WATER_ID}`)
        .closest("form") as HTMLFormElement,
    );

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalled();
    });
    expect(
      screen.getByText("Old convo about water damage"),
    ).toBeInTheDocument();
    expect(toast.success).not.toHaveBeenCalled();
    expect(requestsFor(fetchMock, "PATCH")).toHaveLength(1);
  });

  it("asks for confirmation inline before deleting, then deletes", async () => {
    const onDeleteConversation = vi.fn();
    const { fetchMock } = mockFetch();
    renderSidebar({ onDeleteConversation });

    await screen.findByText("Old convo about water damage");
    revealRow(WATER_ID);
    fireEvent.click(screen.getByTestId(`delete-button-${WATER_ID}`));

    // The confirmation is inline and the API is untouched until it is accepted.
    expect(screen.getByText("Delete this conversation?")).toBeInTheDocument();
    expect(requestsFor(fetchMock, "DELETE")).toHaveLength(0);

    fireEvent.click(screen.getByTestId(`delete-confirm-${WATER_ID}`));

    await waitFor(() => {
      expect(onDeleteConversation).toHaveBeenCalledWith(WATER_ID);
    });
    const deletes = requestsFor(fetchMock, "DELETE");
    expect(deletes).toHaveLength(1);
    expect(String(deletes[0][0])).toContain(
      `/api/v1/chat/conversations/${WATER_ID}`,
    );
    await waitFor(() => {
      expect(
        screen.queryByText("Old convo about water damage"),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.getByText("Old convo about filing a claim"),
    ).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalled();
  });

  it("cancels the delete without touching the API", async () => {
    const onDeleteConversation = vi.fn();
    const { fetchMock } = mockFetch();
    renderSidebar({ onDeleteConversation });

    await screen.findByText("Old convo about water damage");
    revealRow(WATER_ID);
    fireEvent.click(screen.getByTestId(`delete-button-${WATER_ID}`));
    fireEvent.click(screen.getByTestId(`delete-cancel-${WATER_ID}`));

    await waitFor(() => {
      expect(
        screen.queryByText("Delete this conversation?"),
      ).not.toBeInTheDocument();
    });
    expect(requestsFor(fetchMock, "DELETE")).toHaveLength(0);
    expect(onDeleteConversation).not.toHaveBeenCalled();
    expect(
      screen.getByText("Old convo about water damage"),
    ).toBeInTheDocument();
  });
});
