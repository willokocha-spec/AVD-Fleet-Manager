import type { CSSProperties } from "react";

import {
  Badge,
  Button,
  ButtonLink,
  Card,
  Checkbox,
  EmptyState,
  Field,
  InlineNotice,
  Input,
  Select,
  Skeleton,
  StatusPill,
  Table,
} from "@/lib/components";
import type {
  AvdInfo,
  CapacityInfo,
  DeviceProfile,
  OobeRecord,
  ProxyView,
  ScriptDefinition,
  SystemImage,
  Template,
} from "@/lib/types";
import { formatBytes, pluralize } from "@/lib/utils";

import {
  InstanceStatePill,
  ProxyBadge,
  canStart,
  describeError,
  portEntries,
} from "./fleet-utils";
import {
  GPU_OPTIONS,
  type CapacityAssessment,
  type CustomTemplateDraft,
  type DeployDraft,
  type TemplateMode,
  imageDisplayName,
  imageSummary,
  oobeStatusFor,
  oobeStatusLabel,
  resourceSummary,
  scriptSummary,
  validateCustomTemplate,
} from "./deploy-model";

function optionStyle(active: boolean, disabled = false): CSSProperties {
  return {
    display: "flex",
    flexDirection: "column",
    gap: "var(--space-2)",
    textAlign: "left",
    width: "100%",
    padding: "var(--space-4)",
    border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
    borderRadius: "var(--radius-md)",
    background: active ? "var(--accent-soft)" : "var(--surface-sunken)",
    color: "var(--text)",
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.65 : 1,
  };
}

function ErrorList({ messages }: { messages: readonly string[] }) {
  return (
    <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
      {messages.map((message) => (
        <li key={message}>{message}</li>
      ))}
    </ul>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 1 — image                                                              */
/* -------------------------------------------------------------------------- */

export interface DeployImageStepProps {
  images: readonly SystemImage[] | null;
  oobeRecords: readonly OobeRecord[];
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
  selectedImageId: string | null;
  onSelect: (imageId: string) => void;
}

export function DeployImageStep({
  images,
  oobeRecords,
  loading,
  error,
  onRetry,
  selectedImageId,
  onSelect,
}: DeployImageStepProps) {
  const selected = images?.find((image) => image.id === selectedImageId) ?? null;
  const selectedStatus = selected ? oobeStatusFor(selected, oobeRecords) : null;

  return (
    <Card
      title="System image"
      description="Only installed images can be deployed. Images verified through the OOBE first-boot check are strongly preferred."
      icon="images"
    >
      {loading && !images ? <Skeleton lines={5} /> : null}

      {error ? (
        <InlineNotice
          tone="danger"
          title="Could not load system images"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={onRetry}>
              Retry
            </Button>
          }
        >
          <p>{describeError(error)}</p>
        </InlineNotice>
      ) : null}

      {images && images.length === 0 ? (
        <EmptyState
          icon="images"
          title="No installed system images"
          description="Download or import a system image before deploying a fleet. The Images page lists both installed and remote images."
          action={
            <ButtonLink to="/images" variant="primary" icon="images">
              Open Images
            </ButtonLink>
          }
        />
      ) : null}

      {images && images.length > 0 ? (
        <div
          role="group"
          aria-label="Installed system images"
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
            gap: "var(--space-3)",
          }}
        >
          {images.map((image) => {
            const status = oobeStatusFor(image, oobeRecords);
            const active = image.id === selectedImageId;
            return (
              <button
                key={image.id}
                type="button"
                aria-pressed={active}
                onClick={() => onSelect(image.id)}
                style={optionStyle(active)}
              >
                <span style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontWeight: 620 }}>{imageDisplayName(image)}</span>
                  <StatusPill status={status} label={oobeStatusLabel(status)} />
                </span>
                <span className="muted small">{imageSummary(image)}</span>
                <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  <Badge tone="success" icon="check-circle">
                    Installed
                  </Badge>
                  {image.tag ? <Badge tone="neutral">{image.tag}</Badge> : null}
                  {image.kind ? <Badge tone="info">{image.kind}</Badge> : null}
                </span>
                {image.notes ? <span className="muted small">{image.notes}</span> : null}
              </button>
            );
          })}
        </div>
      ) : null}

      {selected && selectedStatus === "verified" ? (
        <div style={{ marginTop: "var(--space-3)" }}>
          <InlineNotice tone="success" title="OOBE verified on this host">
            <p>
              {imageDisplayName(selected)} reached the Setup Wizard on a previous first boot. Reset
              strategies and golden snapshots work best with this image.
            </p>
          </InlineNotice>
        </div>
      ) : null}

      {selected && selectedStatus && selectedStatus !== "verified" ? (
        <div style={{ marginTop: "var(--space-3)" }}>
          <InlineNotice
            tone={selectedStatus === "failed" ? "danger" : "warning"}
            title={selectedStatus === "failed" ? "OOBE verification failed" : "OOBE not verified yet"}
          >
            <p>
              {selectedStatus === "failed"
                ? "The last verification of this image did not reach the Setup Wizard. Deployed instances may first boot into an unknown state."
                : selectedStatus === "unsupported"
                  ? "The daemon marks OOBE verification as unsupported for this image. First-boot behaviour is unknown."
                  : "This image has not been verified through a first boot on this host. Deployed instances may need manual OOBE handling."}{" "}
              You can continue, but verify the first instance before scaling up.
            </p>
          </InlineNotice>
        </div>
      ) : null}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 2 — template                                                           */
