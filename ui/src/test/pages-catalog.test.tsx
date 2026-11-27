import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

import { renderApp } from "./utils";

/**
 * Page-level coverage for the U3/U4 catalogue pages (Images, Templates,
 * Proxies) against the MSW mock daemon. Every assertion goes through the real
 * typed API client — no component internals are mocked.
 *
 * jsdom + MSW is comparatively slow on a busy host, so waits use explicit
 * generous timeouts and each test carries its own budget.
 */
const SLOW = { timeout: 60_000 };
const TEST_TIMEOUT = 180_000;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function findText(text: string | RegExp) {
  return screen.findByText(
    text instanceof RegExp ? text : new RegExp(`^${escapeRegExp(text)}$`),
    undefined,
    SLOW,
  );
}

async function waitForToast(pattern: RegExp): Promise<void> {
  await waitFor(() => {
    const texts = screen.queryAllByTestId("toast").map((toast) => toast.textContent ?? "");
    expect(texts.some((text) => pattern.test(text))).toBe(true);
  }, SLOW);
}

async function openImages(): Promise<void> {
  renderApp({ route: "/images" });
  await findText("Android 16 (API 36) · Google APIs");
}

describe("catalogue pages", () => {
  it(
    "Images: filters by tag, installed-state and API level and exposes OOBE evidence",
    async () => {
      const user = userEvent.setup();
      await openImages();
      expect(await findText("Android 14 (API 34) · AOSP")).toBeInTheDocument();

      // Tag filter narrows the catalogue to AOSP (the only non-installed image).
      await user.selectOptions(screen.getByLabelText("Tag"), "aosp");
      expect(await findText("Android 14 (API 34) · AOSP")).toBeInTheDocument();
      expect(screen.queryByText("Android 16 (API 36) · Google APIs")).not.toBeInTheDocument();

      // Installed-state filter: nothing in the AOSP-only view is installed.
      await user.selectOptions(screen.getByLabelText("Installed"), "installed");
      await waitFor(
        () => expect(screen.queryByText("Android 14 (API 34) · AOSP")).not.toBeInTheDocument(),
        SLOW,
      );

      // API-level filter over the "not installed" slice.
      await user.selectOptions(screen.getByLabelText("Installed"), "available");
      await user.selectOptions(screen.getByLabelText("Tag"), "all");
      expect(await findText("Android 14 (API 34) · AOSP")).toBeInTheDocument();
      await user.selectOptions(screen.getByLabelText("API level"), "36");
      await waitFor(
        () => expect(screen.queryByText("Android 14 (API 34) · AOSP")).not.toBeInTheDocument(),
        SLOW,
      );
      await user.selectOptions(screen.getByLabelText("API level"), "all");
      await user.selectOptions(screen.getByLabelText("Installed"), "all");

      // OOBE evidence surfaces as a tooltip on the status pill.
      const renderedImage = await findText("Android 16 (API 36) · Google APIs");
      const row = renderedImage.closest("tr");
      expect(row).not.toBeNull();
      expect(within(row as HTMLElement).getByTitle(/Evidence screenshot\.png/)).toBeInTheDocument();
      expect(
        within(row as HTMLElement).getByTitle(/Activity com\.google\.android\.setupwizard/),
      ).toBeInTheDocument();

      // The matrix panel expands to the recorded activity/screenshot/timings.
      await user.click(screen.getByRole("button", { name: "Show details" }));
      expect(
        (
          await screen.findAllByText(
            "com.google.android.setupwizard/.SetupWizardActivity",
            undefined,
            SLOW,
          )
        ).length,
      ).toBeGreaterThan(0);
      expect(await findText("research/oobe/api36-google_apis.png")).toBeInTheDocument();
      expect(await findText("34s")).toBeInTheDocument();
      expect(screen.getAllByText("6s").length).toBeGreaterThan(0);
    },
    TEST_TIMEOUT,
  );

  it(
    "Images: dry-run preview gates the download, then installs and refreshes",
    async () => {
      const user = userEvent.setup();
      const downloadSpy = vi.spyOn(api, "downloadImage");
      renderApp({ route: "/images" });

      await findText("Android 14 (API 34) · AOSP");
      await user.click(screen.getByRole("button", { name: "Download" }));

      const dialog = await screen.findByRole("dialog", { name: /Install system image/i }, SLOW);
      expect(within(dialog).getByText("Dry-run preview")).toBeInTheDocument();
      expect(within(dialog).getByText(/system-images;android-34;aosp;x86_64/)).toBeInTheDocument();
      expect(downloadSpy.mock.calls[0]?.[0]).toBe("api34-aosp-x86_64");
      expect(downloadSpy.mock.calls[0]?.[1]).toEqual({ dry_run: true });

      await user.click(within(dialog).getByRole("button", { name: "Install image" }));

      await waitForToast(/Image installed/i);
      expect(downloadSpy.mock.calls[1]?.[0]).toBe("api34-aosp-x86_64");
      expect(downloadSpy.mock.calls[1]?.[1]).toEqual({ dry_run: false });

      // The catalogue refetches, so the image now shows as installed.
      await waitFor(
        () => expect(screen.queryByRole("button", { name: "Download" })).not.toBeInTheDocument(),
        SLOW,
      );
      const renderedImage = screen.getByText("Android 14 (API 34) · AOSP");
      const row = renderedImage.closest("tr");
      expect(within(row as HTMLElement).getByText("Installed")).toBeInTheDocument();
    },
    TEST_TIMEOUT,
  );

  it(
    "Images: custom import validates the host path, then registers the image",
    async () => {
      const user = userEvent.setup();
      await openImages();

      await user.click(screen.getByRole("button", { name: "Import image" }));
      const dialog = await screen.findByRole("dialog", { name: /Import custom image/i }, SLOW);
      const importButton = within(dialog).getByRole("button", { name: "Import image" });

      await user.click(importButton);
      expect(
        await within(dialog).findByText("Host directory path is required.", undefined, SLOW),
      ).toBeInTheDocument();
      const pathInput = await within(dialog).findByLabelText(
        /Host directory path/,
        undefined,
        SLOW,
      );
      expect(pathInput).toHaveAttribute("aria-invalid", "true");

      await user.type(pathInput, "D:\\android\\system-images\\custom-x86_64");
      await user.type(
        await within(dialog).findByLabelText(/Display name/, undefined, SLOW),
        "Custom test image",
      );
      await user.click(importButton);

      await waitForToast(/Custom image imported/i);
      expect(await findText("Custom test image")).toBeInTheDocument();
      expect(
        screen.queryByRole("dialog", { name: /Import custom image/i }),
      ).not.toBeInTheDocument();
    },
    TEST_TIMEOUT,
  );

  it(
    "Templates: validates resources and creates a new template",
    async () => {
      const user = userEvent.setup();
      renderApp({ route: "/templates" });
      await findText("Pixel 7 · Play (API 35)");

      await user.click(screen.getByRole("button", { name: "New template" }));
      const dialog = await screen.findByRole("dialog", { name: /New template/i }, SLOW);

      await user.type(
        await within(dialog).findByRole("textbox", { name: /Template ID/ }, SLOW),
        "tpl-test-1",
      );
      await user.type(
        await within(dialog).findByRole("textbox", { name: /Template name/ }, SLOW),
        "Integration test template",
      );

      const ramInput = await within(dialog).findByRole("spinbutton", { name: /RAM/ }, SLOW);
      await user.clear(ramInput);
      await user.type(ramInput, "128");
      await user.selectOptions(
        await within(dialog).findByRole("combobox", { name: /System image/ }, SLOW),
        "api36-google_apis-x86_64",
      );
      await user.click(within(dialog).getByRole("button", { name: "Create template" }));
      expect(
        await within(dialog).findByText("RAM must be between 512 and 16384.", undefined, SLOW),
      ).toBeInTheDocument();

      await user.clear(ramInput);
      await user.type(ramInput, "4096");
      await user.click(within(dialog).getByRole("button", { name: "Create template" }));

      await waitForToast(/Template created/i);
      expect(await findText("Integration test template")).toBeInTheDocument();
      expect(screen.queryByRole("dialog", { name: /New template/i })).not.toBeInTheDocument();
    },
    TEST_TIMEOUT,
  );

  it(
    "Proxies: bulk import reports counts/errors and never renders passwords",
    async () => {
      const user = userEvent.setup();
      renderApp({ route: "/proxies" });
      await findText("EU egress 01");
      expect(screen.getAllByText("Password in vault").length).toBeGreaterThan(0);

      await user.click(screen.getByRole("button", { name: "Bulk import" }));
      const dialog = await screen.findByRole("dialog", { name: /Bulk import proxies/i }, SLOW);
      const textarea = await within(dialog).findByLabelText(/Proxy URLs/, undefined, SLOW);
      fireEvent.change(textarea, {
        target: {
          value:
            "http://bulk-user:sup3r-secret@bulk-proxy.example.net:8000\n" +
            "ftp://bulk-user:sup3r-secret@bad.example.net:21",
        },
      });
      await user.click(within(dialog).getByRole("button", { name: "Import proxies" }));

      expect(await within(dialog).findByText("1 added", undefined, SLOW)).toBeInTheDocument();
      const errorLine = await within(dialog).findByText(/unsupported scheme/i, undefined, SLOW);
      // The API can echo the offending line back; the rendered error must redact it.
      expect(errorLine).toHaveTextContent("ftp://***:***@");
      expect(errorLine.textContent).not.toContain("sup3r-secret");

      await user.click(within(dialog).getByRole("button", { name: "Done" }));
      const table = await screen.findByRole("table", { name: "Proxy pool" }, SLOW);
      expect(
        await within(table).findByText("bulk-proxy.example.net:8000", undefined, SLOW),
      ).toBeInTheDocument();
      expect(within(table).getByText("bulk-user")).toBeInTheDocument();
      // Once the import textarea is unmounted no password survives in the DOM.
      expect(document.body.textContent).not.toContain("sup3r-secret");
    },
    TEST_TIMEOUT,
  );

  it(
    "Proxies: checks the whole pool and records health updates",
    async () => {
      const user = userEvent.setup();
      renderApp({ route: "/proxies" });
      await findText("EU egress 01");

      await user.click(screen.getByRole("button", { name: "Check all" }));
      await waitForToast(/Checked 5 proxies/i);

      const table = await screen.findByRole("table", { name: "Proxy pool" }, SLOW);
      await waitFor(
        () => expect(within(table).getAllByText("Failed").length).toBeGreaterThan(0),
        SLOW,
      );
      expect(within(table).getAllByText("Healthy").length).toBeGreaterThan(0);
    },
    TEST_TIMEOUT,
  );

  it(
    "Proxies: assigns a proxy per instance and verifies guest egress",
    async () => {
      const user = userEvent.setup();
      renderApp({ route: "/proxies" });
      await findText("EU egress 01");

      const instanceSelect = await screen.findByLabelText("Proxy for avd-tablet-01", undefined, SLOW);
      await user.selectOptions(instanceSelect, "px-http-1");
      await waitForToast(/Proxy assigned to avd-tablet-01/i);

      await user.click(screen.getByRole("button", { name: "Auto-assign round robin" }));
      await waitForToast(/Round-robin assigned/i);

      await user.click(screen.getByRole("button", { name: "Verify egress for avd-pixel7-01" }));
      const dialog = await screen.findByRole("dialog", { name: /Verify guest egress/i }, SLOW);
      await user.type(
        await within(dialog).findByLabelText("Token URL", undefined, SLOW),
        "https://api.ipify.org?format=json",
      );
      await user.click(within(dialog).getByRole("button", { name: "Verify egress" }));

      expect(await within(dialog).findByText("203.0.113.77", undefined, SLOW)).toBeInTheDocument();
      expect(within(dialog).getByText("Guest egress confirmed")).toBeInTheDocument();
      expect(document.body.textContent).not.toContain("sup3r-secret");
    },
    TEST_TIMEOUT,
  );
});
