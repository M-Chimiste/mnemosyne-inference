import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Downloads from "../views/Downloads";
import { jsonResponse, renderWithClient } from "./testUtils";

function installDownloadsFetch() {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = input.toString();
    if (path === "/manager/downloads") {
      return Promise.resolve(jsonResponse({
        downloads: [{
          model: "Org/Foo",
          alias: "foo",
          status: "downloading",
          started_at: 1700000000,
          finished_at: null,
          path: "/models/hub/foo",
          error: null,
          revision: "main",
          bytes_downloaded: 50_000_000,
          total_bytes: 100_000_000,
          elapsed_seconds: 5
        }]
      }));
    }
    if (path === "/manager/install/foo") {
      return Promise.resolve(jsonResponse({
        alias: "foo",
        hf_model_id: "Org/Foo",
        source: "ui_install",
        quantization: null,
        gpus: "all",
        max_model_len: null,
        storage_location: "fast",
        cache_path: null,
        size_bytes: null,
        status: "queued",
        installed_at: null,
        last_used_at: null,
        request_count: 0,
        extra_args: [],
        revision: "main",
        resolved_sha: null,
        active: true,
        download: {
          status: "downloading",
          started_at: 1700000000,
          finished_at: null,
          bytes_downloaded: 50_000_000,
          total_bytes: 100_000_000,
          error: null,
          pid: 123,
          elapsed_seconds: 5
        }
      }));
    }
    if (path === "/manager/install/foo/cancel" && init?.method === "POST") {
      return Promise.resolve(jsonResponse({ alias: "foo", status: "cancelling" }));
    }
    return Promise.resolve(new Response("missing mock", { status: 500 }));
  });
}

function completedDownloadsFetch() {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = input.toString();
    if (path === "/manager/downloads") {
      return Promise.resolve(jsonResponse({
        downloads: [{
          model: "Org/Foo",
          alias: "foo",
          status: "complete",
          started_at: 1700000000,
          finished_at: 1700000100,
          path: "/models/hub/foo",
          error: null,
          revision: "main",
          bytes_downloaded: 100_000_000,
          total_bytes: 100_000_000,
          elapsed_seconds: 100
        }]
      }));
    }
    if (path === "/manager/install/foo/download" && init?.method === "DELETE") {
      return Promise.resolve(jsonResponse({ alias: "foo", cleared: "Org/Foo", deleted_downloads: 1 }));
    }
    return Promise.resolve(new Response("missing mock", { status: 500 }));
  });
}

describe("Downloads", () => {
  it("polls install detail and cancels active downloads", async () => {
    const fetchMock = installDownloadsFetch();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithClient(<Downloads />);

    await user.click(await screen.findByRole("button", { name: "foo" }));

    expect(await screen.findByText("Downloaded")).toBeInTheDocument();
    expect(screen.getByText("50 MB / 100 MB")).toBeInTheDocument();
    expect(screen.getAllByText("50%").length).toBeGreaterThanOrEqual(1);

    await user.click(screen.getByRole("button", { name: "Cancel foo" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/manager/install/foo/cancel",
        expect.objectContaining({ method: "POST", credentials: "include" })
      );
    });
  });

  it("clears completed download records by alias", async () => {
    const fetchMock = completedDownloadsFetch();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithClient(<Downloads />);

    await user.click(await screen.findByRole("button", { name: "Clear record for Org/Foo" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/manager/install/foo/download",
        expect.objectContaining({ method: "DELETE", credentials: "include" })
      );
    });
  });
});