/* -------------------------------------------------------------------------- */

export interface DeployTemplateStepProps {
  image: SystemImage;
  templates: readonly Template[] | null;
  templatesLoading: boolean;
  templatesError: Error | null;
  onRetryTemplates: () => void;
  mode: TemplateMode;
  onModeChange: (mode: TemplateMode) => void;
  templateId: string | null;
  onSelectTemplate: (templateId: string) => void;
  custom: CustomTemplateDraft;
  onCustomChange: (patch: Partial<CustomTemplateDraft>) => void;
  devices: readonly DeviceProfile[] | null;
  proxies: readonly ProxyView[] | null;
  scripts: readonly ScriptDefinition[] | null;
  lookupsLoading: boolean;
  lookupError: Error | null;
  onRetryLookups: () => void;
  submitted: boolean;
}

export function DeployTemplateStep({
  image,
  templates,
  templatesLoading,
  templatesError,
  onRetryTemplates,
  mode,
  onModeChange,
  templateId,
  onSelectTemplate,
  custom,
  onCustomChange,
  devices,
  proxies,
  scripts,
  lookupsLoading,
  lookupError,
  onRetryLookups,
  submitted,
}: DeployTemplateStepProps) {
  const matching = (templates ?? []).filter((template) => template.image_id === image.id);
  const customErrors = validateCustomTemplate(custom);
  const showCustomErrors = submitted && customErrors.length > 0;

  const deviceOptions = (devices ?? []).map((device) => ({
    value: device.id,
    label: `${device.name}${device.resolution ? ` · ${device.resolution}` : ""}${
      device.ram_mb ? ` · ${formatBytes(device.ram_mb * 1024 * 1024)}` : ""
    }`,
  }));
  const proxyOptions = (proxies ?? [])
    .filter((proxy) => proxy.enabled !== false)
    .map((proxy) => ({
      value: proxy.id,
      label: `${proxy.name?.trim() || proxy.host} · ${proxy.scheme ?? "http"}://${proxy.host}:${proxy.port}`,
    }));
  const scriptOptions = scripts ?? [];

  const changeDevice = (deviceId: string): void => {
    const device = (devices ?? []).find((candidate) => candidate.id === deviceId);
    onCustomChange({
      deviceId,
      ...(device
        ? {
            ramMb: device.ram_mb ?? custom.ramMb,
            name: custom.name.trim() ? custom.name : `${device.name} profile`,
          }
        : {}),
    });
  };

  return (
    <Card
      title="Device template"
      description="A template packages the device profile, resources and post-boot scripts used by every instance."
      icon="templates"
    >
      <div style={{ display: "flex", gap: "var(--space-2)", marginBottom: "var(--space-4)", flexWrap: "wrap" }}>
        <Button
          variant={mode === "existing" ? "secondary" : "ghost"}
          aria-pressed={mode === "existing"}
          icon="templates"
          onClick={() => onModeChange("existing")}
        >
          Use an existing template
        </Button>
        <Button
          variant={mode === "custom" ? "secondary" : "ghost"}
          aria-pressed={mode === "custom"}
          icon="plus"
          onClick={() => onModeChange("custom")}
        >
          Create a new template
        </Button>
      </div>

      {mode === "existing" ? (
        <>
          {templatesLoading && !templates ? <Skeleton lines={4} /> : null}
          {templatesError ? (
            <InlineNotice
              tone="danger"
              title="Could not load templates"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={onRetryTemplates}>
                  Retry
                </Button>
              }
            >
              <p>{describeError(templatesError)}</p>
            </InlineNotice>
          ) : null}

          {!templatesLoading && templates && matching.length === 0 ? (
            <EmptyState
              compact
              icon="templates"
              title="No templates use this image"
              description="Create a custom template for this image, or pick another image with an existing template."
              action={
                <Button size="sm" variant="primary" icon="plus" onClick={() => onModeChange("custom")}>
                  Create a template
                </Button>
              }
            />
          ) : null}

          {matching.length > 0 ? (
            <div
              role="group"
              aria-label="Templates for the selected image"
              style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: "var(--space-3)" }}
            >
              {matching.map((template) => {
                const active = template.id === templateId;
                return (
                  <button
                    key={template.id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => onSelectTemplate(template.id)}
                    style={optionStyle(active)}
                  >
                    <span style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
                      <span style={{ fontWeight: 620 }}>{template.name}</span>
                      <code style={{ fontSize: "var(--fs-xs)" }}>{template.id}</code>
                    </span>
                    {template.description ? (
                      <span className="muted small">{template.description}</span>
                    ) : null}
                    <span className="small" style={{ color: "var(--text-secondary)" }}>
                      {resourceSummary(template)}
                    </span>
                    <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {template.device_id ? <Badge tone="neutral" icon="instance">{template.device_id}</Badge> : null}
                      {template.proxy_id ? (
                        <ProxyBadge proxyId={template.proxy_id} proxies={proxies} />
                      ) : (
                        <Badge tone="neutral" icon="proxies">Direct</Badge>
                      )}
                      {template.tags?.map((tag) => (
                        <Badge key={tag} tone="info">
                          {tag}
                        </Badge>
                      ))}
                    </span>
                  </button>
                );
              })}
            </div>
          ) : null}
        </>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
          {lookupError ? (
            <InlineNotice
              tone="warning"
              title="Some template options could not be loaded"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={onRetryLookups}>
                  Retry
                </Button>
              }
            >
              <p>{describeError(lookupError)}</p>
            </InlineNotice>
          ) : null}
          {showCustomErrors ? (
            <InlineNotice tone="danger" title="Fix the template before continuing">
              <ErrorList messages={customErrors} />
            </InlineNotice>
          ) : null}

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
              gap: "var(--space-4)",
            }}
          >
            <Field
              label="Template name"
              required
              hint="Shown in the Templates page and on every deployed instance."
            >
              <Input
                value={custom.name}
                onChange={(event) => onCustomChange({ name: event.target.value })}
              />
            </Field>

            <Field label="Device profile" required hint="Determines resolution and density.">
              <Select
                aria-label="Device profile"
                value={custom.deviceId}
                placeholder={lookupsLoading ? "Loading devices…" : "Choose a device…"}
                options={deviceOptions}
                disabled={lookupsLoading}
                onChange={(event) => changeDevice(event.target.value)}
              />
            </Field>

            <Field label="RAM (MB)" hint="Guest RAM. The host must have enough free memory.">
              <Input
                type="number"
                min={512}
                step={256}
                value={custom.ramMb}
                onChange={(event) => onCustomChange({ ramMb: Number(event.target.value) })}
              />
            </Field>

            <Field label="Heap (MB)" hint="dalvik.vm.heapgrowthlimit equivalent.">
              <Input
                type="number"
                min={32}
                step={32}
                value={custom.heapMb}
                onChange={(event) => onCustomChange({ heapMb: Number(event.target.value) })}
              />
            </Field>

            <Field label="CPU cores">
              <Input
                type="number"
                min={1}
                max={16}
                value={custom.cpuCores}
                onChange={(event) => onCustomChange({ cpuCores: Number(event.target.value) })}
              />
            </Field>

            <Field label="GPU mode">
              <Select
                aria-label="GPU mode"
                value={custom.gpuMode}
                options={GPU_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
                onChange={(event) => onCustomChange({ gpuMode: event.target.value as CustomTemplateDraft["gpuMode"] })}
              />
            </Field>

            <Field label="Proxy" hint="Optional egress proxy applied to every instance.">
              <Select
                aria-label="Template proxy"
                value={custom.proxyId ?? ""}
                options={[{ value: "", label: "No proxy (direct)" }, ...proxyOptions]}
                disabled={lookupsLoading}
                onChange={(event) => onCustomChange({ proxyId: event.target.value || null })}
              />
            </Field>
          </div>

          <Checkbox
            label="Headless"
            hint="Run without a visible emulator window (recommended on servers)."
            checked={custom.headless}
            onChange={(event) => onCustomChange({ headless: event.target.checked })}
          />

          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="ds-field__label" style={{ marginBottom: "var(--space-2)" }}>
              Post-boot scripts (optional)
            </legend>
            {lookupsLoading && !scripts ? <Skeleton lines={2} /> : null}
            {scriptOptions.length === 0 && !lookupsLoading ? (
              <p className="muted small" style={{ margin: 0 }}>
                No scripts are defined. Create scripts on the Scripts page to run them after boot.
              </p>
            ) : null}
            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
              {scriptOptions.map((script) => (
                <Checkbox
                  key={script.id}
                  label={`${script.name} · ${script.type}`}
                  hint={script.description}
                  checked={custom.scriptIds.includes(script.id)}
                  onChange={(event) =>
                    onCustomChange({
                      scriptIds: event.target.checked
                        ? [...custom.scriptIds, script.id]
                        : custom.scriptIds.filter((id) => id !== script.id),
                    })
                  }
                />
              ))}
            </div>
          </fieldset>

          <InlineNotice tone="info" title="Template will be created at deploy time">
            <p>
              The new template is saved to the daemon together with the deployment, so backing out
              of the wizard leaves no orphan templates behind.
            </p>
          </InlineNotice>
        </div>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 3 — fleet size / capacity                                              */
/* -------------------------------------------------------------------------- */

export interface DeployFleetStepProps {
  count: number;
  prefix: string;
  names: readonly string[];
  onCountChange: (count: number) => void;
  onPrefixChange: (prefix: string) => void;
  countError: string | null;
  prefixError: string | null;
  capacity: CapacityInfo | null;
  capacityLoading: boolean;
  capacityError: Error | null;
  onRefreshCapacity: () => void;
  assessment: CapacityAssessment;
  ramPerInstanceMb: number;
}

export function DeployFleetStep({
  count,
  prefix,
  names,
  onCountChange,
  onPrefixChange,
  countError,
  prefixError,
  capacity,
  capacityLoading,
  capacityError,
  onRefreshCapacity,
  assessment,
  ramPerInstanceMb,
}: DeployFleetStepProps) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
      <Card
        title="Fleet size & naming"
        description="Instances are named <prefix>-01, <prefix>-02… up to the requested count."
        icon="fleet"
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            gap: "var(--space-4)",
          }}
        >
          <Field label="Instance count" required error={countError ?? undefined} hint="Between 1 and 64 instances.">
            <Input
              type="number"
              min={1}
              max={64}
              step={1}
              value={count}
              invalid={countError ? true : undefined}
              onChange={(event) => onCountChange(Number(event.target.value))}
            />
          </Field>
          <Field
            label="Name prefix"
            required
            error={prefixError ?? undefined}
            hint="Letters, digits, dots, dashes and underscores."
          >
            <Input
              value={prefix}
              invalid={prefixError ? true : undefined}
              onChange={(event) => onPrefixChange(event.target.value)}
            />
          </Field>
        </div>

        <div style={{ marginTop: "var(--space-4)" }}>
          <p className="ds-field__label" style={{ marginBottom: "var(--space-2)" }}>
            Names preview
          </p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {names.slice(0, 12).map((name) => (
              <Badge key={name} tone="neutral">
                {name}
              </Badge>
            ))}
            {names.length > 12 ? (
              <span className="muted small">+{names.length - 12} more</span>
            ) : null}
            {names.length === 0 ? <span className="muted small">No names yet.</span> : null}
          </div>
        </div>
      </Card>

      <Card
        title="Capacity check"
        description="GET /capacity is read before deploying so the daemon is not asked to create more instances than it can hold."
        icon="server"
        actions={
          <Button
            size="sm"
            variant="secondary"
            icon="refresh"
            loading={capacityLoading}
            onClick={onRefreshCapacity}
          >
            Re-check capacity
          </Button>
        }
      >
        {capacityError ? (
          <InlineNotice tone="warning" title="Could not read capacity">
            <p>{describeError(capacityError)} The deployment can still be submitted, but it may fail.</p>
          </InlineNotice>
        ) : null}
        {capacity ? (
          <dl className="kv-list">
            <div className="kv-list__row">
              <dt>Daemon instance limit</dt>
              <dd>{capacity.max_instances ?? "—"}</dd>
            </div>
            <div className="kv-list__row">
              <dt>Already deployed</dt>
              <dd>{capacity.deployed ?? "—"}</dd>
            </div>
            <div className="kv-list__row">
              <dt>Projected after deploy</dt>
              <dd>
                {assessment.projectedInstances ?? "—"}
                {capacity.max_instances !== undefined ? ` / ${capacity.max_instances}` : ""}
              </dd>
            </div>
            <div className="kv-list__row">
              <dt>Running now</dt>
              <dd>{capacity.running ?? "—"}</dd>
            </div>
            <div className="kv-list__row">
              <dt>Can start more</dt>
              <dd>{capacity.can_start_more ?? "—"}</dd>
            </div>
            <div className="kv-list__row">
              <dt>RAM available</dt>
              <dd>
                {capacity.ram_available_mb !== undefined && capacity.ram_available_mb !== null
                  ? formatBytes(capacity.ram_available_mb * 1024 * 1024)
                  : "—"}
              </dd>
            </div>
            <div className="kv-list__row">
              <dt>Requested RAM</dt>
              <dd>
                {formatBytes(ramPerInstanceMb * count * 1024 * 1024)}
                <span className="muted small"> ({count} × {ramPerInstanceMb} MB)</span>
              </dd>
            </div>
            <div className="kv-list__row">
              <dt>CPU cores</dt>
              <dd>{capacity.cpu_cores ?? "—"}</dd>
            </div>
          </dl>
        ) : capacityLoading ? (
          <Skeleton lines={5} />
        ) : null}

        {assessment.blocking.length > 0 ? (
          <div style={{ marginTop: "var(--space-3)" }}>
            <InlineNotice tone="danger" title="Not enough capacity">
              <ErrorList messages={assessment.blocking} />
            </InlineNotice>
          </div>
        ) : null}

        {assessment.warnings.length > 0 ? (
          <div style={{ marginTop: "var(--space-3)" }}>
            <InlineNotice tone="warning" title="Capacity warnings">
              <ErrorList messages={assessment.warnings} />
            </InlineNotice>
          </div>
        ) : null}
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 4 — review                                                             */
/* -------------------------------------------------------------------------- */

