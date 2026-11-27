/**
 * Create / edit / clone dialog for AVD templates (`POST/PUT /templates`).
 * Page-private to the Templates page.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import {
  Button,
  Checkbox,
  Field,
  InlineNotice,
  Input,
  Modal,
  Select,
  Textarea,
} from "@/lib/components";
import type {
  DeviceProfile,
  GpuMode,
  ProxyView,
  ScriptDefinition,
  SystemImage,
  Template,
} from "@/lib/types";

import { errorMessage, styles } from "./common";
import { parseTagsInput } from "./proxy-utils";

export type TemplateDialogMode = "create" | "edit" | "clone";

const GPU_MODES: Array<{ value: GpuMode; label: string }> = [
  { value: "auto", label: "Automatic" },
  { value: "host", label: "Host GPU" },
  { value: "swiftshader_indirect", label: "SwiftShader (indirect)" },
  { value: "swiftshader", label: "SwiftShader" },
  { value: "angle_indirect", label: "ANGLE (indirect)" },
  { value: "off", label: "Off (software only)" },
];

const ID_PATTERN = /^[a-zA-Z0-9._-]{1,63}$/;
const RESOLUTION_PATTERN = /^\d{3,5}x\d{3,5}$/;

interface TemplateForm {
  id: string;
  name: string;
  imageId: string;
  deviceId: string;
  resolution: string;
  density: string;
  ramMb: string;
  heapMb: string;
  cpuCores: string;
  gpuMode: GpuMode;
  headless: boolean;
  proxyId: string;
  scripts: string[];
  tags: string;
  description: string;
  configText: string;
}

type FormErrors = Partial<
  Record<
    | "id"
    | "name"
    | "imageId"
    | "deviceId"
    | "resolution"
    | "density"
    | "ramMb"
    | "heapMb"
    | "cpuCores"
    | "proxyId"
    | "config"
    | "form",
    string
  >
>;

function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

function initialForm(initial: Template | null): TemplateForm {
  if (initial) {
    return {
      id: initial.id,
      name: initial.name,
      imageId: initial.image_id,
      deviceId: initial.device_id ?? "",
      resolution: initial.resolution ?? "",
      density: initial.density === null || initial.density === undefined ? "" : String(initial.density),
      ramMb: initial.ram_mb === undefined ? "4096" : String(initial.ram_mb),
      heapMb: initial.heap_mb === undefined ? "" : String(initial.heap_mb),
      cpuCores: initial.cpu_cores === undefined ? "4" : String(initial.cpu_cores),
      gpuMode: initial.gpu_mode ?? "auto",
      headless: initial.headless ?? true,
      proxyId: initial.proxy_id ?? "",
      scripts: initial.scripts ?? [],
      tags: (initial.tags ?? []).join(", "),
      description: initial.description ?? "",
      configText: initial.config && Object.keys(initial.config).length > 0 ? JSON.stringify(initial.config, null, 2) : "",
    };
  }
  return {
    id: "",
    name: "",
    imageId: "",
    deviceId: "",
    resolution: "",
    density: "",
    ramMb: "4096",
    heapMb: "",
    cpuCores: "4",
    gpuMode: "swiftshader_indirect",
    headless: true,
    proxyId: "",
    scripts: [],
    tags: "",
    description: "",
    configText: "",
  };
}

function parseInteger(
  value: string,
  label: string,
  min: number,
  max: number,
  required: boolean,
): { value?: number; error?: string } {
  const trimmed = value.trim();
  if (!trimmed) {
    return required ? { error: `${label} is required.` } : {};
  }
  if (!/^\d+$/.test(trimmed)) {
    return { error: `${label} must be a whole number.` };
  }
  const parsed = Number(trimmed);
  if (parsed < min || parsed > max) {
    return { error: `${label} must be between ${min} and ${max}.` };
  }
  return { value: parsed };
}

function parseConfigText(text: string): { value?: Record<string, string>; error?: string } {
  const trimmed = text.trim();
  if (!trimmed) return { value: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { error: "Extra config must be valid JSON." };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { error: "Extra config must be a JSON object of string values." };
  }
  const result: Record<string, string> = {};
  for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof value === "string") result[key] = value;
    else if (typeof value === "number" || typeof value === "boolean") result[key] = String(value);
    else return { error: `Config value for "${key}" must be a string, number or boolean.` };
  }
  return { value: result };
}

export function TemplateDialog({
  open,
  mode,
  initial,
  templates,
  images,
  devices,
  scripts,
  proxies,
  onClose,
  onSaved,
}: {
  open: boolean;
  mode: TemplateDialogMode;
  initial: Template | null;
  templates: Template[];
  images: SystemImage[];
  devices: DeviceProfile[];
  scripts: ScriptDefinition[];
  proxies: ProxyView[];
  onClose: () => void;
  onSaved: (template: Template, mode: TemplateDialogMode) => void;
}) {
  const [form, setForm] = useState<TemplateForm>(() => initialForm(initial));
  const [errors, setErrors] = useState<FormErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const idTouchedRef = useRef(mode !== "create");

  useEffect(() => {
    if (!open) return;
    setForm(initialForm(initial));
    setErrors({});
    setSubmitError(null);
    setSaving(false);
    idTouchedRef.current = mode !== "create";
  }, [open, mode, initial]);

  const imageOptions = useMemo(
    () =>
      images.map((image) => ({
        value: image.id,
        label: `${image.display_name ?? image.id} · API ${image.api_level ?? "?"}${
          image.installed ? "" : " (not installed)"
        }`,
        disabled: !image.installed,
      })),
    [images],
  );

  const deviceOptions = useMemo(
    () =>
      devices.map((device) => ({
        value: device.id,
        label: `${device.name}${device.resolution ? ` · ${device.resolution}` : ""}`,
      })),
    [devices],
  );

  const proxyOptions = useMemo(
    () =>
      proxies.map((proxy) => ({
        value: proxy.id,
        label: `${proxy.name ?? proxy.id} · ${proxy.scheme ?? "http"}://${proxy.host}:${proxy.port}${
          proxy.enabled === false ? " (disabled)" : ""
        }`,
      })),
    [proxies],
  );

  const update = <Key extends keyof TemplateForm>(key: Key, value: TemplateForm[Key]): void => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const toggleScript = (scriptId: string, checked: boolean): void => {
    setForm((current) => ({
      ...current,
      scripts: checked
        ? [...current.scripts.filter((id) => id !== scriptId), scriptId]
        : current.scripts.filter((id) => id !== scriptId),
    }));
  };

  const validate = (): FormErrors => {
    const next: FormErrors = {};
    const id = form.id.trim();
    if (!id) {
      next.id = "Template ID is required.";
    } else if (!ID_PATTERN.test(id)) {
      next.id = "Use 1–63 letters, digits, dots, underscores or dashes.";
    } else if (
      mode !== "edit" &&
      templates.some((template) => template.id === id)
    ) {
      next.id = "A template with this ID already exists.";
    }

    if (!form.name.trim()) {
      next.name = "Template name is required.";
    } else if (form.name.trim().length > 120) {
      next.name = "Keep the name at 120 characters or fewer.";
    }

    if (!form.imageId) {
      next.imageId = "Choose a system image.";
    } else {
      const image = images.find((candidate) => candidate.id === form.imageId);
      if (!image) next.imageId = "That image is not in the catalogue.";
      else if (!image.installed) {
        next.imageId = "This image is not installed. Download it from the Images page first.";
      }
    }

    if (form.deviceId && !devices.some((device) => device.id === form.deviceId)) {
      next.deviceId = "Unknown device profile.";
    }

    if (form.resolution.trim() && !RESOLUTION_PATTERN.test(form.resolution.trim())) {
      next.resolution = "Use WIDTHxHEIGHT, for example 1080x2400.";
    }

    const density = parseInteger(form.density, "Density", 120, 640, false);
    if (density.error) next.density = density.error;

    const ram = parseInteger(form.ramMb, "RAM", 512, 16384, true);
    if (ram.error) next.ramMb = ram.error;

    const heap = parseInteger(form.heapMb, "Heap", 64, 4096, false);
    if (heap.error) next.heapMb = heap.error;

    const cores = parseInteger(form.cpuCores, "CPU cores", 1, 16, true);
    if (cores.error) next.cpuCores = cores.error;

    if (form.proxyId && !proxies.some((proxy) => proxy.id === form.proxyId)) {
      next.proxyId = "Unknown proxy.";
    }

    const config = parseConfigText(form.configText);
    if (config.error) next.config = config.error;

    return next;
  };

  const submit = async (): Promise<void> => {
    const nextErrors = validate();
    setErrors(nextErrors);
    setSubmitError(null);
    if (Object.keys(nextErrors).length > 0) return;

    const config = parseConfigText(form.configText).value ?? {};
    const tags = parseTagsInput(form.tags);
    const payload: Template = {
      id: form.id.trim(),
      name: form.name.trim(),
      image_id: form.imageId,
      device_id: form.deviceId,
      resolution: form.resolution.trim(),
      density: form.density.trim() ? Number(form.density.trim()) : null,
      ram_mb: Number(form.ramMb.trim()),
      cpu_cores: Number(form.cpuCores.trim()),
      gpu_mode: form.gpuMode,
      headless: form.headless,
      proxy_id: form.proxyId || null,
      scripts: form.scripts,
      tags,
      description: form.description.trim(),
      config,
      ...(form.heapMb.trim() ? { heap_mb: Number(form.heapMb.trim()) } : {}),
    };

    setSaving(true);
    try {
      const saved =
        mode === "edit" && initial
          ? await api.updateTemplate(initial.id, payload)
          : await api.createTemplate(payload);
      onSaved(saved, mode);
    } catch (caught) {
      setSubmitError(errorMessage(caught));
    } finally {
      setSaving(false);
    }
  };

  const submitLabel = mode === "edit" ? "Save changes" : mode === "clone" ? "Create copy" : "Create template";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === "edit" ? "Edit template" : mode === "clone" ? "Clone template" : "New template"}
      description={
        mode === "edit"
          ? "Update the blueprint used for future deployments. Existing AVDs keep their current configuration."
          : "A template pins the image, device profile, resources, proxy and boot scripts used when deploying AVDs."
      }
      size="lg"
      dismissible={!saving}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button variant="primary" icon="templates" loading={saving} onClick={() => void submit()}>
            {submitLabel}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
        {submitError ? (
          <InlineNotice tone="danger" title="Could not save the template">
            <p>{submitError}</p>
          </InlineNotice>
        ) : null}

        <section style={styles.stack}>
          <h3 style={styles.entityTitle}>Identity</h3>
          <div style={styles.fieldGridTwo}>
            <Field
              label="Template ID"
              required
              error={errors.id}
              hint={
                mode === "edit"
                  ? "The ID is immutable once the template exists."
                  : "Stable identifier used by deploy URLs and the API (letters, digits, . _ -)."
              }
            >
              <Input
                value={form.id}
                disabled={mode === "edit"}
                placeholder="tpl-pixel7-play"
                invalid={Boolean(errors.id)}
                onChange={(event) => {
                  idTouchedRef.current = true;
                  update("id", event.target.value);
                }}
              />
            </Field>
            <Field label="Template name" required error={errors.name}>
              <Input
                value={form.name}
                placeholder="Pixel 7 · Play (API 35)"
                invalid={Boolean(errors.name)}
                onChange={(event) => {
                  const value = event.target.value;
                  setForm((current) => ({
                    ...current,
                    name: value,
                    id: idTouchedRef.current ? current.id : slugify(value),
                  }));
                }}
              />
            </Field>
          </div>
        </section>

        <section style={styles.stack}>
          <h3 style={styles.entityTitle}>Image &amp; device</h3>
          <div style={styles.fieldGridTwo}>
            <Field
              label="System image"
              required
              error={errors.imageId}
              hint={images.length === 0 ? "Loading the image catalogue…" : "Only installed images can be selected."}
            >
              <Select
                value={form.imageId}
                invalid={Boolean(errors.imageId)}
                placeholder="Choose an installed image"
                options={imageOptions}
                onChange={(event) => update("imageId", event.target.value)}
              />
            </Field>
            <Field label="Device profile" error={errors.deviceId} hint="Optional hardware profile from GET /devices.">
              <Select
                value={form.deviceId}
                options={[{ value: "", label: "Any device profile" }, ...deviceOptions]}
                onChange={(event) => update("deviceId", event.target.value)}
              />
            </Field>
          </div>
          <div style={styles.fieldGridTwo}>
            <Field label="Resolution" error={errors.resolution} hint="Optional override, e.g. 1080x2400.">
              <Input
                value={form.resolution}
                placeholder="1080x2400"
                invalid={Boolean(errors.resolution)}
                onChange={(event) => update("resolution", event.target.value)}
              />
            </Field>
            <Field label="Density (dpi)" error={errors.density} hint="Optional; 120–640.">
              <Input
                value={form.density}
                type="number"
                min={120}
                max={640}
                placeholder="420"
                invalid={Boolean(errors.density)}
                onChange={(event) => update("density", event.target.value)}
              />
            </Field>
          </div>
        </section>

        <section style={styles.stack}>
          <h3 style={styles.entityTitle}>Resources</h3>
          <div style={styles.fieldGrid}>
            <Field label="RAM (MB)" required error={errors.ramMb} hint="512–16384 MB.">
              <Input
                value={form.ramMb}
                type="number"
                min={512}
                max={16384}
                invalid={Boolean(errors.ramMb)}
                onChange={(event) => update("ramMb", event.target.value)}
              />
            </Field>
            <Field label="Heap size (MB)" error={errors.heapMb} hint="Optional; 64–4096 MB.">
              <Input
                value={form.heapMb}
                type="number"
                min={64}
                max={4096}
                placeholder="512"
                invalid={Boolean(errors.heapMb)}
                onChange={(event) => update("heapMb", event.target.value)}
              />
            </Field>
            <Field label="CPU cores" required error={errors.cpuCores} hint="1–16 vCPUs.">
              <Input
                value={form.cpuCores}
                type="number"
                min={1}
                max={16}
                invalid={Boolean(errors.cpuCores)}
                onChange={(event) => update("cpuCores", event.target.value)}
              />
            </Field>
            <Field label="GPU mode" hint="Rendering backend passed to the emulator.">
              <Select
                value={form.gpuMode}
                onChange={(event) => update("gpuMode", event.target.value as GpuMode)}
              >
                {GPU_MODES.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Checkbox
            label="Headless"
            hint="Run without the emulator window; required for most CI hosts and isolated fleet workers."
            checked={form.headless}
            onChange={(event) => update("headless", event.target.checked)}
          />
        </section>

        <section style={styles.stack}>
          <h3 style={styles.entityTitle}>Network &amp; automation</h3>
          <div style={styles.fieldGridTwo}>
            <Field label="Default proxy" error={errors.proxyId} hint="Optional; instances can be re-assigned later.">
              <Select
                value={form.proxyId}
                options={[{ value: "", label: "No proxy" }, ...proxyOptions]}
                onChange={(event) => update("proxyId", event.target.value)}
              />
            </Field>
            <Field label="Tags" hint="Comma-separated labels used for filtering and fleet specs.">
              <Input
                value={form.tags}
                placeholder="play, pixel, verified-oobe"
                onChange={(event) => update("tags", event.target.value)}
              />
            </Field>
          </div>

          <Field label="Boot scripts" hint="Scripts started automatically after the guest boots.">
            {scripts.length === 0 ? (
              <p className="muted small" style={{ margin: 0 }}>
                No scripts are available yet.
              </p>
            ) : (
              <div style={styles.scrollBox} role="group" aria-label="Boot scripts">
                {scripts.map((script) => (
                  <Checkbox
                    key={script.id}
                    label={script.name}
                    hint={`${script.id}${script.description ? ` · ${script.description}` : ""}`}
                    checked={form.scripts.includes(script.id)}
                    onChange={(event) => toggleScript(script.id, event.target.checked)}
                  />
                ))}
              </div>
            )}
          </Field>
        </section>

        <section style={styles.stack}>
          <h3 style={styles.entityTitle}>Metadata</h3>
          <Field label="Description" hint="Optional notes shown on the template card.">
            <Textarea
              value={form.description}
              rows={2}
              placeholder="Play Store capable Pixel 7 profile for account-free automation."
              onChange={(event) => update("description", event.target.value)}
            />
          </Field>
          <details>
            <summary className="muted small" style={{ cursor: "pointer" }}>
              Advanced emulator config (JSON)
            </summary>
            <div style={{ marginTop: "var(--space-3)" }}>
              <Field
                label="Extra config"
                error={errors.config}
                hint='Optional key/value pairs passed to the AVD, e.g. {"disk.dataPartition.size":"6G"}.'
              >
                <Textarea
                  value={form.configText}
                  rows={4}
                  placeholder={'{\n  "disk.dataPartition.size": "6G"\n}'}
                  invalid={Boolean(errors.config)}
                  onChange={(event) => update("configText", event.target.value)}
                />
              </Field>
            </div>
          </details>
        </section>
      </div>
    </Modal>
  );
}
