import type {
  CapacityInfo,
  DeviceProfile,
  GpuMode,
  OobeRecord,
  OobeStatus,
  ScriptDefinition,
  SystemImage,
  Template,
} from "@/lib/types";
import { formatBytes, humanize } from "@/lib/utils";

import { MAX_INSTANCE_COUNT, NAME_PATTERN, previewNames } from "./fleet-utils";

/* -------------------------------------------------------------------------- */
/* Wizard model                                                                */
/* -------------------------------------------------------------------------- */

export type DeployStepId = "image" | "template" | "fleet" | "review";

export interface DeployStepDefinition {
  id: DeployStepId;
  label: string;
  description: string;
}

export const DEPLOY_STEPS: readonly DeployStepDefinition[] = [
  {
    id: "image",
    label: "Image",
    description: "Pick an installed, OOBE-verified system image",
  },
  {
    id: "template",
    label: "Template",
    description: "Choose an existing template or build a new profile",
  },
  {
    id: "fleet",
    label: "Fleet",
    description: "Instance count, name prefix and capacity check",
  },
  {
    id: "review",
    label: "Review",
    description: "Confirm the deployment plan",
  },
];

export const GPU_OPTIONS: ReadonlyArray<{ value: GpuMode; label: string }> = [
  { value: "auto", label: "Auto" },
  { value: "host", label: "Host GPU" },
  { value: "swiftshader_indirect", label: "SwiftShader (indirect)" },
  { value: "swiftshader", label: "SwiftShader" },
  { value: "angle_indirect", label: "ANGLE (indirect)" },
  { value: "off", label: "Off (software only)" },
];

export interface CustomTemplateDraft {
  name: string;
  deviceId: string;
  ramMb: number;
  heapMb: number;
  cpuCores: number;
  gpuMode: GpuMode;
  headless: boolean;
  proxyId: string | null;
  scriptIds: string[];
}

export type TemplateMode = "existing" | "custom";

export interface DeployDraft {
  imageId: string | null;
  templateMode: TemplateMode;
  templateId: string | null;
  custom: CustomTemplateDraft;
  count: number;
  prefix: string;
}

export function defaultCustomTemplate(device?: DeviceProfile): CustomTemplateDraft {
  return {
    name: device ? `${device.name} profile` : "Custom profile",
    deviceId: device?.id ?? "",
    ramMb: device?.ram_mb ?? 4096,
    heapMb: 512,
    cpuCores: 4,
    gpuMode: "swiftshader_indirect",
    headless: true,
    proxyId: null,
    scriptIds: [],
  };
}

export function initialDeployDraft(): DeployDraft {
  return {
    imageId: null,
    templateMode: "existing",
    templateId: null,
    custom: defaultCustomTemplate(),
    count: 1,
    prefix: "avd",
  };
}

export function imageDisplayName(image: SystemImage): string {
  return image.display_name?.trim() || image.id;
}

/** OOBE status combining the image record with the OOBE verification matrix. */
export function oobeStatusFor(image: SystemImage, records: readonly OobeRecord[]): OobeStatus {
  const record = records.find((candidate) => candidate.image_id === image.id);
  return record?.status ?? image.oobe_status ?? "unverified";
}

export function oobeStatusLabel(status: OobeStatus): string {
  switch (status) {
    case "verified":
      return "OOBE verified";
    case "failed":
      return "OOBE failed";
    case "unsupported":
      return "OOBE unsupported";
    default:
      return "OOBE unverified";
  }
}

export function imageSummary(image: SystemImage): string {
  const parts = [
    image.api_level !== undefined ? `API ${image.api_level}` : null,
    image.tag ? humanize(image.tag) : null,
    image.abi,
    image.size_bytes !== undefined && image.size_bytes !== null ? formatBytes(image.size_bytes) : null,
    image.installed ? "installed" : "not installed",
  ];
  return parts.filter(Boolean).join(" · ");
}

/* -------------------------------------------------------------------------- */
/* Validation                                                                  */
/* -------------------------------------------------------------------------- */

export function validateCustomTemplate(draft: CustomTemplateDraft): string[] {
  const errors: string[] = [];
  if (!draft.name.trim()) errors.push("A template name is required.");
  if (draft.name.trim().length > 80) errors.push("Template names must be 80 characters or fewer.");
  if (!draft.deviceId) errors.push("Choose a device profile.");
  if (!Number.isFinite(draft.ramMb) || draft.ramMb < 512) errors.push("RAM must be at least 512 MB.");
  if (!Number.isFinite(draft.heapMb) || draft.heapMb < 32) errors.push("Heap must be at least 32 MB.");
  if (!Number.isFinite(draft.cpuCores) || draft.cpuCores < 1 || draft.cpuCores > 16) {
    errors.push("CPU cores must be between 1 and 16.");
  }
  return errors;
}

