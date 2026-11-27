/**
 * Profiles tab.
 *
 * Built-in profiles are read-only on the API (edit → 422, delete → 409), so the
 * UI disables those controls with an explanation and offers `Duplicate`
 * (`POST /profiles/{id}/clone`) as the supported path to an editable copy.
 */
import { useMemo, useState } from "react";

import { instrumentationApi } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  Field,
  IconButton,
  InlineNotice,
  Input,
  Table,
  useToast,
  type Column,
} from "@/lib/components";
import { useAsync } from "@/lib/hooks";
import type { InstrumentationProfile } from "@/lib/types";
import { formatDateTime, pluralize } from "@/lib/utils";

import { ProfileEditorDialog } from "@/pages/_components/instrumentation/ProfileEditor";
import {
  apiErrorText,
  profileWarnings,
  stepActionMeta,
  styles,
} from "@/pages/_components/instrumentation/shared";

export function ProfilesTab() {
  const toast = useToast();
  const profiles = useAsync(() => instrumentationApi.listProfiles(), []);
  const modules = useAsync(() => instrumentationApi.listModules(), []);

  const [query, setQuery] = useState("");
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<"create" | "edit">("create");
  const [editing, setEditing] = useState<InstrumentationProfile | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<InstrumentationProfile | null>(null);
  const [cloningId, setCloningId] = useState<string | null>(null);

  const allProfiles = useMemo(() => profiles.data?.profiles ?? [], [profiles.data]);
  const allModules = useMemo(() => modules.data?.modules ?? [], [modules.data]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return allProfiles;
    return allProfiles.filter((profile) =>
      [profile.name, profile.description, ...profile.steps.map((step) => step.action)]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [allProfiles, query]);

  const openCreate = (): void => {
    setEditing(null);
    setEditorMode("create");
    setEditorOpen(true);
  };

  const openEdit = (profile: InstrumentationProfile): void => {
    setEditing(profile);
    setEditorMode("edit");
    setEditorOpen(true);
  };

  const handleSaved = (profile: InstrumentationProfile, mode: "create" | "edit"): void => {
    toast.success(
      mode === "create" ? "Profile created" : "Profile saved",
      `${profile.name} · revision ${profile.revision} · ${pluralize(profile.steps.length, "step")}`,
    );
    setEditorOpen(false);
    setEditing(null);
    profiles.reload();
  };

  const duplicate = async (profile: InstrumentationProfile): Promise<void> => {
    setCloningId(profile.id);
    try {
      const clone = await instrumentationApi.cloneProfile(profile.id);
      toast.success(
        "Profile duplicated",
        `${clone.name} · bind a module on the steps that need one, then save.`,
      );
      profiles.reload();
      setEditing(clone);
      setEditorMode("edit");
      setEditorOpen(true);
    } catch (caught) {
      toast.error("Could not duplicate the profile", apiErrorText(caught));
    } finally {
      setCloningId(null);
    }
  };

  const deleteProfile = async (): Promise<void> => {
    if (!deleteTarget) return;
    try {
      await instrumentationApi.deleteProfile(deleteTarget.id);
      toast.success("Profile deleted", deleteTarget.name);
      setDeleteTarget(null);
      profiles.reload();
    } catch (caught) {
      toast.error("Could not delete the profile", apiErrorText(caught));
    }
  };

  const columns: Column<InstrumentationProfile>[] = [
    {
      key: "name",
      header: "Profile",
      sortable: true,
      sortValue: (profile) => profile.name,
      render: (profile) => {
        const warnings = profileWarnings(profile);
        return (
          <div style={styles.stackTight}>
            <div style={styles.rowTight}>
              <span style={{ fontWeight: 600, color: "var(--text)" }}>{profile.name}</span>
              {profile.builtin ? (
                <Badge tone="accent" icon="shield" title="Ships with AVD Fleet; read-only.">
                  Built-in
                </Badge>
              ) : (
                <Badge tone="neutral">Custom</Badge>
              )}
              {warnings.length > 0 ? (
                <Badge
                  tone="warning"
                  icon="alert-triangle"
                  title={warnings.join("\n")}
                >
                  Step warning
                </Badge>
              ) : null}
            </div>
            {profile.description ? (
              <span className="muted small" style={{ maxWidth: "60ch" }}>
                {profile.description}
              </span>
            ) : null}
            <code style={styles.code}>{profile.id}</code>
          </div>
        );
      },
    },
    {
      key: "steps",
      header: "Steps",
      width: "280px",
      render: (profile) => (
        <div style={styles.stackTight}>
          <span>{pluralize(profile.steps.length, "step")}</span>
          <span className="muted small">
            {profile.steps.map((step) => stepActionMeta(step.action).label).join(" → ")}
          </span>
        </div>
      ),
    },
    {
      key: "revision",
      header: "Revision",
      width: "100px",
      sortable: true,
      sortValue: (profile) => profile.revision,
      render: (profile) => <Badge tone="neutral">r{profile.revision}</Badge>,
    },
    {
      key: "updated",
      header: "Updated",
      width: "170px",
      sortable: true,
      sortValue: (profile) => profile.updated_at,
      render: (profile) => (
        <span className="muted small">{formatDateTime(profile.updated_at)}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      width: "130px",
      render: (profile) => (
        <div style={styles.actions}>
          <IconButton
            label={`Edit ${profile.name}`}
            icon="edit"
            size="sm"
            disabled={profile.builtin}
            style={profile.builtin ? styles.controlDisabled : undefined}
            title={
              profile.builtin
                ? "Built-in profiles are read-only — the API rejects edits with HTTP 422. Duplicate it first."
                : `Edit ${profile.name}`
            }
            onClick={() => openEdit(profile)}
          />
          <IconButton
            label={`Duplicate ${profile.name}`}
            icon="copy"
            size="sm"
            loading={cloningId === profile.id}
            onClick={() => void duplicate(profile)}
          />
          <IconButton
            label={`Delete ${profile.name}`}
            icon="trash"
            size="sm"
            variant="danger"
            disabled={profile.builtin}
            style={profile.builtin ? styles.controlDisabled : undefined}
            title={
              profile.builtin
                ? "Built-in profiles cannot be deleted — the API rejects deletes with HTTP 409."
                : `Delete ${profile.name}`
            }
            onClick={() => setDeleteTarget(profile)}
          />
        </div>
      ),
    },
  ];

  return (
    <div style={styles.stack}>
      <InlineNotice tone="info" title="Built-in profiles are read-only">
        <p>
          The daemon ships five built-in profiles: <strong>Root</strong>, <strong>Magisk + Zygisk</strong>,{" "}
          <strong>LSPosed</strong>, <strong>Xposed Instrumentation</strong> and <strong>Custom</strong>. The API
          rejects edits with HTTP 422 and deletes with HTTP 409, so both controls are disabled here. Use{" "}
          <strong>Duplicate</strong> to get an editable copy, then bind a library module on the steps that need
          one.
        </p>
        <p>
          A module step (<code>install_module</code>, <code>enable_module</code>, <code>disable_module</code>,{" "}
          <code>remove_module</code>) with no module bound will fail at deploy time. An{" "}
          <code>install_lsposed</code> step is different: the backend resolves the newest LSPosed ZIP from the
          module library, so it works once that library holds one (acquire it on the Root &amp; Magisk or
          LSPosed &amp; Zygisk tab). With an empty library it fails with “no LSPosed is available in the module
          library”. The run result reports the step failure instead of a fake success.
        </p>
      </InlineNotice>

      <Card
        title="Profiles"
        icon="templates"
        description="GET /instrumentation/profiles. Built-in profiles appear first; custom profiles can be edited and deleted."
        actions={
          <div style={styles.rowTight}>
            <IconButton
              label="Reload profiles"
              icon="refresh"
              size="sm"
              loading={profiles.loading}
              onClick={profiles.reload}
            />
            <Button variant="primary" icon="plus" onClick={openCreate}>
              New profile
            </Button>
          </div>
        }
      >
        <div style={styles.stack}>
          {profiles.error ? (
            <InlineNotice
              tone="danger"
              title="Could not load instrumentation profiles"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={profiles.reload}>
                  Retry
                </Button>
              }
            >
              <p>{apiErrorText(profiles.error)}</p>
            </InlineNotice>
          ) : null}

          {modules.error ? (
            <InlineNotice
              tone="warning"
              title="Could not load the module library"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={modules.reload}>
                  Retry
                </Button>
              }
            >
              <p>{apiErrorText(modules.error)}</p>
              <p>Steps that need a module cannot be bound until the library loads.</p>
            </InlineNotice>
          ) : null}

          <div style={styles.toolbar}>
            <div style={{ flex: "1 1 260px", maxWidth: "380px" }}>
              <Field label="Search">
                <Input
                  type="search"
                  value={query}
                  placeholder="Name, description or step action…"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </Field>
            </div>
            <span className="muted small">
              {filtered.length} of {allProfiles.length} profiles
              {profiles.loading ? " · refreshing…" : ""}
            </span>
          </div>

          <Table
            columns={columns}
            rows={filtered}
            rowKey={(profile) => profile.id}
            loading={profiles.loading && allProfiles.length === 0}
            skeletonRows={4}
            caption="Instrumentation profiles"
            empty={
              profiles.error && allProfiles.length === 0 ? (
                <EmptyState
                  compact
                  icon="alert-circle"
                  title="Profiles unavailable"
                  description="The profile request failed; retry it above. No profile data is being shown."
                />
              ) : (
                <EmptyState
                  compact
                  icon="templates"
                  title={query.trim() ? "No profiles match" : "No profiles"}
                  description={
                    query.trim()
                      ? `Nothing matches “${query}”.`
                      : "Create a profile to deploy root, Magisk, Zygisk, LSPosed and library modules in one run."
                  }
                  action={
                    query.trim() ? (
                      <Button size="sm" variant="secondary" icon="close" onClick={() => setQuery("")}>
                        Clear search
                      </Button>
                    ) : (
                      <Button size="sm" variant="primary" icon="plus" onClick={openCreate}>
                        New profile
                      </Button>
                    )
                  }
                />
              )
            }
          />
        </div>
      </Card>

      <ProfileEditorDialog
        open={editorOpen}
        mode={editorMode}
        initial={editing}
        modules={allModules}
        onClose={() => {
          setEditorOpen(false);
          setEditing(null);
        }}
        onSaved={handleSaved}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this profile?"
        description={deleteTarget ? `${deleteTarget.name} · r${deleteTarget.revision}` : undefined}
        confirmLabel="Delete profile"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={deleteProfile}
      >
        <p>
          The profile and its steps are removed. Deployment runs that used it stay in the run history with their
          results; the run&apos;s profile reference simply cannot be resolved any more.
        </p>
      </ConfirmDialog>
    </div>
  );
}
