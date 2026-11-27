import { useMemo, useState } from "react";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  ButtonLink,
  Card,
  ConfirmDialog,
  EmptyState,
  IconButton,
  InlineNotice,
  PageHeader,
  SearchBox,
  Skeleton,
  Spinner,
  StatusPill,
  useToast,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type { DeviceProfile, ProxyView, ScriptDefinition, SystemImage, Template } from "@/lib/types";
import { formatDateTime, formatRelativeTime, humanize } from "@/lib/utils";

import {
  DetailList,
  Mono,
  SubText,
  TagList,
  errorMessage,
  styles,
} from "@/pages/_components/catalog/common";
import {
  TemplateDialog,
  type TemplateDialogMode,
} from "@/pages/_components/catalog/template-dialog";

export const meta: PageMeta = {
  path: "/templates",
  title: "Templates",
  icon: "templates",
  nav: 40,
  description: "Reusable AVD blueprints",
};

function cloneTemplate(source: Template, existing: Template[]): Template {
  const ids = new Set(existing.map((template) => template.id));
  const base = `${source.id}-copy`.slice(0, 63);
  let candidate = base;
  let suffix = 2;
  while (ids.has(candidate)) {
    const tail = `-${suffix}`;
    candidate = `${base.slice(0, 63 - tail.length)}${tail}`;
    suffix += 1;
  }
  return { ...source, id: candidate, name: `${source.name} (copy)` };
}

function imageStatusTone(image: SystemImage | undefined): "success" | "warning" | "danger" | "neutral" {
  if (!image) return "danger";
  if (image.oobe_status === "verified") return "success";
  if (image.oobe_status === "unverified") return "warning";
  if (image.oobe_status === "failed") return "danger";
  return "neutral";
}

function TemplateCard({
  template,
  image,
  device,
  proxy,
  scripts,
  onEdit,
  onClone,
  onDelete,
}: {
  template: Template;
  image: SystemImage | undefined;
  device: DeviceProfile | undefined;
  proxy: ProxyView | undefined;
  scripts: ScriptDefinition[];
  onEdit: () => void;
  onClone: () => void;
  onDelete: () => void;
}) {
  const scriptNames = (template.scripts ?? [])
    .map((id) => scripts.find((script) => script.id === id)?.name ?? id)
    .join(", ");

  return (
    <Card
      title={template.name}
      description={<Mono>{template.id}</Mono>}
      icon="templates"
      actions={
        <div style={styles.actions}>
          <IconButton label={`Edit ${template.name}`} icon="edit" size="sm" onClick={onEdit} />
          <IconButton label={`Clone ${template.name}`} icon="copy" size="sm" onClick={onClone} />
          <IconButton
            label={`Delete ${template.name}`}
            icon="trash"
            size="sm"
            variant="danger"
            onClick={onDelete}
          />
        </div>
      }
      footer={
        <div style={styles.rowBetween}>
          <SubText title={formatDateTime(template.updated_at ?? template.created_at)}>
            Updated {formatRelativeTime(template.updated_at ?? template.created_at)}
          </SubText>
          <ButtonLink
            to={`/deploy?template=${encodeURIComponent(template.id)}`}
            variant="primary"
            size="sm"
            icon="deploy"
          >
            Deploy
          </ButtonLink>
        </div>
      }
    >
      <div style={styles.cardStack}>
        <div style={styles.rowTight}>
          <Badge tone={image ? "info" : "danger"} icon="images">
            {image?.display_name ?? `Missing image: ${template.image_id}`}
          </Badge>
          {image ? (
            <StatusPill
              status={image.oobe_status ?? "unverified"}
              label={`OOBE ${humanize(image.oobe_status ?? "unverified")}`}
              tone={imageStatusTone(image)}
            />
          ) : null}
          <Badge tone="neutral" icon="instance">
            {device?.name ?? template.device_id ?? "Any device profile"}
          </Badge>
        </div>

        <div style={styles.rowTight}>
          <Badge tone="neutral" icon="cpu">
            {template.cpu_cores ?? "—"} vCPU
          </Badge>
          <Badge tone="neutral" icon="hard-drive">
            {template.ram_mb ?? "—"} MB RAM
          </Badge>
          {template.heap_mb ? <Badge tone="neutral">{template.heap_mb} MB heap</Badge> : null}
          <Badge tone="neutral" icon="monitor">
            {humanize(template.gpu_mode ?? "auto")}
          </Badge>
          <Badge tone={template.headless === false ? "neutral" : "success"} icon="shield">
            {template.headless === false ? "Windowed" : "Headless"}
          </Badge>
        </div>

        {template.description ? <SubText>{template.description}</SubText> : null}

        <DetailList
          items={[
            {
              label: "Proxy",
              value: template.proxy_id ? (
                proxy ? (
                  <span style={styles.rowTight}>
                    <Badge tone={proxy.enabled === false ? "neutral" : "success"}>{proxy.name ?? proxy.id}</Badge>
                    <Mono>{`${proxy.scheme ?? "http"}://${proxy.host}:${proxy.port}`}</Mono>
                  </span>
                ) : (
                  <Badge tone="warning">Missing proxy: {template.proxy_id}</Badge>
                )
              ) : (
                "No proxy"
              ),
            },
            {
              label: "Boot scripts",
              value: scriptNames ? <span title={scriptNames}>{scriptNames}</span> : "—",
            },
            { label: "Tags", value: <TagList tags={template.tags} /> },
            {
              label: "Screen",
              value:
                template.resolution || template.density
                  ? [template.resolution, template.density ? `${template.density} dpi` : null]
                      .filter(Boolean)
                      .join(" · ")
                  : "—",
            },
          ]}
        />
      </div>
    </Card>
  );
}

