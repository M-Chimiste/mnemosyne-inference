import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Dashboard from "../views/Dashboard";
import { jsonResponse, renderWithClient } from "./testUtils";

function installDashboardFetch({ gpuAvailable = true } = {}) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = input.toString();
    if (path === "/manager/status") {
      return Promise.resolve(jsonResponse({
        loaded_model: "Qwen/Qwen3-8B",
        loading: false,
        vllm_pid: 123,
        loaded_at: 1700000000,
        loaded_at_human: "2023-11-14 22:13:20",
        tp_size: 1,
        gpu_mem_util: 0.86,
        inner_endpoint: "http://127.0.0.1:8002",
        alias: "qwen",
        gpus: [0],
        quantization: null,
        max_model_len: null,
        storage_location: "fast",
        last_used_at: 1700000020,
        idle_seconds: 1,
        seconds_until_eviction: 899,
        inflight_requests: 0,
        swap_target: null,
        vllm_arch_count: 12,
        vllm_arch_source: "snapshot"
      }));
    }
    if (path === "/manager/gpu") {
      return Promise.resolve(jsonResponse(gpuAvailable ? {
        available: true,
        gpus: [{ index: 0, name: "NVIDIA RTX", memory_used_mb: 1024, memory_total_mb: 4096, utilization_pct: 25 }]
      } : { available: false, gpus: [] }));
    }
    if (path === "/manager/catalog?include_cache_only=false") {
      return Promise.resolve(jsonResponse({
        models: [{
          alias: "qwen",
          hf_model_id: "Qwen/Qwen3-8B",
          source: "ui_install",
          quantization: null,
          gpus: [0],
          max_model_len: null,
          storage_location: "fast",
          cache_path: null,
          size_bytes: 10,
          status: "installed",
          installed_at: null,
          last_used_at: null,
          request_count: 7,
          extra_args: [],
          revision: "main",
          resolved_sha: null
        }]
      }));
    }
    if (path === "/manager/unload" && init?.method === "POST") {
      return Promise.resolve(jsonResponse({ status: "unloaded" }));
    }
    return Promise.resolve(new Response("missing mock", { status: 500 }));
  });
}

describe("Dashboard", () => {
  it("shows live GPU telemetry and catalog request count", async () => {
    vi.stubGlobal("fetch", installDashboardFetch());
    renderWithClient(<Dashboard />);

    expect(await screen.findByText("NVIDIA RTX")).toBeInTheDocument();
    expect(screen.getByText("Catalog Request Count")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("cap 86%")).toBeInTheDocument();
  });

  it("handles missing GPU telemetry and unloads resident model", async () => {
    const fetchMock = installDashboardFetch({ gpuAvailable: false });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithClient(<Dashboard />);

    expect(await screen.findByText("No live GPU telemetry available from nvidia-smi.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Unload resident model" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/manager/unload",
        expect.objectContaining({ method: "POST", credentials: "include" })
      );
    });
  });
});