export function validatePrefix(prefix: string): string | null {
  const value = prefix.trim();
  if (!value) return "A name prefix is required.";
  if (!NAME_PATTERN.test(value)) {
    return "Use 1–60 letters, digits, dots, dashes or underscores (no spaces or slashes).";
  }
  return null;
}

export function validateCount(count: number): string | null {
  if (!Number.isFinite(count)) return "Instance count must be a number.";
  if (!Number.isInteger(count)) return "Instance count must be a whole number.";
  if (count < 1) return "Deploy at least one instance.";
  if (count > MAX_INSTANCE_COUNT) return `The daemon supports at most ${MAX_INSTANCE_COUNT} instances per bulk deploy.`;
  return null;
}

export function deployNames(prefix: string, count: number): string[] {
  return previewNames(prefix, count);
}

/* -------------------------------------------------------------------------- */
/* Capacity                                                                    */
/* -------------------------------------------------------------------------- */

export interface CapacityAssessment {
  blocking: string[];
  warnings: string[];
  projectedInstances: number | null;
  projectedRamMb: number | null;
  maxInstances: number | null;
}

/** Compare the requested deployment with `GET /capacity`. */
export function assessCapacity(
  capacity: CapacityInfo | null,
  count: number,
  ramPerInstanceMb: number,
): CapacityAssessment {
  if (!capacity) {
    return { blocking: [], warnings: [], projectedInstances: null, projectedRamMb: null, maxInstances: null };
  }
  const max = capacity.max_instances ?? null;
  const deployed = capacity.deployed ?? 0;
  const projectedInstances = deployed + count;
  const projectedRamMb = count * Math.max(0, ramPerInstanceMb);
  const blocking: string[] = [];
  if (max !== null && projectedInstances > max) {
    blocking.push(
      `This deployment would create ${projectedInstances} instances but the daemon limit is ${max}.`,
    );
  }
  const warnings: string[] = [...(capacity.warnings ?? [])];
  if (
    projectedRamMb > 0 &&
    capacity.ram_available_mb !== undefined &&
    capacity.ram_available_mb !== null &&
    projectedRamMb > capacity.ram_available_mb
  ) {
    warnings.push(
      `Requested RAM (${formatBytes(projectedRamMb * 1024 * 1024)}) exceeds the ${formatBytes(
        capacity.ram_available_mb * 1024 * 1024,
      )} the daemon reports as available.`,
    );
  }
  if (
    capacity.can_start_more !== undefined &&
    capacity.can_start_more !== null &&
    count > capacity.can_start_more
  ) {
    warnings.push(
      `The host can start ${capacity.can_start_more} more running instances; count ${count} may leave some instances stopped until RAM frees up.`,
    );
  }
  return { blocking, warnings, projectedInstances, projectedRamMb, maxInstances: max };
}

/* -------------------------------------------------------------------------- */
/* Template construction / summaries                                           */
/* -------------------------------------------------------------------------- */

export function buildCustomTemplate(
  draft: CustomTemplateDraft,
  imageId: string,
  devices: readonly DeviceProfile[],
  id: string,
): Template {
  const device = devices.find((candidate) => candidate.id === draft.deviceId);
  return {
    id,
    name: draft.name.trim(),
    image_id: imageId,
    device_id: draft.deviceId || undefined,
    resolution: device?.resolution,
    density: device?.density ?? null,
    ram_mb: draft.ramMb,
    heap_mb: draft.heapMb,
    cpu_cores: draft.cpuCores,
    gpu_mode: draft.gpuMode,
    headless: draft.headless,
    proxy_id: draft.proxyId,
    scripts: draft.scriptIds,
    tags: ["ui-created"],
    description: "Created from the Deploy wizard.",
  };
}

export function gpuLabel(mode: GpuMode | string | undefined): string {
  if (!mode) return "—";
  return GPU_OPTIONS.find((option) => option.value === mode)?.label ?? humanize(mode);
}

export interface ResourceSummaryInput {
  ramMb?: number;
  heapMb?: number;
  cpuCores?: number;
  gpuMode?: GpuMode | string;
  headless?: boolean;
}

export function resourceSummary(input: ResourceSummaryInput): string {
  const parts = [
    input.cpuCores !== undefined ? `${input.cpuCores} vCPU` : null,
    input.ramMb !== undefined ? `${formatBytes(input.ramMb * 1024 * 1024)} RAM` : null,
    input.heapMb !== undefined ? `${input.heapMb} MiB heap` : null,
    input.gpuMode !== undefined ? gpuLabel(input.gpuMode) : null,
    input.headless ? "headless" : "windowed",
  ];
  return parts.filter(Boolean).join(" · ");
}

export function scriptSummary(
  scriptIds: readonly string[],
  scripts: readonly ScriptDefinition[],
): string {
  if (scriptIds.length === 0) return "No scripts";
  const names = scriptIds.map(
    (id) => scripts.find((script) => script.id === id)?.name ?? id,
  );
  return names.join(", ");
}

export function templateRam(draft: DeployDraft, template: Template | null): number {
  return template?.ram_mb ?? draft.custom.ramMb;
}
