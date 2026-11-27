import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type {
  ArtifactStatus,
  DeploymentRun,
  DeviceInstrumentation,
  InstrumentationProfile,
  ModuleRecord,
  StepAction,
} from "@/lib/types";

import { server } from "@/mocks/server";
import { renderApp } from "./utils";

const API = "/api/v1";

function json(data: unknown, status = 200): Response {
  return HttpResponse.json(data as Record<string, unknown>, { status });
}

function errorEnvelope(status: number, code: string, message: string, hint?: string): Response {
  return HttpResponse.json({ error: { code, message, ...(hint ? { hint } : {}) } }, { status });
}

function device(overrides: Partial<DeviceInstrumentation> = {}): DeviceInstrumentation {
  return {
    instance: "avd-pixel7-01",
    api_level: 35,
    abi: "x86_64",
    image_id: "img-api35-play",
    image_tag: "google_apis_playstore",
    serial: "emulator-5554",
    running: true,
    boot_completed: true,
    root_implementation: "magisk",
    capabilities: {
      adb_root: false,
      writable_system: false,
      magisk: true,
      magisk_version: "27.0",
      magisk_environment_ready: false,
      zygisk_available: true,
      zygisk_enabled: true,
      zygisk_active: false,
      lsposed_installed: true,
      lsposed_version: "1.9.2",
      lsposed_active: false,
      reboot_required: true,
      notes: ["module scripts wait for the Magisk Additional Setup"],
    },
    xposed_modules: [],
    magisk_modules: [],
    compatible: true,
    blockers: ["reboot required before Zygisk becomes active"],
    probed_at: "2026-11-27T20:00:00Z",
    ...overrides,
  };
}

function profile(overrides: Partial<InstrumentationProfile> = {}): InstrumentationProfile {
  return {
    id: "prof_builtin_root",
    name: "Root",
    description: "Verify that the AVD has usable root.",
    revision: 1,
    steps: [
      { id: "root-verify", action: "verify_root", module_id: "", module_identifier: "", params: {} },
    ],
    builtin: true,
    created_at: "2026-11-27T20:00:00Z",
    updated_at: "2026-11-27T20:00:00Z",
    ...overrides,
  };
}

function stepResult(action: StepAction, message: string) {
  return {
    step_id: `step-${action}`,
    action,
    ok: false,
    state: "absent" as const,
    message,
    data: {},
  };
}

function run(overrides: Partial<DeploymentRun> = {}): DeploymentRun {
  return {
    id: "irun_abc123",
    profile_id: "prof_custom",
    label: "Nightly LSPosed rollout",
    state: "partial",
    targets: ["avd-pixel7-01", "avd-pixel7-02"],
    results: [
      {
        instance: "avd-pixel7-01",
        ok: false,
        skipped: true,
        error: "instance is not running (start it first)",
        steps: [],
      },
      {
        instance: "avd-pixel7-02",
        ok: false,
        skipped: false,
        error: "no root: install Magisk",
        steps: [stepResult("verify_root", "no root: install Magisk or use a userdebug image")],
      },
    ],
    created_at: "2026-11-27T20:00:00Z",
    started_at: "2026-11-27T20:00:01Z",
    finished_at: "2026-11-27T20:00:05Z",
    error: "",
    ...overrides,
  };
}

function moduleRecord(): ModuleRecord {
  return {
    id: "mod_lsposed",
    kind: "lsposed_zip",
    name: "LSPosed",
    package_name: "zygisk_lsposed",
    version: "1.9.2",
    version_code: 1920,
    min_sdk: 27,
    abi: "arm64-v8a",
    description: "LSPosed framework module",
    sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    size_bytes: 2048,
    source: "upload",
    source_url: "",
    file_path: "/data/modules/lsposed.zip",
    metadata: {},
    created_at: "2026-11-27T20:00:00Z",
    updated_at: "2026-11-27T20:00:00Z",
  };
}

/* -- toolchain fixtures (the real pinned releases) ------------------------- */

