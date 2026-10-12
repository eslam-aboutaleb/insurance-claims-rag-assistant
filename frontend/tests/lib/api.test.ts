import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  ApiError,
  deleteConversation,
  getConversations,
  renameConversation,
} from "@/lib/api";

const TOKEN = "header.payload.signature";
const CONVERSATION_ID = "11111111-1111-1111-1111-111111111111";
const CONVERSATIONS_URL = "http://localhost:8000/api/v1/chat/conversations";
const CONVERSATION_URL = `${CONVERSATIONS_URL}/${CONVERSATION_ID}`;

/** A fetch-shaped response carrying a JSON body. */
function jsonResponse(
  body: unknown,
  init: { ok?: boolean; status?: number } = {},
): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: () => Promise.resolve(body),
  } as Response;
}

/** Replace fetch with one that always answers the same response. */
function installFetch(response: Response) {
  const fetchMock = vi.fn(() => Promise.resolve(response));
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("ApiError", () => {
  it("creates an error with status and code", () => {
    const error = new ApiError("Session expired", 401, "UNAUTHORIZED");
    expect(error.message).toBe("Session expired");
    expect(error.status).toBe(401);
    expect(error.code).toBe("UNAUTHORIZED");
    expect(error.name).toBe("ApiError");
  });

  it("creates an error without code", () => {
    const error = new ApiError("Something went wrong", 500);
    expect(error.status).toBe(500);
    expect(error.code).toBeUndefined();
  });
});

describe("getConversations", () => {
  beforeEach(() => {
    installFetch(jsonResponse({ conversations: [] }));
  });

  it("requests the unfiltered list when no query is given", async () => {
    const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;

    await getConversations(TOKEN);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(CONVERSATIONS_URL);
  });

  it("appends ?q= when a query is given", async () => {
    const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;

    await getConversations(TOKEN, "water damage");

    expect(fetchMock.mock.calls[0][0]).toBe(
      `${CONVERSATIONS_URL}?q=water%20damage`,
    );
  });
});

describe("renameConversation", () => {
  const renamed = {
    id: CONVERSATION_ID,
    title: "Spring flood claim",
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-02T00:00:00Z",
  };

  it("sends the new title as JSON on a PATCH and returns the stored metadata", async () => {
    const fetchMock = installFetch(jsonResponse(renamed));

    const result = await renameConversation(
      TOKEN,
      CONVERSATION_ID,
      "  Spring flood claim  ",
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(CONVERSATION_URL);
    expect(init?.method).toBe("PATCH");
    // The backend only parses a JSON body for an application/json request, so
    // the content type is what makes the rename succeed in a real browser.
    expect(init?.credentials).toBe("include");
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
    expect((init?.headers as Record<string, string>).Authorization).toBe(
      `Bearer ${TOKEN}`,
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      title: "  Spring flood claim  ",
    });
    expect(result).toEqual(renamed);
  });

  it("throws ApiError with the server's detail when the rename is refused", async () => {
    installFetch(
      jsonResponse(
        { detail: "Title must be between 1 and 255 characters" },
        { ok: false, status: 422 },
      ),
    );

    await expect(
      renameConversation(TOKEN, CONVERSATION_ID, ""),
    ).rejects.toThrow(ApiError);
  });
});

describe("deleteConversation", () => {
  it("resolves on a 204 without reading a body", async () => {
    const json = vi.fn(() => Promise.resolve({}));
    const fetchMock = installFetch({
      ok: true,
      status: 204,
      json,
    } as unknown as Response);

    await expect(
      deleteConversation(TOKEN, CONVERSATION_ID),
    ).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(CONVERSATION_URL);
    expect(init?.method).toBe("DELETE");
    expect(json).not.toHaveBeenCalled();
  });

  it("throws ApiError when the delete is refused", async () => {
    installFetch(
      jsonResponse(
        { detail: "Conversation not found" },
        { ok: false, status: 404 },
      ),
    );

    await expect(
      deleteConversation(TOKEN, CONVERSATION_ID),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      message: "Conversation not found",
    });
  });
});
