import { useEffect, useMemo, useState } from "react";

import {
  Button,
  ButtonLink,
  Card,
  EmptyState,
  InlineNotice,
  PageHeader,
  ProgressBar,
  useToast,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import { api } from "@/lib/api";
import type { PageMeta } from "@/lib/page-meta";
import type { AvdInfo } from "@/lib/types";
import { pluralize } from "@/lib/utils";

import {
  DeployResultPanel,
  DeployFleetStep,
  DeployImageStep,
  DeployReviewStep,
  DeployTemplateStep,
} from "./_components/fleet/DeploySteps";
import { DeployStepper } from "./_components/fleet/DeployStepper";
import {
  DEPLOY_STEPS,
  assessCapacity,
  buildCustomTemplate,
  defaultCustomTemplate,
  deployNames,
  initialDeployDraft,
  oobeStatusFor,
  templateRam,
  validateCount,
  validateCustomTemplate,
  validatePrefix,
  type CustomTemplateDraft,
  type DeployDraft,
  type DeployStepId,
  type TemplateMode,
} from "./_components/fleet/deploy-model";
import {
  canStart,
  describeError,
  runBulk,
  type BulkResult,
} from "./_components/fleet/fleet-utils";

export const meta: PageMeta = {
  path: "/deploy",
  title: "Deploy AVDs",
  icon: "deploy",
  nav: false,
  description: "Bulk-deploy instances from a template",
};

type DeployPhase = "form" | "deploying" | "done";

function stepIndex(id: DeployStepId): number {
  return DEPLOY_STEPS.findIndex((step) => step.id === id);
}

/**
 * Multi-step bulk deployment wizard:
 * image → template (existing or new) → fleet size/capacity → review → deploy.
 *
 * The only daemon call that creates anything is `POST /instances/bulk` (plus
 * `POST /templates` first when a custom template is used). A created template
 * is only persisted at deploy time so abandoning the wizard leaves no orphans.
 */
export default function DeployPage() {
  const toast = useToast();

  const images = useAsync(() => api.listImages({ installed_only: true, include_remote: false }), []);
  const oobe = useAsync(() => api.getOobeMatrix(), []);
  const templates = useAsync(() => api.listTemplates(), []);
  const devices = useAsync(() => api.listDeviceProfiles(), []);
  const proxies = useAsync(() => api.listProxies(false), []);
  const scripts = useAsync(() => api.listScripts(), []);
  const capacity = useAsync(() => api.getCapacity(), []);

  const [draft, setDraft] = useState<DeployDraft>(initialDeployDraft);
  const [step, setStep] = useState<DeployStepId>("image");
  const [stepErrors, setStepErrors] = useState<string[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [phase, setPhase] = useState<DeployPhase>("form");
  const [deployStage, setDeployStage] = useState<"template" | "instances" | null>(null);
  const [created, setCreated] = useState<AvdInfo[]>([]);
  const [deployError, setDeployError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const oobeRecords = oobe.data?.records ?? [];
  const selectedImage = images.data?.find((image) => image.id === draft.imageId) ?? null;
  const selectedTemplate =
    (templates.data ?? []).find(
      (template) => template.id === draft.templateId && template.image_id === draft.imageId,
    ) ?? null;
  const ramPerInstance = templateRam(draft, selectedTemplate);
  const assessment = useMemo(
    () => assessCapacity(capacity.data, draft.count, ramPerInstance),
    [capacity.data, draft.count, ramPerInstance],
  );
  const names = useMemo(
    () => deployNames(draft.prefix, Number.isFinite(draft.count) ? Math.max(0, Math.round(draft.count)) : 0),
    [draft.prefix, draft.count],
  );
  const countError = validateCount(draft.count);
  const prefixError = validatePrefix(draft.prefix);

  const currentIndex = stepIndex(step);
  const canGoBack = currentIndex > 0;
  const lookupsLoading = devices.loading || proxies.loading || scripts.loading;
  const lookupError = devices.error ?? proxies.error ?? scripts.error;
  const reloadCapacity = capacity.reload;

  // Re-read capacity when the operator reaches the sizing/review steps.
  useEffect(() => {
    if (step !== "fleet" && step !== "review") return;
    reloadCapacity();
  }, [step, reloadCapacity]);

  const selectImage = (imageId: string): void => {
    setStepErrors([]);
    setDraft((current) => {
      const matching = (templates.data ?? []).filter((template) => template.image_id === imageId);
      const templateId = matching.some((template) => template.id === current.templateId)
        ? current.templateId
        : null;
      const custom =
        current.custom.deviceId || !devices.data?.[0]
          ? current.custom
          : { ...current.custom, ...defaultCustomTemplate(devices.data[0]) };
      return { ...current, imageId, templateId, custom };
    });
  };

  const changeTemplateMode = (mode: TemplateMode): void => {
    setStepErrors([]);
    setDraft((current) => ({
      ...current,
      templateMode: mode,
      custom:
        mode === "custom" && !current.custom.deviceId && devices.data?.[0]
          ? { ...current.custom, ...defaultCustomTemplate(devices.data[0]) }
          : current.custom,
    }));
  };

  const changeCustomTemplate = (patch: Partial<CustomTemplateDraft>): void => {
    setDraft((current) => ({ ...current, custom: { ...current.custom, ...patch } }));
  };

  const selectTemplate = (templateId: string): void => {
    setStepErrors([]);
    setDraft((current) => ({ ...current, templateId, templateMode: "existing" }));
  };

  const validateStep = (target: DeployStepId): string[] => {
    if (target === "image") {
      if (!draft.imageId) return ["Choose a system image to continue."];
      if (!selectedImage) return ["The selected image is no longer available. Pick another image."];
      return [];
    }
    if (target === "template") {
      if (draft.templateMode === "custom") return validateCustomTemplate(draft.custom);
      if (!selectedTemplate) return ["Select a template, or create a new one."];
      return [];
    }
    if (target === "fleet") {
      const errors: string[] = [];
      if (countError) errors.push(countError);
      if (prefixError) errors.push(prefixError);
      if (!countError && !prefixError) errors.push(...assessment.blocking);
      return errors;
    }
    return [];
  };

  const goToStep = (target: DeployStepId): void => {
    const targetIndex = stepIndex(target);
    if (targetIndex < 0 || targetIndex > currentIndex) return;
    setStep(target);
    setStepErrors([]);
    setSubmitted(false);
  };

  const goNext = (): void => {
    const errors = validateStep(step);
    if (errors.length > 0) {
      setStepErrors(errors);
      setSubmitted(true);
      return;
    }
    setStepErrors([]);
    setSubmitted(false);
    const next = DEPLOY_STEPS[currentIndex + 1];
    if (!next) return;
    setStep(next.id);
  };

  const goBack = (): void => {
    const previous = DEPLOY_STEPS[currentIndex - 1];
    if (!previous) return;
    setStep(previous.id);
    setStepErrors([]);
    setSubmitted(false);
  };

  const canDeploy =
    phase === "form" &&
    step === "review" &&
    Boolean(selectedImage) &&
    assessment.blocking.length === 0 &&
    !countError &&
    !prefixError &&
    (draft.templateMode === "existing" ? Boolean(selectedTemplate) : validateCustomTemplate(draft.custom).length === 0);

  const deploy = async (): Promise<void> => {
    if (!selectedImage || !canDeploy) return;
    setPhase("deploying");
    setDeployError(null);
    setStartError(null);
    setCreated([]);
    try {
      let templateId = draft.templateMode === "existing" ? selectedTemplate?.id : undefined;
      if (draft.templateMode === "custom") {
        setDeployStage("template");
        const template = await api.createTemplate(
          buildCustomTemplate(
            draft.custom,
            selectedImage.id,
            devices.data ?? [],
            `tpl-ui-${Date.now().toString(36)}`,
          ),
        );
        templateId = template.id;
      }
      if (!templateId) throw new Error("No template selected for the deployment.");
      setDeployStage("instances");
      const result = await api.bulkDeployInstances({
        template_id: templateId,
        count: Math.round(draft.count),
        prefix: draft.prefix.trim(),
      });
      setCreated(result);
      setPhase("done");
      toast.success(
        `Deployed ${pluralize(result.length, "instance")}`,
        `Template ${templateId} · ${pluralize(result.length, "name")} created`,
      );
      void capacity.reload();
      void templates.reload();
    } catch (error) {
      const message = describeError(error);
      setDeployError(message);
      setPhase("form");
      setStep("review");
      toast.error("Deployment failed", message);
    } finally {
      setDeployStage(null);
    }
  };

  const startAll = async (): Promise<void> => {
    const targets = created.filter((instance) => canStart(instance.state)).map((instance) => instance.name);
    if (targets.length === 0) return;
    setStarting(true);
    setStartError(null);
    try {
      const result: BulkResult = await runBulk(targets, (name) => api.startInstance(name));
      const all = await api.listInstances();
      setCreated((current) =>
        current.map((instance) => all.find((candidate) => candidate.name === instance.name) ?? instance),
      );
      if (result.failed.length === 0) {
        toast.success(`Started ${pluralize(result.succeeded.length, "instance")}`);
      } else {
        const detail = result.failed
          .map((failure) => `${failure.name}: ${failure.message}`)
          .join(" · ");
        setStartError(detail);
        toast.warning(
          `Started ${result.succeeded.length} of ${pluralize(targets.length, "instance")}`,
          detail,
        );
      }
    } catch (error) {
      toast.error("Could not start the deployed instances", describeError(error));
    } finally {
      setStarting(false);
    }
  };

  const deployAnother = (): void => {
    setDraft({ ...initialDeployDraft(), prefix: draft.prefix });
    setStep("image");
    setPhase("form");
    setCreated([]);
    setDeployError(null);
    setStartError(null);
    setStepErrors([]);
    setSubmitted(false);
  };

  const oobeStatus = selectedImage
    ? oobeStatusFor(selectedImage, oobeRecords)
    : "unverified";

  return (
    <div className="page">
      <PageHeader
        title="Deploy AVDs"
        eyebrow="Bulk deployment"
        icon="deploy"
        breadcrumbs={[{ label: "Fleet", to: "/fleet" }, { label: "Deploy" }]}
        description="Create one or many instances from a template, with an OOBE-aware image picker and a capacity check before anything is created."
        actions={
          <>
            <ButtonLink to="/fleet" variant="secondary" icon="fleet">
              Fleet
            </ButtonLink>
            <ButtonLink to="/templates" variant="secondary" icon="templates">
              Templates
            </ButtonLink>
          </>
        }
      />

      {phase === "form" ? (
        <>
          <DeployStepper current={step} onSelect={goToStep} />

          {stepErrors.length > 0 ? (
            <InlineNotice tone="danger" title="Cannot continue yet">
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                {stepErrors.map((error) => (
                  <li key={error}>{error}</li>
                ))}
              </ul>
            </InlineNotice>
          ) : null}

          {step === "image" ? (
            <DeployImageStep
              images={images.data}
              oobeRecords={oobeRecords}
              loading={images.loading || oobe.loading}
              error={images.error ?? oobe.error}
              onRetry={() => {
                images.reload();
                oobe.reload();
              }}
              selectedImageId={draft.imageId}
              onSelect={selectImage}
            />
          ) : null}

          {step === "template" && selectedImage ? (
            <DeployTemplateStep
              image={selectedImage}
              templates={templates.data}
              templatesLoading={templates.loading}
              templatesError={templates.error}
              onRetryTemplates={templates.reload}
              mode={draft.templateMode}
              onModeChange={changeTemplateMode}
              templateId={draft.templateId}
              onSelectTemplate={selectTemplate}
              custom={draft.custom}
              onCustomChange={changeCustomTemplate}
              devices={devices.data}
              proxies={proxies.data}
              scripts={scripts.data}
              lookupsLoading={lookupsLoading}
              lookupError={lookupError}
              onRetryLookups={() => {
                devices.reload();
                proxies.reload();
                scripts.reload();
              }}
              submitted={submitted}
            />
          ) : null}

          {step === "template" && !selectedImage ? (
            <EmptyState
              icon="images"
              title="Choose an image first"
              description="Templates are filtered by the system image selected in the previous step."
              action={
                <Button variant="primary" icon="arrow-left" onClick={() => goToStep("image")}>
                  Back to images
                </Button>
              }
            />
          ) : null}

          {step === "fleet" ? (
            <DeployFleetStep
              count={draft.count}
              prefix={draft.prefix}
              names={names}
              onCountChange={(count) => setDraft((current) => ({ ...current, count }))}
              onPrefixChange={(prefix) => setDraft((current) => ({ ...current, prefix }))}
              countError={countError}
              prefixError={prefixError}
              capacity={capacity.data}
              capacityLoading={capacity.loading}
              capacityError={capacity.error}
              onRefreshCapacity={() => void capacity.reload()}
              assessment={assessment}
              ramPerInstanceMb={ramPerInstance}
            />
          ) : null}

          {step === "review" && selectedImage ? (
            <DeployReviewStep
              image={selectedImage}
              oobeStatus={oobeStatus}
              draft={draft}
              template={selectedTemplate}
              devices={devices.data}
              proxies={proxies.data}
              scripts={scripts.data}
              names={names}
              assessment={assessment}
              capacity={capacity.data}
              deployError={deployError}
            />
          ) : null}

          {step === "review" && !selectedImage ? (
            <EmptyState
              icon="deploy"
              title="Nothing to review yet"
              description="Choose an image and a template before reviewing the deployment plan."
              action={
                <Button variant="primary" icon="arrow-left" onClick={() => goToStep("image")}>
                  Start over
                </Button>
              }
            />
          ) : null}

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "var(--space-3)",
              flexWrap: "wrap",
              paddingTop: "var(--space-2)",
              borderTop: "1px solid var(--border)",
            }}
          >
            <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
              <Button variant="ghost" icon="arrow-left" disabled={!canGoBack} onClick={goBack}>
                Back
              </Button>
              <ButtonLink to="/fleet" variant="ghost">
                Cancel
              </ButtonLink>
            </div>
            <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
              <span className="muted small" aria-live="polite">
                Step {currentIndex + 1} of {DEPLOY_STEPS.length}
              </span>
              {step === "review" ? (
                <Button
                  variant="primary"
                  icon="deploy"
                  disabled={!canDeploy}
                  onClick={() => void deploy()}
                >
                  Deploy {pluralize(Math.max(1, Math.round(draft.count) || 1), "instance")}
                </Button>
              ) : (
                <Button variant="primary" iconRight="arrow-right" onClick={goNext}>
                  Continue
                </Button>
              )}
            </div>
          </div>
        </>
      ) : null}

      {phase === "deploying" ? (
        <Card title="Deploying…" description="The daemon is creating the instances. Keep this page open." icon="deploy">
          <ProgressBar
            indeterminate
            label={deployStage === "template" ? "Creating the template…" : "Creating instances…"}
          />
          <p className="muted small" style={{ marginTop: "var(--space-3)", marginBottom: 0 }}>
            {deployStage === "template"
              ? "Saving the new template before the bulk request."
              : `POST /instances/bulk · ${pluralize(Math.round(draft.count), "instance")} with prefix "${draft.prefix.trim()}"`}
          </p>
        </Card>
      ) : null}

      {phase === "done" ? (
        <DeployResultPanel
          created={created}
          deploying={false}
          starting={starting}
          startError={startError}
          onStartAll={() => void startAll()}
          onDeployAnother={deployAnother}
        />
      ) : null}
    </div>
  );
}
