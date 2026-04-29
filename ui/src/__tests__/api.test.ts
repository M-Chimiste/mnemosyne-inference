import { describe, expect, it, vi } from "vitest";
import { api, jsonBody } from "../api/client";
import { jsonResponse } from "./testUtils";

describe("api client", () => {
  it("sends credentials and parses JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api("/manager/status")).resolves.toEqual({ ok: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/manager/status",
      expect.objectContaining({ credentials: "include" })
    );
  });

  it("sets JSON content type when a body is present", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ queued: true }));
    vi.stubGlobal("fetch", fetchMock);

    await api("/manager/install", { method: "POST", ...jsonBody({ alias: "qwen" }) });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe("include");
    expect((init.headers as Headers).get("Content-Type")).toBe("application/json");
    expect(init.body).toBe(JSON.stringify({ alias: "qwen" }));
  });

  it("throws ApiError with response body text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("nope", { status: 409 })));

    await expect(api("/manager/load", { method: "POST" })).rejects.toMatchObject({
      status: 409,
      body: "nope"
    });
  });
});