export default function TemplatesPage() {
  const toast = useToast();
  const templates = useAsync(() => api.listTemplates(), []);
  const images = useAsync(() => api.listImages(), []);
  const devices = useAsync(() => api.listDeviceProfiles(), []);
  const scripts = useAsync(() => api.listScripts(), []);
  const proxies = useAsync(() => api.listProxies(true), []);

  const [query, setQuery] = useState("");
  const [dialog, setDialog] = useState<{ mode: TemplateDialogMode; initial: Template | null } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Template | null>(null);

  const allTemplates = useMemo(() => templates.data ?? [], [templates.data]);
  const allImages = useMemo(() => images.data ?? [], [images.data]);
  const allDevices = useMemo(() => devices.data ?? [], [devices.data]);
  const allScripts = useMemo(() => scripts.data ?? [], [scripts.data]);
  const allProxies = useMemo(() => proxies.data ?? [], [proxies.data]);

  const imageById = useMemo(() => new Map(allImages.map((image) => [image.id, image])), [allImages]);
  const deviceById = useMemo(() => new Map(allDevices.map((device) => [device.id, device])), [allDevices]);
  const proxyById = useMemo(() => new Map(allProxies.map((proxy) => [proxy.id, proxy])), [allProxies]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return allTemplates;
    return allTemplates.filter((template) => {
      const haystack = [
        template.name,
        template.id,
        template.description,
        template.image_id,
        template.device_id,
        ...(template.tags ?? []),
        ...(template.scripts ?? []),
      ]
        .filter((value): value is string => typeof value === "string")
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [allTemplates, query]);

  const supportingError = images.error ?? devices.error ?? scripts.error ?? proxies.error;

  const handleSaved = (saved: Template, mode: TemplateDialogMode): void => {
    const verb = mode === "edit" ? "updated" : mode === "clone" ? "cloned" : "created";
    toast.success(`Template ${verb}`, saved.name);
    setDialog(null);
    templates.reload();
  };

  const handleDelete = async (): Promise<void> => {
    if (!deleteTarget) return;
    try {
      await api.deleteTemplate(deleteTarget.id);
      toast.success("Template deleted", deleteTarget.name);
      setDeleteTarget(null);
      templates.reload();
    } catch (caught) {
      toast.error("Could not delete the template", errorMessage(caught));
    }
  };

  const reloadSupporting = (): void => {
    images.reload();
    devices.reload();
    scripts.reload();
    proxies.reload();
  };

  return (
    <div className="page">
      <PageHeader
        title="Templates"
        eyebrow="Blueprints"
        description="Reusable device configurations: image, device profile, RAM/CPU, GPU mode, proxy and boot scripts."
        meta={
          <div style={styles.rowTight}>
            <Badge tone="info" icon="templates">
              {allTemplates.length} templates
            </Badge>
            <Badge tone="success" icon="images">
              {allImages.filter((image) => image.installed).length} installed images
            </Badge>
            <Badge tone="neutral" icon="link">
              {allProxies.filter((proxy) => proxy.enabled !== false).length} enabled proxies
            </Badge>
          </div>
        }
        actions={
          <>
            <ButtonLink to="/deploy" variant="secondary" icon="deploy">
              Deploy AVDs
            </ButtonLink>
            <Button variant="primary" icon="plus" onClick={() => setDialog({ mode: "create", initial: null })}>
              New template
            </Button>
          </>
        }
      />

      {templates.error ? (
        <InlineNotice
          tone="danger"
          title="Could not load templates"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={templates.reload}>
              Retry
            </Button>
          }
        >
          <p>{errorMessage(templates.error)}</p>
        </InlineNotice>
      ) : null}

      {supportingError ? (
        <InlineNotice
          tone="warning"
          title="Some template options could not be loaded"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={reloadSupporting}>
              Retry
            </Button>
          }
        >
          <p>
            Images, device profiles, scripts or proxies failed to load, so the template editor may offer
            fewer choices. {errorMessage(supportingError)}
          </p>
        </InlineNotice>
      ) : null}

      {allTemplates.length > 0 || query ? (
        <div style={styles.rowBetween}>
          <div style={{ flex: "1 1 300px", maxWidth: "440px" }}>
            <SearchBox
              value={query}
              onChange={setQuery}
              placeholder="Search templates by name, tag, image or ID…"
              label="Search templates"
            />
          </div>
          <SubText>
            {filtered.length} of {allTemplates.length} templates
            {templates.loading ? " · refreshing…" : ""}
          </SubText>
        </div>
      ) : null}

      {templates.loading && allTemplates.length === 0 ? (
        <div style={styles.cardGrid} aria-label="Loading templates">
          {[0, 1, 2].map((index) => (
            <Card key={index} title={<Skeleton width="52%" height={18} />}>
              <div className="ds-skeleton-stack">
                <Skeleton lines={5} />
              </div>
            </Card>
          ))}
        </div>
      ) : templates.error && allTemplates.length === 0 ? null : allTemplates.length === 0 ? (
        <EmptyState
          icon="templates"
          title="No templates yet"
          description="A template pins the image, device profile, resources and automation hooks used when deploying AVDs. Create or clone one to get started."
          action={
            <div style={styles.rowTight}>
              <Button variant="primary" icon="plus" onClick={() => setDialog({ mode: "create", initial: null })}>
                Create your first template
              </Button>
              <ButtonLink to="/images" variant="secondary" icon="images">
                Browse images
              </ButtonLink>
            </div>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          compact
          icon="search"
          title="No templates match your search"
          description={`Nothing matches “${query}”. Try a different name, tag or image ID.`}
          action={
            <Button size="sm" variant="secondary" icon="close" onClick={() => setQuery("")}>
              Clear search
            </Button>
          }
        />
      ) : (
        <div style={styles.cardGrid}>
          {filtered.map((template) => (
            <TemplateCard
              key={template.id}
              template={template}
              image={imageById.get(template.image_id)}
              device={template.device_id ? deviceById.get(template.device_id) : undefined}
              proxy={template.proxy_id ? proxyById.get(template.proxy_id) : undefined}
              scripts={allScripts}
              onEdit={() => setDialog({ mode: "edit", initial: template })}
              onClone={() =>
                setDialog({ mode: "clone", initial: cloneTemplate(template, allTemplates) })
              }
              onDelete={() => setDeleteTarget(template)}
            />
          ))}
        </div>
      )}

      {templates.loading && allTemplates.length > 0 ? (
        <div style={styles.row}>
          <Spinner size="sm" label="Refreshing templates" />
          <SubText>Refreshing templates…</SubText>
        </div>
      ) : null}

      <TemplateDialog
        open={dialog !== null}
        mode={dialog?.mode ?? "create"}
        initial={dialog?.initial ?? null}
        templates={allTemplates}
        images={allImages}
        devices={allDevices}
        scripts={allScripts}
        proxies={allProxies}
        onClose={() => setDialog(null)}
        onSaved={handleSaved}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete template?"
        description={deleteTarget ? `“${deleteTarget.name}” will be removed from the blueprint catalogue.` : undefined}
        confirmLabel="Delete template"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
      >
        <p>
          AVDs already deployed from this template keep their current configuration and continue to
          run. New deployments will not be able to use it. This action cannot be undone.
        </p>
      </ConfirmDialog>
    </div>
  );
}
