import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Search from "../views/Search";
import { jsonResponse, renderWithClient } from "./testUtils";

function installSearchFetch() {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = input.toString();
    if (path === "/manager/storage") {
      return Promise.resolve(jsonResponse({
        locations: [{ name: "fast", path: "/models", free_bytes: 50_000_000_000, total_bytes: 100_000_000_000, writable: true, is_default: true }]
      }));
    }
    if (path.startsWith("/manager/hf/search?")) {
      return Promise.resolve(jsonResponse({
        query: "qwen",
        limit: 20,
        page: 1,
        page_size: 20,
        has_next: false,
        next_page: null,
        include_vision: true,
        vllm_arch_source: "snapshot",
        vllm_arch_count: 12,
        results: [
          {
            model_id: "Org/Good",
            architectures: ["LlamaForCausalLM"],
            is_compatible: true,
            compat_reason: null,
            size_estimate_gb: 42,
            downloads: 10,
            likes: 2,
            last_modified: null,
            tags: [],
            pipeline_tag: null
          },
          {
            model_id: "Org/Bad",
            architectures: ["NopeModel"],
            is_compatible: false,
            compat_reason: "No supported architecture",
            size_estimate_gb: null,
            downloads: 1,
            likes: 0,
            last_modified: null,
            tags: [],
            pipeline_tag: null
          }
        ]
      }));
    }
    if (path === "/manager/install" && init?.method === "POST") {
      return Promise.resolve(jsonResponse({ alias: "good", status: "queued" }, { status: 202 }));
    }
    return Promise.resolve(new Response("missing mock", { status: 500 }));
  });
}

describe("Search", () => {
  it("queries with filters, disables incompatible installs, and submits size estimate", async () => {
    const fetchMock = installSearchFetch();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithClient(<Search />);

    await user.type(screen.getByLabelText("Query"), "qwen");
    await user.click(screen.getByLabelText("Vision models"));
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("Org/Good")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/manager/hf/search?q=qwen&page=1&limit=20&include_vision=true&filter_compat=false",
      expect.objectContaining({ credentials: "include" })
    );
    expect(screen.getByRole("button", { name: "Install Org/Bad" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Install Org/Good" }));
    await user.click(await screen.findByRole("button", { name: "Install" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([path]) => path === "/manager/install");
      expect(call).toBeTruthy();
      const body = JSON.parse((call?.[1] as RequestInit).body as string);
      expect(body).toMatchObject({
        alias: "good",
        model: "Org/Good",
        size_estimate_gb: 42
      });
    });
  });
});
