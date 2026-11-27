import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  Badge,
  Button,
  ConfirmDialog,
  Field,
  Input,
  Modal,
  ProgressBar,
  StatusPill,
  Tabs,
  ToastProvider,
  useToast,
} from "@/lib/components";

describe("StatusPill", () => {
  it.each([
    ["running", "ds-status--success"],
    ["succeeded", "ds-status--success"],
    ["verified", "ds-status--success"],
    ["booting", "ds-status--warning"],
    ["failed", "ds-status--danger"],
    ["offline", "ds-status--danger"],
    ["stopped", "ds-status--neutral"],
  ])("maps %s to %s", (status, expectedClass) => {
    const { container } = render(<StatusPill status={status} />);
    expect(container.firstElementChild).toHaveClass(expectedClass);
  });

  it("uses a friendly label and supports an explicit tone override", () => {
    render(<StatusPill status="custom_state" tone="accent" label="Custom" pulse />);
    const pill = screen.getByText("Custom").closest(".ds-status");
    expect(pill).toHaveClass("ds-status--accent");
    expect(pill).toHaveClass("ds-status--pulse");
  });
});

describe("buttons and badges", () => {
  it("exposes the loading state to assistive technology", () => {
    render(
      <Button variant="primary" loading>
        Deploy
      </Button>,
    );
    const button = screen.getByRole("button", { name: /deploy/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
  });

  it("renders badges with semantic tone classes", () => {
    const { container } = render(<Badge tone="warning">Needs attention</Badge>);
    expect(container.firstElementChild).toHaveClass("ds-badge--warning");
  });
});

describe("form fields", () => {
  it("associates label, hint and error with the control", () => {
    render(
      <Field label="Proxy URL" hint="scheme://user:pass@host:port" error="Not a valid URL">
        <Input />
      </Field>,
    );
    const input = screen.getByLabelText("Proxy URL");
    expect(input).toHaveAttribute("aria-invalid", "true");
    const describedBy = input.getAttribute("aria-describedby") ?? "";
    expect(describedBy).not.toBe("");
    expect(screen.getByText("Not a valid URL")).toHaveAttribute("role", "alert");
  });
});

describe("Modal", () => {
  it("closes on Escape and renders into a portal", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Modal open onClose={onClose} title="Delete instance">
        <p>This cannot be undone.</p>
      </Modal>,
    );
    expect(screen.getByRole("dialog", { name: "Delete instance" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("runs the async confirm and closes through the parent", async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    const onCancel = vi.fn();
    const user = userEvent.setup();
    render(
      <ConfirmDialog
        open
        title="Remove proxy?"
        description="Instances using it will lose egress."
        confirmLabel="Remove proxy"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Remove proxy" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});

describe("Tabs", () => {
  it("supports keyboard navigation", async () => {
    const user = userEvent.setup();
    render(
      <Tabs
        ariaLabel="Instance sections"
        items={[
          { id: "overview", label: "Overview", content: <p>Overview panel</p> },
          { id: "logs", label: "Logs", content: <p>Logs panel</p> },
        ]}
      />,
    );
    const overview = screen.getByRole("tab", { name: "Overview" });
    expect(overview).toHaveAttribute("aria-selected", "true");
    overview.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Logs" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Logs panel")).toBeVisible();
  });
});

describe("ProgressBar", () => {
  it("exposes determinate values", () => {
    render(<ProgressBar value={25} max={50} label="Deploying" showValue />);
    const bar = screen.getByRole("progressbar", { name: "Deploying" });
    expect(bar).toHaveAttribute("aria-valuenow", "25");
    expect(screen.getByText("50%")).toBeInTheDocument();
  });
});

describe("Toasts", () => {
  function Harness() {
    const toast = useToast();
    return (
      <button type="button" onClick={() => toast.success("Deployed", "5 instances created")}>
        Deploy now
      </button>
    );
  }

  it("announces notifications and allows dismissal", async () => {
    const user = userEvent.setup();
    render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Deploy now" }));
    expect(await screen.findByText("Deployed")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("5 instances created");
    await user.click(screen.getByRole("button", { name: /dismiss notification: deployed/i }));
    expect(screen.queryByText("Deployed")).not.toBeInTheDocument();
  });
});