export interface DeployReviewStepProps {
  image: SystemImage;
  oobeStatus: ReturnType<typeof oobeStatusFor>;
  draft: DeployDraft;
  template: Template | null;
  devices: readonly DeviceProfile[] | null;
  proxies: readonly ProxyView[] | null;
  scripts: readonly ScriptDefinition[] | null;
  names: readonly string[];
  assessment: CapacityAssessment;
  capacity: CapacityInfo | null;
  deployError: string | null;
}

export function DeployReviewStep({
  image,
  oobeStatus,
  draft,
  template,
  devices,
  proxies,
  scripts,
  names,
  assessment,
  capacity,
  deployError,
}: DeployReviewStepProps) {
  const customMode = draft.templateMode === "custom";
  const deviceId = template?.device_id ?? draft.custom.deviceId;
  const device = (devices ?? []).find((candidate) => candidate.id === deviceId);
  const templateName = customMode ? draft.custom.name : (template?.name ?? "—");
  const resources = customMode
    ? resourceSummary(draft.custom)
    : template
      ? resourceSummary(template)
      : "—";
  const proxyId = customMode ? draft.custom.proxyId : (template?.proxy_id ?? null);
  const scriptIds = customMode ? draft.custom.scriptIds : (template?.scripts ?? []);

  return (
    <Card title="Review the deployment" description="Nothing is created until you confirm." icon="deploy">
      {deployError ? (
        <InlineNotice tone="danger" title="Deployment failed">
          <p>{deployError}</p>
        </InlineNotice>
      ) : null}

      <dl className="kv-list">
        <div className="kv-list__row">
          <dt>Image</dt>
          <dd style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
            {imageDisplayName(image)}
            <StatusPill status={oobeStatus} label={oobeStatusLabel(oobeStatus)} />
          </dd>
        </div>
        <div className="kv-list__row">
          <dt>Template</dt>
          <dd>
            {templateName}
            {customMode ? <span className="muted small"> · created at deploy time</span> : null}
          </dd>
        </div>
        <div className="kv-list__row">
          <dt>Device</dt>
          <dd>{device ? `${device.name}${device.resolution ? ` · ${device.resolution}` : ""}` : deviceId || "—"}</dd>
        </div>
        <div className="kv-list__row">
          <dt>Resources</dt>
          <dd>{resources}</dd>
        </div>
        <div className="kv-list__row">
          <dt>Proxy</dt>
          <dd>
            {proxyId ? (
              <ProxyBadge proxyId={proxyId} proxies={proxies} />
            ) : (
              <span className="muted">Direct (no proxy)</span>
            )}
          </dd>
        </div>
        <div className="kv-list__row">
          <dt>Scripts</dt>
          <dd>{scriptSummary(scriptIds, scripts ?? [])}</dd>
        </div>
        <div className="kv-list__row">
          <dt>Instances</dt>
          <dd>{pluralize(draft.count, "instance")}</dd>
        </div>
        <div className="kv-list__row">
          <dt>Capacity</dt>
          <dd>
            {capacity
              ? `${assessment.projectedInstances ?? "?"} / ${capacity.max_instances ?? "?"} after deploy`
              : "Not checked"}
          </dd>
        </div>
      </dl>

      <div style={{ marginTop: "var(--space-4)" }}>
        <p className="ds-field__label" style={{ marginBottom: "var(--space-2)" }}>
          Instance names
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {names.slice(0, 16).map((name) => (
            <Badge key={name} tone="info">
              {name}
            </Badge>
          ))}
          {names.length > 16 ? (
            <span className="muted small">+{names.length - 16} more</span>
          ) : null}
        </div>
      </div>

      {assessment.blocking.length > 0 ? (
        <div style={{ marginTop: "var(--space-4)" }}>
          <InlineNotice tone="danger" title="Not enough capacity">
            <ErrorList messages={assessment.blocking} />
          </InlineNotice>
        </div>
      ) : null}
      {assessment.warnings.length > 0 ? (
        <div style={{ marginTop: "var(--space-4)" }}>
          <InlineNotice tone="warning" title="Capacity warnings">
            <ErrorList messages={assessment.warnings} />
          </InlineNotice>
        </div>
      ) : null}

      <div style={{ marginTop: "var(--space-4)" }}>
        <InlineNotice tone="info" title="No dry-run for bulk deploy">
          <p>
            The daemon API has no dry-run mode for <code>POST /instances/bulk</code>. This review is
            the preview — confirming creates the instances immediately.
          </p>
        </InlineNotice>
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Result                                                                      */
/* -------------------------------------------------------------------------- */

export interface DeployResultPanelProps {
  created: readonly AvdInfo[];
  deploying: boolean;
  starting: boolean;
  startError: string | null;
  onStartAll: () => void;
  onDeployAnother: () => void;
}

export function DeployResultPanel({
  created,
  deploying,
  starting,
  startError,
  onStartAll,
  onDeployAnother,
}: DeployResultPanelProps) {
  const startable = created.filter((instance) => canStart(instance.state));
  return (
    <Card
      title={deploying ? "Deploying…" : `Deployed ${pluralize(created.length, "instance")}`}
      description={
        deploying
          ? "Waiting for the daemon to create the instances."
          : "The daemon accepted the deployment. Start the instances or open them in the fleet."
      }
      icon="deploy"
      actions={
        deploying ? null : (
          <>
            <ButtonLink to="/fleet" size="sm" variant="secondary" icon="fleet">
              Open fleet
            </ButtonLink>
            <Button
              size="sm"
              variant="ghost"
              icon="plus"
              disabled={starting}
              onClick={onDeployAnother}
            >
              Deploy another batch
            </Button>
          </>
        )
      }
    >
      {startError ? (
        <InlineNotice tone="warning" title="Some instances could not start">
          <p>{startError}</p>
        </InlineNotice>
      ) : null}

      {created.length === 0 && !deploying ? (
        <EmptyState
          compact
          icon="deploy"
          title="The daemon returned no instances"
          description="The request was accepted but no instance descriptors came back. Refresh the fleet to check."
          action={
            <ButtonLink to="/fleet" size="sm" variant="primary" icon="fleet">
              Open fleet
            </ButtonLink>
          }
        />
      ) : null}

      {created.length > 0 ? (
        <>
          <Table<AvdInfo>
            caption={`Instances created by this deployment (${created.length})`}
            rows={[...created]}
            rowKey={(instance) => instance.name}
            columns={[
              {
                key: "name",
                header: "Instance",
                render: (instance) => (
                  <ButtonLink
                    to={`/fleet/${encodeURIComponent(instance.name)}`}
                    size="sm"
                    variant="ghost"
                    icon="arrow-up-right"
                  >
                    {instance.name}
                  </ButtonLink>
                ),
              },
              {
                key: "state",
                header: "State",
                width: "120px",
                render: (instance) => <InstanceStatePill instance={instance} />,
              },
              {
                key: "ports",
                header: "Ports",
                render: (instance) =>
                  portEntries(instance)
                    .filter(([label]) => label === "Console" || label === "ADB")
                    .map(([, value]) => value)
                    .join(" / ") || "—",
              },
              {
                key: "image",
                header: "Image",
                render: (instance) => instance.image_name ?? instance.image_id ?? "—",
              },
              {
                key: "actions",
                header: "",
                width: "120px",
                align: "right",
                render: (instance) => (
                  <ButtonLink
                    to={`/fleet/${encodeURIComponent(instance.name)}`}
                    size="sm"
                    variant="secondary"
                  >
                    Open
                  </ButtonLink>
                ),
              },
            ]}
          />
          {startable.length > 0 ? (
            <div style={{ display: "flex", gap: "var(--space-2)", marginTop: "var(--space-4)", flexWrap: "wrap" }}>
              <Button
                variant="primary"
                icon="play"
                loading={starting}
                onClick={onStartAll}
              >
                Start {startable.length === created.length ? "all" : startable.length}
              </Button>
              <span className="muted small" style={{ alignSelf: "center" }}>
                Starting boots the emulators and allocates their runtime ports.
              </span>
            </div>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}
