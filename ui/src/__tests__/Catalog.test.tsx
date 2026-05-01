import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Catalog from "../views/Catalog";
import { jsonResponse, renderWithClient } from "./testUtils";

const rows = [
  {
    alias: "cfg",
    hf_model_id: "Org/Config",
    source: "config",
    quantization: null,
    gpus: "all",
    max_model_len: null,
    storage_location: "fast",
    cache_path: null,
    size_bytes: null,
    status: "installed",
    installed_at: null,
    last_used_at: null,
    request_count: 2,
    extra_args: [],
    revision: "main",
    resolved_sha: null
  },
  {
    alias: "ui",
    hf_model_id: "Org/UI",
    source: "ui_install",
    quantization: null,
    gpus: [0],
    max_model_len: null,
    storage_location: "fast",
    cache_path: null,
    size_bytes: 2000,
    status: "installed",
    installed_at: null,
    last_used_at: null,
    request_count: 0,
    extra_args: [],
    revision: "main",
    resolved_sha: null
  },
  {
    alias: "__cache__:abc",
    hf_model_id: "Org/CacheOnly",
    source: "ui_install",
    quantization: null,
    gpus: "all",
    max_model_len: null,
    storage_location: "fast",
    cache_path: null,
    size_bytes: 3000,
    status: "installed",
    installed_at: null,
    last_used_at: null,
    request_count: 0,
    extra_args: [],
    revision: "main",
    resolved_sha: null
  }
];

function installCatalogFetch() {
  return vi.fn((input: RequestInfo | URL) => {
    const path = input.toString();
    if (path === "/manager/catalog?include_cache_only=true") {
      return Promise.resolve(jsonResponse({ models: rows }));
    }
    if (path === "/manager/storage") {
      return Promise.resolve(jsonResponse({
        locations: [{ name: "fast", path: "/models", free_bytes: 1000, total_bytes: 2000, writable: true, is_default: true }]
      }));
    }
    return Promise.resolve(jsonResponse({}));
  });
}

describe("Catalog", () => {
  it("derives cache-only source and exposes actions by row type", async () => {
    vi.stubGlobal("fetch", installCatalogFetch());
    renderWithClient(<Catalog />);

    expect(await screen.findByText("cfg")).toBeInTheDocument();
    expect(screen.getByText("config")).toBeInTheDocument();
    expect(screen.getByText("ui_install")).toBeInTheDocument();
    expect(screen.getByText("cache-only")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load cfg" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load ui" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load __cache__:abc" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create alias for Org/CacheOnly" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete cache for cfg" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete cache for ui" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete cache for __cache__:abc" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit ui" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit cfg" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit __cache__:abc" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove ui" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove __cache__:abc" })).toBeInTheDocument();
  });

  it("opens create-alias form for cache-only rows", async () => {
    vi.stubGlobal("fetch", installCatalogFetch());
    const user = userEvent.setup();
    renderWithClient(<Catalog />);

    await user.click(await screen.findByRole("button", { name: "Create alias for Org/CacheOnly" }));

    expect(screen.getByText("Create Alias")).toBeInTheDocument();
    expect(screen.getByDisplayValue("cacheonly")).toBeInTheDocument();
  });

  it("edits launch settings for UI-installed rows", async () => {
    const fetchMock = installCatalogFetch();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithClient(<Catalog />);

    await user.click(await screen.findByRole("button", { name: "Edit ui" }));
    expect(screen.getByText("Edit Install")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Max Context"), "262144");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/manager/install/ui",
        expect.objectContaining({
          method: "PATCH",
          body: expect.stringContaining("\"max_model_len\":262144")
        })
      );
    });
  });
});