const MAGISK_SHA = "ae1a02b1ab608a51d5bc9b323e0588d06d30d9987ac8da01f4710d76f705dccb";
const MAGISK_URL =
  "https://github.com/topjohnwu/Magisk/releases/download/v26.1/Magisk-v26.1.apk";
const LSPOSED_SHA = "0ebc6bcb465d1c4b44b7220ab5f0252e6b4eb7fe43da74650476d2798bb29622";
const LSPOSED_URL =
  "https://github.com/LSPosed/LSPosed/releases/download/v1.9.2/LSPosed-v1.9.2-7024-zygisk-release.zip";

function pinnedArtifact(
  key: "magisk" | "lsposed",
  overrides: Partial<ArtifactStatus> = {},
): ArtifactStatus {
  const base: ArtifactStatus =
    key === "magisk"
      ? {
          key: "magisk",
          name: "Magisk",
          version: "26.1 (26100)",
          kind: "apk",
          min_api: 21,
          source_url: MAGISK_URL,
          pinned_sha256: MAGISK_SHA,
          pinned_size_bytes: 11_411_692,
          present: false,
          verified: false,
          path: "",
          size_bytes: 0,
          sha256: "",
          acquired_at: "",
        }
      : {
          key: "lsposed",
          name: "LSPosed",
          version: "1.9.2 (7024)",
          kind: "module_zip",
          min_api: 27,
          source_url: LSPOSED_URL,
          pinned_sha256: LSPOSED_SHA,
          pinned_size_bytes: 2_462_055,
          present: false,
          verified: false,
          path: "",
          size_bytes: 0,
          sha256: "",
          acquired_at: "",
        };
  return { ...base, ...overrides };
}

function toolchainView(artifacts: ArtifactStatus[], available = true) {
  return { artifacts, manifest: [], available };
}

describe("Root & Instrumentation page", () => {
  it("renders installed, enabled and active as separate states with the reboot requirement", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API}/instrumentation/scan`, () => json({ devices: [device()] })),
    );

    renderApp({ route: "/instrumentation" });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" }),
    ).toBeInTheDocument();

    await user.click(await screen.findByRole("checkbox", { name: "avd-pixel7-01" }));
    await user.click(screen.getByRole("button", { name: "Scan selected" }));

    await screen.findAllByText("avd-pixel7-01");
    // Zygisk is enabled but not active; LSPosed is installed but not active.
    expect(await screen.findByText("enabled: yes")).toBeInTheDocument();
    expect(screen.getAllByText("active: no")).toHaveLength(2);
    expect(screen.getAllByText("Installed: yes")).toHaveLength(2);
    expect(screen.getByText("Environment ready: no")).toBeInTheDocument();
    expect(screen.getAllByText(/Additional Setup/).length).toBeGreaterThan(0);
    expect(screen.getByText("reboot required before Zygisk becomes active")).toBeInTheDocument();
    expect(screen.getByText("Reboot required")).toBeInTheDocument();
  });

  it("renders the Xposed inventory with scope titles and surfaces an unavailable inventory as a note", async () => {
    const user = userEvent.setup();
    const baseCapabilities = device().capabilities;
    const withInventory = device({
      instance: "avd-xposed-01",
      xposed_modules: [
        {
          identifier: "com.example.module",
          kind: "xposed_apk",
          version: "",
          enabled: true,
          active: false,
          source: "lsposed",
          scope: ["com.target.app", "com.other.app"],
          notes: [],
        },
      ],
    });
    const unavailable = device({
      instance: "avd-xposed-02",
      xposed_modules: [],
      capabilities: {
        ...baseCapabilities,
        notes: [
          "Xposed module inventory unavailable: no sqlite3 on the device; inspect modules and scope in the LSPosed manager",
        ],
      },
    });
    server.use(
      http.post(`${API}/instrumentation/scan`, () =>
        json({ devices: [withInventory, unavailable] }),
      ),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(await screen.findByRole("checkbox", { name: "avd-pixel7-01" }));
    await user.click(screen.getByRole("button", { name: "Scan selected" }));

    expect(await screen.findByText("com.example.module")).toBeInTheDocument();
    const scope = screen.getByText("2 apps");
    expect(scope).toHaveAttribute("title", "com.target.app, com.other.app");

    // The unavailable inventory is a notice, not an empty table claiming "no modules".
    expect(await screen.findByText("Xposed module inventory unavailable")).toBeInTheDocument();
    const note = /Xposed module inventory unavailable: no sqlite3/;
    expect(screen.getAllByText(note)).toHaveLength(1);
    expect(screen.getAllByRole("table")).toHaveLength(1);
  });

  it("never shows a success toast when a module import fails and surfaces the API error", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API}/instrumentation/modules`, () => json({ modules: [] })),
      http.post(`${API}/instrumentation/modules/import`, () =>
        errorEnvelope(
          422,
          "invalid_request",
          "notes.txt is not a ZIP/APK archive",
          "import a .apk (Xposed module) or a .zip (Magisk/Zygisk module)",
        ),
      ),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Module library" }));

    expect(await screen.findByText("No modules in the library")).toBeInTheDocument();

    await user.type(
      screen.getByLabelText("Import from a daemon path"),
      "C:\\modules\\notes.txt",
    );
    await user.click(screen.getByRole("button", { name: "Import path" }));

    expect((await screen.findAllByText(/not a ZIP\/APK archive/)).length).toBeGreaterThan(0);
    expect(screen.queryByText("Module imported")).not.toBeInTheDocument();
  });

  it("disables built-in profile edit/delete and duplicates instead", async () => {
    const user = userEvent.setup();
    const builtin = profile();
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [builtin] })),
      http.get(`${API}/instrumentation/modules`, () => json({ modules: [] })),
      http.post(`${API}/instrumentation/profiles/${builtin.id}/clone`, () =>
        json({ ...builtin, id: "prof_copy", name: "Root (copy)", builtin: false, revision: 1 }),
      ),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Profiles" }));

    const edit = await screen.findByRole("button", { name: "Edit Root" });
    expect(edit).toBeDisabled();
    expect(edit).toHaveAttribute("title", expect.stringContaining("422"));
    const remove = screen.getByRole("button", { name: "Delete Root" });
    expect(remove).toBeDisabled();
    expect(remove).toHaveAttribute("title", expect.stringContaining("409"));

    await user.click(screen.getByRole("button", { name: "Duplicate Root" }));
    expect(await screen.findByText("Profile duplicated")).toBeInTheDocument();
  });

  it("shows skipped targets separately from failed ones and renders the run log", async () => {
    const user = userEvent.setup();
    const custom = profile({
      id: "prof_custom",
      name: "Custom",
      builtin: false,
    });
    const deployment = run();
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [custom] })),
      http.get(`${API}/instrumentation/deployments`, () => json({ runs: [deployment] })),
      http.get(`${API}/instrumentation/deployments/${deployment.id}`, () => json(deployment)),
      http.get(`${API}/instrumentation/deployments/${deployment.id}/logs`, () =>
        json({
          logs: [
            {
              id: 1,
              run_id: deployment.id,
              instance: "",
              level: "info",
              message: "run finished: partial",
              at: "2026-11-27T20:00:05Z",
            },
          ],
        }),
      ),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Deployments" }));

    await user.click(await screen.findByRole("button", { name: /Open run Nightly LSPosed rollout/ }));

    expect(await screen.findByText("0 passed · 1 failed · 1 skipped")).toBeInTheDocument();
    expect(screen.getAllByText("Skipped").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Failed").length).toBeGreaterThan(0);
    expect(screen.getByText("state: absent")).toBeInTheDocument();
    expect(screen.getByText("no root: install Magisk or use a userdebug image")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/run finished: partial/)).toBeInTheDocument(),
    );
  });
});

describe("module library table", () => {
  it("shortens the SHA-256 in the cell and keeps the full value in the title", async () => {
    const record: ModuleRecord = {
      ...moduleRecord(),
      id: "mod_0123456789abcdef",
    };
    server.use(
      http.get(`${API}/instrumentation/modules`, () => json({ modules: [record] })),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await userEvent.setup().click(screen.getByRole("tab", { name: "Module library" }));

    const shortHash = await screen.findByText("0123456789ab…");
    expect(shortHash).toHaveAttribute("title", record.sha256);
    expect(screen.getAllByText("LSPosed ZIP").length).toBeGreaterThan(0);
    expect(screen.getByText("2.0 KiB")).toBeInTheDocument();
  });
});

describe("profile editor", () => {
  it("offers expect JSON on verify steps and blocks saving a module step without a module", async () => {
    const user = userEvent.setup();
    const custom = profile({ id: "prof_custom", name: "Custom", builtin: false });
    let posted = false;
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [custom] })),
      http.get(`${API}/instrumentation/modules`, () => json({ modules: [moduleRecord()] })),
      http.post(`${API}/instrumentation/profiles`, () => {
        posted = true;
        return json({ ...custom, id: "prof_new", name: "My profile" });
      }),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Profiles" }));
    await user.click(await screen.findByRole("button", { name: "New profile" }));

    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "My profile");

    const action = within(dialog).getByRole("combobox", { name: "Action" });
    await user.selectOptions(action, "verify");
    expect(within(dialog).getByRole("textbox", { name: "params.expect (JSON)" })).toBeInTheDocument();

    await user.selectOptions(action, "install_lsposed");
    expect(
      within(dialog).getByRole("combobox", { name: "Module from the library" }),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Create profile" }));
    expect(
      await within(dialog).findByText("Choose a library module for this step."),
    ).toBeInTheDocument();
    expect(posted).toBe(false);
    expect(screen.queryByText("Profile created")).not.toBeInTheDocument();
  });

  it("offers complete_magisk_setup and shows the rebuilt builtin profile sequences", async () => {
    const user = userEvent.setup();
    const builtinRoot = profile({
      steps: [
        { id: "adb-root", action: "enable_adb_root", module_id: "", module_identifier: "", params: {} },
        { id: "root-verify", action: "verify_root", module_id: "", module_identifier: "", params: {} },
      ],
    });
    const builtinLsposed = profile({
      id: "prof_builtin_lsposed",
      name: "LSPosed",
      description: "Magisk + Zygisk, then the LSPosed framework module from the library.",
      builtin: true,
      steps: [
        { id: "magisk-install", action: "install_magisk", module_id: "", module_identifier: "", params: {} },
        {
          id: "magisk-setup",
          action: "complete_magisk_setup",
          module_id: "",
          module_identifier: "",
          params: {},
        },
        { id: "zygisk-enable", action: "enable_zygisk", module_id: "", module_identifier: "", params: {} },
        { id: "lsposed-install", action: "install_lsposed", module_id: "", module_identifier: "", params: {} },
        { id: "lsposed-reboot", action: "reboot", module_id: "", module_identifier: "", params: {} },
        {
          id: "lsposed-manager",
          action: "install_lsposed_manager",
          module_id: "",
          module_identifier: "",
          params: {},
        },
        {
          id: "lsposed-verify",
          action: "verify",
          module_id: "",
          module_identifier: "",
          params: { expect: { magisk: true, lsposed_active: true } },
        },
      ],
    });
    const builtinXposed = profile({
      id: "prof_builtin_xposed",
      name: "Xposed Instrumentation",
      description: "Full stack: Magisk, Zygisk, LSPosed and an Xposed APK from the library.",
      builtin: true,
      steps: [
        { id: "magisk-install", action: "install_magisk", module_id: "", module_identifier: "", params: {} },
        {
          id: "magisk-setup",
          action: "complete_magisk_setup",
          module_id: "",
          module_identifier: "",
          params: {},
        },
        { id: "zygisk-enable", action: "enable_zygisk", module_id: "", module_identifier: "", params: {} },
        { id: "lsposed-install", action: "install_lsposed", module_id: "", module_identifier: "", params: {} },
        { id: "lsposed-reboot", action: "reboot", module_id: "", module_identifier: "", params: {} },
        {
          id: "lsposed-manager",
          action: "install_lsposed_manager",
          module_id: "",
          module_identifier: "",
          params: {},
        },
        { id: "xposed-apk-install", action: "install_module", module_id: "", module_identifier: "", params: {} },
        {
          id: "framework-verify",
          action: "verify",
          module_id: "",
          module_identifier: "",
          params: { expect: { magisk: true, lsposed_active: true } },
        },
      ],
    });
    server.use(
      http.get(`${API}/instrumentation/profiles`, () =>
        json({ profiles: [builtinRoot, builtinLsposed, builtinXposed] }),
      ),
      http.get(`${API}/instrumentation/modules`, () => json({ modules: [] })),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Profiles" }));

    // The card preview lists every step in order — no truncated prefix.
    expect(await screen.findByText("Enable adb root → Verify root")).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Install Magisk → Complete Magisk setup → Enable Zygisk → Install LSPosed → Reboot AVD → Install LSPosed manager → Verify expectations",
      ),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Install Magisk → Complete Magisk setup → Enable Zygisk → Install LSPosed → Reboot AVD → Install LSPosed manager → Install module → Verify expectations",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    const action = within(dialog).getByRole("combobox", { name: "Action" });
    expect(within(action).getByRole("option", { name: "Complete Magisk setup" })).toBeInTheDocument();

    const values = Array.from(action.querySelectorAll("option")).map(
      (option) => (option as HTMLOptionElement).value,
    );
    expect(values.indexOf("complete_magisk_setup")).toBe(values.indexOf("install_magisk") + 1);

    await user.selectOptions(action, "complete_magisk_setup");
    expect(
      within(dialog).getByText(/Requires the pinned Magisk APK from the Toolchain panel/),
    ).toBeInTheDocument();
  });

  it("offers the Xposed actions and blocks saving an invalid package name", async () => {
    const user = userEvent.setup();
    const custom = profile({ id: "prof_custom", name: "Custom", builtin: false });
    let posted: unknown = null;
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [custom] })),
      http.get(`${API}/instrumentation/modules`, () => json({ modules: [] })),
      http.post(`${API}/instrumentation/profiles`, async ({ request }) => {
        posted = await request.json();
        return json({ ...custom, id: "prof_new", name: "Xposed" });
      }),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Profiles" }));
    await user.click(await screen.findByRole("button", { name: "New profile" }));

    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Xposed profile");

    const action = within(dialog).getByRole("combobox", { name: "Action" });
    expect(within(action).getByRole("option", { name: "Enable Xposed module" })).toBeInTheDocument();
    expect(within(action).getByRole("option", { name: "Set Xposed scope" })).toBeInTheDocument();

    const values = Array.from(action.querySelectorAll("option")).map(
      (option) => (option as HTMLOptionElement).value,
    );
    expect(values.indexOf("enable_xposed_module")).toBe(values.indexOf("remove_module") + 1);
    expect(values.indexOf("set_xposed_scope")).toBe(values.indexOf("enable_xposed_module") + 1);
    // The round-2 actions follow the backend enum order and precede verify.
    expect(values.indexOf("enable_adb_root")).toBe(values.indexOf("set_xposed_scope") + 1);
    expect(values.indexOf("enable_writable_system")).toBe(values.indexOf("enable_adb_root") + 1);
    expect(values.indexOf("install_lsposed_manager")).toBe(
      values.indexOf("enable_writable_system") + 1,
    );
    expect(values.indexOf("reboot")).toBe(values.indexOf("install_lsposed_manager") + 1);
    expect(values.indexOf("remove_lsposed")).toBe(values.indexOf("reboot") + 1);
    expect(values.indexOf("remove_magisk")).toBe(values.indexOf("remove_lsposed") + 1);
    expect(values[values.length - 1]).toBe("verify");
    // remove_magisk is shown as an explained, permanently unavailable choice.
    expect(
      within(action).getByRole("option", { name: "Remove Magisk (unsupported)" }),
    ).toBeDisabled();

    await user.selectOptions(action, "enable_xposed_module");
    const packageInput = within(dialog).getByRole("textbox", { name: "Xposed module package" });
    expect(within(dialog).getByRole("checkbox", { name: "Enabled" })).toBeInTheDocument();
    await user.type(packageInput, "not a package");
    await user.click(within(dialog).getByRole("button", { name: "Create profile" }));
    expect(await within(dialog).findByText(/is not a valid Android package name/)).toBeInTheDocument();
    expect(posted).toBeNull();

    await user.clear(packageInput);
    await user.type(packageInput, "com.example.module");
    await user.selectOptions(action, "set_xposed_scope");
    expect(within(dialog).getByRole("textbox", { name: "Scope app packages" })).toBeInTheDocument();
    expect(
      within(dialog).getByRole("checkbox", { name: "Replace the existing scope" }),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create profile" }));
    expect(await within(dialog).findByText(/Add at least one app package/)).toBeInTheDocument();
    expect(posted).toBeNull();

    // Valid input saves, writing params.package / params.apps / params.replace.
    await user.type(
      within(dialog).getByRole("textbox", { name: "Scope app packages" }),
      "com.target.app,com.other.app",
    );
    await user.click(within(dialog).getByRole("checkbox", { name: "Replace the existing scope" }));
    await user.click(within(dialog).getByRole("button", { name: "Create profile" }));

    await waitFor(() => expect(posted).not.toBeNull());
    const body = posted as {
      steps: Array<{ action: string; params: Record<string, unknown> }>;
    };
    expect(body.steps[0]?.action).toBe("set_xposed_scope");
    expect(body.steps[0]?.params).toEqual({
      package: "com.example.module",
      apps: ["com.target.app", "com.other.app"],
      replace: false,
    });
  });

  it("validates the optional reboot timeout and sends params.timeout_s", async () => {
    const user = userEvent.setup();
    const custom = profile({ id: "prof_custom", name: "Custom", builtin: false });
    let posted: unknown = null;
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [custom] })),
      http.get(`${API}/instrumentation/modules`, () => json({ modules: [] })),
      http.post(`${API}/instrumentation/profiles`, async ({ request }) => {
        posted = await request.json();
        return json({ ...custom, id: "prof_new", name: "Reboot profile" });
      }),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Profiles" }));
    await user.click(await screen.findByRole("button", { name: "New profile" }));

    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Reboot profile");
    const action = within(dialog).getByRole("combobox", { name: "Action" });
    await user.selectOptions(action, "reboot");

    const timeout = within(dialog).getByRole("textbox", { name: "Boot timeout (seconds)" });
    await user.type(timeout, "0");
    await user.click(within(dialog).getByRole("button", { name: "Create profile" }));
    expect(
      await within(dialog).findByText(/Boot timeout must be a whole number of seconds/),
    ).toBeInTheDocument();
    expect(posted).toBeNull();

    await user.clear(timeout);
    await user.type(timeout, "120");
    await user.click(within(dialog).getByRole("button", { name: "Create profile" }));
    await waitFor(() => expect(posted).not.toBeNull());
    const body = posted as {
      steps: Array<{ action: string; params: Record<string, unknown> }>;
    };
    expect(body.steps[0]?.action).toBe("reboot");
    expect(body.steps[0]?.params).toEqual({ timeout_s: 120 });
  });
});

describe("toolchain acquisition", () => {
  it("renders a present but unverified artifact as failed verification, never as verified", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [] })),
      http.get(`${API}/instrumentation/toolchain`, () =>
        json(
          toolchainView([
            pinnedArtifact("magisk", {
              present: true,
              verified: false,
              path: "/data/instrumentation/toolchain/magisk/26.1/Magisk-v26.1.apk",
              size_bytes: 11_411_692,
              sha256: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
              acquired_at: "2026-11-27T20:00:00Z",
            }),
            pinnedArtifact("lsposed"),
          ]),
        ),
      ),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Root & Magisk" }));

    expect(await screen.findByText("downloaded, FAILED verification")).toBeInTheDocument();
    expect(screen.queryByText("downloaded + verified")).not.toBeInTheDocument();
    expect(screen.getByText("not downloaded")).toBeInTheDocument();

    // Pinned metadata is shown truthfully: exact size and a truncated hash whose
    // title carries the full pinned value.
    expect(screen.getByText("11,411,692 bytes (10.9 MiB)")).toHaveAttribute(
      "title",
      "Pinned size: 11,411,692 bytes",
    );
    expect(screen.getByText("ae1a02b1ab60…")).toHaveAttribute("title", MAGISK_SHA);
    expect(screen.getByText("0ebc6bcb465d…")).toHaveAttribute("title", LSPOSED_SHA);
    expect(screen.getByText("min API 27")).toBeInTheDocument();
    expect(screen.getAllByText("module ZIP").length).toBeGreaterThan(0);
  });

  it("requires the confirm dialog and sends {confirm:true} only on confirmation", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    let acquired = false;
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [] })),
      http.get(`${API}/instrumentation/toolchain`, () => {
        const status = pinnedArtifact("magisk", {
          present: acquired,
          verified: acquired,
          path: acquired ? "/data/instrumentation/toolchain/magisk/26.1/Magisk-v26.1.apk" : "",
          size_bytes: acquired ? 11_411_692 : 0,
          sha256: acquired ? MAGISK_SHA : "",
          acquired_at: acquired ? "2026-11-27T20:00:00Z" : "",
        });
        return json(toolchainView([status]));
      }),
      http.post(`${API}/instrumentation/toolchain/magisk/acquire`, async ({ request }) => {
        bodies.push(await request.json());
        acquired = true;
        return json({
          artifact: pinnedArtifact("magisk", {
            present: true,
            verified: true,
            path: "/data/instrumentation/toolchain/magisk/26.1/Magisk-v26.1.apk",
            size_bytes: 11_411_692,
            sha256: MAGISK_SHA,
            acquired_at: "2026-11-27T20:00:00Z",
          }),
          module_id: "",
        });
      }),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Root & Magisk" }));

    expect(await screen.findByText("not downloaded")).toBeInTheDocument();
    expect(bodies).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Acquire Magisk" }));
    const dialog = await screen.findByRole("dialog", { name: /Acquire Magisk 26\.1 \(26100\)\?/ });

    // The dialog body states version, exact byte size, official source, licence
    // and that acquisition is not installation.
    expect(within(dialog).getByText(/26\.1 \(26100\) · APK/)).toBeInTheDocument();
    expect(within(dialog).getByText("11,411,692 bytes")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: MAGISK_URL })).toHaveAttribute(
      "href",
      MAGISK_URL,
    );
    expect(within(dialog).getByText("GPL-3.0")).toBeInTheDocument();
    expect(within(dialog).getByText("Acquiring is not installation.")).toBeInTheDocument();

    // Opening the dialog must not send anything.
    expect(bodies).toHaveLength(0);
    expect(screen.queryByText("Acquired Magisk")).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Confirm acquisition" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ confirm: true });

    expect(await screen.findByText("Acquired Magisk")).toBeInTheDocument();
    expect(await screen.findByText("downloaded + verified")).toBeInTheDocument();
    expect(screen.queryByText("downloaded, FAILED verification")).not.toBeInTheDocument();
  });

  it("surfaces the 409 consent message, retries nothing and shows no success toast", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [] })),
      http.get(`${API}/instrumentation/toolchain`, () =>
        json(toolchainView([pinnedArtifact("magisk")])),
      ),
      http.post(`${API}/instrumentation/toolchain/magisk/acquire`, () => {
        calls += 1;
        return errorEnvelope(
          409,
          "conflict",
          "acquiring Magisk requires explicit confirmation",
          "repeat the request with confirm=true after reviewing the pinned version (26.1 (26100)) and its licence (GPL-3.0)",
        );
      }),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "Root & Magisk" }));

    await user.click(await screen.findByRole("button", { name: "Acquire Magisk" }));
    const dialog = await screen.findByRole("dialog", { name: /Acquire Magisk/ });
    await user.click(within(dialog).getByRole("button", { name: "Confirm acquisition" }));

    expect(await screen.findByText("Confirmation required for Magisk")).toBeInTheDocument();
    expect(
      (await screen.findAllByText(/acquiring Magisk requires explicit confirmation/)).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("Acquired Magisk")).not.toBeInTheDocument();
    expect(calls).toBe(1);
  });

  it("reports a discarded download on 422 and never claims a successful acquisition", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [] })),
      http.get(`${API}/instrumentation/toolchain`, () =>
        json(toolchainView([pinnedArtifact("lsposed")])),
      ),
      http.post(`${API}/instrumentation/toolchain/lsposed/acquire`, () =>
        errorEnvelope(
          422,
          "validation_error",
          "LSPosed failed integrity verification",
          `expected sha256 ${LSPOSED_SHA}, got deadbeef; the download was discarded and nothing was installed`,
        ),
      ),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "LSPosed & Zygisk" }));

    await user.click(await screen.findByRole("button", { name: "Acquire LSPosed" }));
    const dialog = await screen.findByRole("dialog", { name: /Acquire LSPosed/ });
    await user.click(within(dialog).getByRole("button", { name: "Confirm acquisition" }));

    expect(
      await screen.findByText("LSPosed was not acquired — the download was discarded"),
    ).toBeInTheDocument();
    expect(
      (await screen.findAllByText(/nothing was installed/i)).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("Acquired LSPosed")).not.toBeInTheDocument();
    expect(screen.queryByText(/imported as mod_/)).not.toBeInTheDocument();
  });

  it("imports an acquired LSPosed ZIP and reports the returned module id", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API}/instrumentation/profiles`, () => json({ profiles: [] })),
      http.get(`${API}/instrumentation/toolchain`, () =>
        json(toolchainView([pinnedArtifact("lsposed")])),
      ),
      http.get(`${API}/instrumentation/modules`, () => json({ modules: [moduleRecord()] })),
      http.post(`${API}/instrumentation/toolchain/lsposed/acquire`, () =>
        json({
          artifact: pinnedArtifact("lsposed", {
            present: true,
            verified: true,
            path: "/data/instrumentation/toolchain/lsposed/1.9.2/LSPosed-v1.9.2-7024-zygisk-release.zip",
            size_bytes: 2_462_055,
            sha256: LSPOSED_SHA,
            acquired_at: "2026-11-27T20:00:00Z",
          }),
          module_id: "mod_lsposed",
        }),
      ),
    );

    renderApp({ route: "/instrumentation" });
    await screen.findByRole("heading", { level: 1, name: "Root & Instrumentation" });
    await user.click(screen.getByRole("tab", { name: "LSPosed & Zygisk" }));

    await user.click(await screen.findByRole("button", { name: "Acquire LSPosed" }));
    const dialog = await screen.findByRole("dialog", { name: /Acquire LSPosed/ });
    await user.click(within(dialog).getByRole("button", { name: "Confirm acquisition" }));

    expect(await screen.findByText("Acquired LSPosed")).toBeInTheDocument();
    expect(await screen.findByText("Module imported")).toBeInTheDocument();
    expect(await screen.findByText(/imported as mod_lsposed/)).toBeInTheDocument();

    // The imported module is resolvable from the library the profiles use.
    await user.click(screen.getByRole("tab", { name: "Module library" }));
    expect(await screen.findByText("1 of 1 modules")).toBeInTheDocument();
    expect(screen.getByText("zygisk_lsposed")).toBeInTheDocument();
  });
});
