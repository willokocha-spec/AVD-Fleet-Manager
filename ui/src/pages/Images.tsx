import { useMemo, useState } from "react";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  InlineNotice,
  PageHeader,
  ProgressBar,
  SearchBox,
  Select,
  StatusPill,
  Table,
  useToast,
  type BadgeTone,
  type Column,
} from "@/lib/components";
import { useAsync, useDebouncedValue } from "@/lib/hooks";
import type { PageMeta } from "@/lib/page-meta";
import type { OobeRecord, OobeStatus, SystemImage } from "@/lib/types";
import { formatBytes, formatDateTime, formatDuration, humanize } from "@/lib/utils";

import {
  EntityCell,
  Mono,
  SubText,
  errorMessage,
  styles,
} from "@/pages/_components/catalog/common";
import {
  DownloadImageDialog,
  ImportImageDialog,
  OobeMatrixPanel,
  VerifyOobeDialog,
} from "@/pages/_components/catalog/image-panels";

export const meta: PageMeta = {
  path: "/images",
  title: "Images",
  icon: "images",
  nav: 30,
  description: "System image catalogue, downloads and OOBE evidence",
};

/* -------------------------------------------------------------------------- */
/* Image helpers                                                               */
/* -------------------------------------------------------------------------- */

function imageTagInfo(image: SystemImage): { label: string; tone: BadgeTone } {
  switch (image.kind) {
    case "google_apis":
      return { label: "Google APIs", tone: "info" };
    case "google_apis_playstore":
      return { label: "Google Play", tone: "accent" };
    case "aosp":
      return { label: "AOSP", tone: "neutral" };
    case "custom":
      return { label: "Custom", tone: "warning" };
    case "default":
      return { label: "Android", tone: "info" };
    default:
      return { label: image.tag ? humanize(image.tag) : "Unknown", tone: "neutral" };
  }
}

function tagFilterLabel(value: string): string {
  switch (value) {
    case "google_apis":
      return "Google APIs";
    case "google_apis_playstore":
      return "Google Play";
    case "aosp":
      return "AOSP";
    case "custom":
      return "Custom";
    case "default":
      return "Android";
    default:
      return humanize(value);
  }
}

function tagValue(image: SystemImage): string {
  return image.tag ?? image.kind ?? "unknown";
}

function isPlayImage(image: SystemImage): boolean {
  return (
    image.kind === "google_apis_playstore" ||
    /playstore|play/i.test(`${image.tag ?? ""} ${image.id} ${image.package ?? ""}`)
  );
}

function oobeStatusFor(image: SystemImage, records: OobeRecord[]): OobeStatus {
  const record = records.find((candidate) => candidate.image_id === image.id);
  return record?.status ?? image.oobe_status ?? "unverified";
}

function oobeRecordFor(image: SystemImage, records: OobeRecord[]): OobeRecord | undefined {
  return records.find((candidate) => candidate.image_id === image.id);
}

function oobeStatusLabel(status: OobeStatus): string {
  return status === "unsupported" ? "Unsupported" : humanize(status);
}

function oobeTooltip(record: OobeRecord | undefined, status: OobeStatus): string {
  const lines: string[] = [`OOBE ${oobeStatusLabel(status)}`];
  if (record?.checked_at) lines.push(`Checked ${formatDateTime(record.checked_at)}`);
  if (record?.checked_by) lines.push(`Checked by ${record.checked_by}`);
  if (record?.activity) lines.push(`Activity ${record.activity}`);
  if (record?.screenshot_path) lines.push(`Screenshot ${record.screenshot_path}`);
  if (record?.boot_seconds !== null && record?.boot_seconds !== undefined) {
    lines.push(`Boot ${formatDuration(record.boot_seconds)}`);
  }
  if (record?.reset_seconds !== null && record?.reset_seconds !== undefined) {
    lines.push(`Reset ${formatDuration(record.reset_seconds)}`);
  }
  if (record?.evidence?.length) lines.push(`Evidence ${record.evidence.join(", ")}`);
  if (record?.notes) lines.push(`Notes ${record.notes}`);
  if (!record) lines.push("No OOBE record for this image in the matrix.");
  return lines.join("\n");
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function ImagesPage() {
  const toast = useToast();
  const images = useAsync(() => api.listImages({ include_remote: true, installed_only: false }), []);
  const oobe = useAsync(() => api.getOobeMatrix(), []);

  const [refreshing, setRefreshing] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [preview, setPreview] = useState<{ image: SystemImage; dryRun: SystemImage } | null>(null);
  const [previewLoadingId, setPreviewLoadingId] = useState<string | null>(null);
  const [installing, setInstalling] = useState<SystemImage | null>(null);
  const [verifyImage, setVerifyImage] = useState<SystemImage | null>(null);

  const [query, setQuery] = useState("");
  const debouncedQuery = useDebouncedValue(query);
  const [apiFilter, setApiFilter] = useState("all");
  const [tagFilter, setTagFilter] = useState("all");
  const [abiFilter, setAbiFilter] = useState("all");
  const [installFilter, setInstallFilter] = useState("all");

  const allImages = useMemo(() => images.data ?? [], [images.data]);
  const records = oobe.data?.records ?? [];

  const apiLevels = useMemo(
    () =>
      Array.from(
        new Set(
          allImages
            .map((image) => image.api_level)
            .filter((level): level is number => typeof level === "number"),
        ),
      ).sort((left, right) => right - left),
    [allImages],
  );
  const tagValues = useMemo(
    () => Array.from(new Set(allImages.map(tagValue))).sort((left, right) => left.localeCompare(right)),
    [allImages],
  );
  const abiValues = useMemo(
    () =>
      Array.from(new Set(allImages.map((image) => image.abi).filter((abi): abi is string => Boolean(abi)))).sort(
        (left, right) => left.localeCompare(right),
      ),
    [allImages],
  );

  const filtered = useMemo(() => {
    const needle = debouncedQuery.trim().toLowerCase();
    return allImages.filter((image) => {
      if (needle) {
        const haystack = [image.display_name, image.id, image.package, image.tag, image.abi, image.local_path]
          .filter((value): value is string => typeof value === "string")
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(needle)) return false;
      }
      if (apiFilter !== "all" && String(image.api_level ?? "") !== apiFilter) return false;
      if (tagFilter !== "all" && tagValue(image) !== tagFilter) return false;
      if (abiFilter !== "all" && image.abi !== abiFilter) return false;
      if (installFilter === "installed" && image.installed !== true) return false;
      if (installFilter === "available" && image.installed === true) return false;
      return true;
    });
  }, [allImages, debouncedQuery, apiFilter, tagFilter, abiFilter, installFilter]);

  const installedCount = allImages.filter((image) => image.installed === true).length;
  const verifiedCount = allImages.filter((image) => oobeStatusFor(image, records) === "verified").length;
  const unverifiedPlayImages = allImages.filter(
    (image) => isPlayImage(image) && oobeStatusFor(image, records) !== "verified",
  );

  const filtersActive =
    debouncedQuery.trim().length > 0 ||
    apiFilter !== "all" ||
    tagFilter !== "all" ||
    abiFilter !== "all" ||
    installFilter !== "all";

  const resetFilters = (): void => {
    setQuery("");
    setApiFilter("all");
    setTagFilter("all");
    setAbiFilter("all");
    setInstallFilter("all");
  };

  const refreshCatalogue = async (): Promise<void> => {
    setRefreshing(true);
    try {
      const refreshed = await api.refreshImages();
      images.setData(refreshed);
      toast.success("Catalogue refreshed", `${refreshed.length} system images discovered.`);
      oobe.reload();
    } catch (caught) {
      toast.error("Could not refresh the catalogue", errorMessage(caught));
    } finally {
      setRefreshing(false);
    }
  };

  const startDownload = async (image: SystemImage): Promise<void> => {
    setPreviewLoadingId(image.id);
    try {
      const dryRun = await api.downloadImage(image.id, { dry_run: true });
      setPreview({ image, dryRun });
    } catch (caught) {
      toast.error(`Could not prepare ${image.display_name ?? image.id}`, errorMessage(caught));
    } finally {
      setPreviewLoadingId(null);
    }
  };

  const confirmDownload = async (): Promise<void> => {
    if (!preview) return;
    const target = preview.image;
    setPreview(null);
    setInstalling(target);
    try {
      await api.downloadImage(target.id, { dry_run: false });
      toast.success("Image installed", `${target.display_name ?? target.id} is now available to templates.`);
    } catch (caught) {
      toast.error("Image install failed", errorMessage(caught));
    } finally {
      setInstalling(null);
      images.reload();
    }
  };

  const handleImported = (image: SystemImage): void => {
    toast.success(
      "Custom image imported",
      `${image.display_name ?? image.id} was registered as unverified. Run OOBE verification before Play-compatible use.`,
    );
    images.reload();
  };

  const handleVerified = (record: OobeRecord): void => {
    const description = record.activity
      ? `Activity ${record.activity}${record.screenshot_path ? ` · ${record.screenshot_path}` : ""}`
      : undefined;
    if (record.status === "verified") {
      toast.success("OOBE verified", description);
    } else {
      toast.warning(`OOBE ${humanize(record.status)}`, description ?? record.notes);
    }
    images.reload();
    oobe.reload();
  };

  const verifyFromMatrix = (imageId: string): void => {
    const image = allImages.find((candidate) => candidate.id === imageId);
    if (image) setVerifyImage(image);
  };

  const columns: Column<SystemImage>[] = [
    {
      key: "image",
      header: "Image",
      sortable: true,
      sortValue: (image) => image.display_name ?? image.id,
      render: (image) => (
        <EntityCell
          title={image.display_name ?? image.id}
          subtitle={
            <>
              {image.id}
              {image.package ? (
                <>
                  {" · "}
                  <Mono>{image.package}</Mono>
                </>
              ) : null}
            </>
          }
          subtitleTitle={image.local_path ?? image.package ?? undefined}
        />
      ),
    },
    {
      key: "api",
      header: "API",
      sortable: true,
      sortValue: (image) => image.api_level ?? null,
      render: (image) => <Badge tone="info">API {image.api_level ?? "—"}</Badge>,
    },
    {
      key: "tag",
      header: "Tag",
      sortable: true,
      sortValue: (image) => tagValue(image),
      render: (image) => {
        const info = imageTagInfo(image);
        return (
          <div style={styles.stackTight}>
            <Badge tone={info.tone}>{info.label}</Badge>
            {image.tag && image.tag !== info.label ? <SubText>{image.tag}</SubText> : null}
          </div>
        );
      },
    },
    {
      key: "abi",
      header: "ABI",
      sortable: true,
      sortValue: (image) => image.abi ?? "",
      render: (image) => <Mono>{image.abi ?? "—"}</Mono>,
    },
    {
      key: "size",
      header: "Size",
      align: "right",
      sortable: true,
      sortValue: (image) => image.size_bytes ?? null,
      render: (image) => formatBytes(image.size_bytes),
    },
    {
      key: "installed",
      header: "Local",
      sortable: true,
      sortValue: (image) => (image.installed ? 0 : 1),
      render: (image) => (
        <div style={styles.stackTight}>
          <Badge tone={image.installed ? "success" : "neutral"} dot>
            {image.installed ? "Installed" : "Remote"}
          </Badge>
          {image.local_path ? (
            <SubText title={image.local_path}>local copy present</SubText>
          ) : (
            <SubText>not downloaded</SubText>
          )}
        </div>
      ),
    },
    {
      key: "oobe",
      header: "OOBE",
      sortable: true,
      sortValue: (image) => oobeStatusFor(image, records),
      render: (image) => {
        const status = oobeStatusFor(image, records);
        const record = oobeRecordFor(image, records);
        return (
          <div style={styles.stackTight}>
            <span title={oobeTooltip(record, status)}>
              <StatusPill status={status} label={oobeStatusLabel(status)} />
            </span>
            {isPlayImage(image) && status !== "verified" ? (
              <Badge tone="warning" icon="alert-triangle">
                Play image not verified
              </Badge>
            ) : null}
          </div>
        );
      },
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (image) => (
        <div style={styles.actions}>
          {!image.installed ? (
            <Button
              size="sm"
              variant="secondary"
              icon="download"
              loading={previewLoadingId === image.id || installing?.id === image.id}
              disabled={installing !== null}
              onClick={() => void startDownload(image)}
            >
              Download
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            icon="shield"
            disabled={!image.installed}
            title={
              image.installed
                ? "Boot a temporary AVD and record setup-wizard evidence."
                : "Download and install the image before verifying OOBE."
            }
            onClick={() => setVerifyImage(image)}
          >
            Verify OOBE
          </Button>
        </div>
      ),
    },
  ];

  const emptyState =
    allImages.length === 0 && !images.loading && !images.error ? (
      <EmptyState
        icon="images"
        title="No system images found"
        description="Run the environment doctor to locate the Android SDK, refresh the catalogue, or import a custom image directory."
        action={
          <div style={styles.rowTight}>
            <Button variant="secondary" icon="refresh" onClick={() => void refreshCatalogue()}>
              Refresh catalogue
            </Button>
            <Button variant="primary" icon="upload" onClick={() => setImportOpen(true)}>
              Import image
            </Button>
          </div>
        }
      />
    ) : (
      <EmptyState
        compact
        icon="filter"
        title="No images match these filters"
        description="Adjust the API level, tag or ABI filters, or clear the search."
        action={
          <Button size="sm" variant="secondary" icon="close" onClick={resetFilters}>
            Clear filters
          </Button>
        }
      />
    );

  return (
    <div className="page">
      <PageHeader
        title="Images"
        eyebrow="System images"
        description="Browse the stock Google APIs, Google Play and AOSP catalogue, download images and import custom builds."
        meta={
          <div style={styles.rowTight}>
            <Badge tone="info" icon="images">
              {allImages.length} images
            </Badge>
            <Badge tone="success" icon="download">
              {installedCount} installed
            </Badge>
            <StatusPill
              status={verifiedCount > 0 ? "verified" : "unverified"}
              label={verifiedCount > 0 ? `${verifiedCount} OOBE verified` : "No OOBE evidence"}
            />
            {unverifiedPlayImages.length > 0 ? (
              <StatusPill
                status="unverified"
                label={`${unverifiedPlayImages.length} Play image${unverifiedPlayImages.length === 1 ? "" : "s"} unverified`}
              />
            ) : null}
          </div>
        }
        actions={
          <>
            <Button variant="secondary" icon="refresh" loading={refreshing} onClick={() => void refreshCatalogue()}>
              Refresh catalogue
            </Button>
            <Button variant="primary" icon="upload" onClick={() => setImportOpen(true)}>
              Import image
            </Button>
          </>
        }
      />

      {images.error ? (
        <InlineNotice
          tone="danger"
          title="Could not load the image catalogue"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={images.reload}>
              Retry
            </Button>
          }
        >
          <p>{errorMessage(images.error)}</p>
        </InlineNotice>
      ) : null}

      {unverifiedPlayImages.length > 0 ? (
        <InlineNotice tone="warning" title="Google Play images must be OOBE-verified">
          <p>
            {unverifiedPlayImages
              .map((image) => image.display_name ?? image.id)
              .join(", ")}{" "}
            {unverifiedPlayImages.length === 1 ? "is" : "are"} not verified on this host. Play images
            often fail first-run setup in automation; run <strong>Verify OOBE</strong> and record the
            setup-wizard evidence before deploying them.
          </p>
        </InlineNotice>
      ) : null}

      <Card
        title="Image catalogue"
        description={`${filtered.length} of ${allImages.length} images · ${installedCount} installed locally · ${verifiedCount} OOBE verified`}
        icon="images"
      >
        <div style={styles.stack}>
          {installing ? (
            <InlineNotice
              tone="info"
              title={`Installing ${installing.display_name ?? installing.id}`}
              action={<Badge tone="warning">Long operation</Badge>}
            >
              <p>
                Downloading and extracting the system image into the Android SDK. This can take several
                minutes; the catalogue is refreshed automatically when it finishes.
              </p>
              <ProgressBar indeterminate label="Installing system image" />
            </InlineNotice>
          ) : null}

          <div style={styles.toolbar}>
            <div style={{ flex: "1 1 240px", minWidth: "220px" }}>
              <SearchBox
                value={query}
                onChange={setQuery}
                placeholder="Search name, package, ID or path…"
                label="Search images"
              />
            </div>
            <div style={{ width: "132px" }}>
              <Field label="API level">
                <Select value={apiFilter} onChange={(event) => setApiFilter(event.target.value)}>
                  <option value="all">All APIs</option>
                  {apiLevels.map((level) => (
                    <option key={level} value={String(level)}>
                      API {level}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <div style={{ width: "158px" }}>
              <Field label="Tag">
                <Select value={tagFilter} onChange={(event) => setTagFilter(event.target.value)}>
                  <option value="all">All tags</option>
                  {tagValues.map((value) => (
                    <option key={value} value={value}>
                      {tagFilterLabel(value)}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <div style={{ width: "132px" }}>
              <Field label="ABI">
                <Select value={abiFilter} onChange={(event) => setAbiFilter(event.target.value)}>
                  <option value="all">All ABIs</option>
                  {abiValues.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <div style={{ width: "148px" }}>
              <Field label="Installed">
                <Select value={installFilter} onChange={(event) => setInstallFilter(event.target.value)}>
                  <option value="all">All images</option>
                  <option value="installed">Installed only</option>
                  <option value="available">Not installed</option>
                </Select>
              </Field>
            </div>
            <Button variant="ghost" icon="close" disabled={!filtersActive} onClick={resetFilters}>
              Reset
            </Button>
          </div>

          <Table
            columns={columns}
            rows={filtered}
            rowKey={(image) => image.id}
            loading={images.loading && allImages.length === 0}
            skeletonRows={4}
            caption="System image catalogue"
            empty={emptyState}
          />
        </div>
      </Card>

      <OobeMatrixPanel
        matrix={oobe.data}
        loading={oobe.loading}
        error={oobe.error}
        onRetry={oobe.reload}
        onVerify={allImages.length > 0 ? verifyFromMatrix : undefined}
      />

      <ImportImageDialog open={importOpen} onClose={() => setImportOpen(false)} onImported={handleImported} />
      <DownloadImageDialog
        open={preview !== null}
        image={preview?.image ?? null}
        preview={preview?.dryRun ?? null}
        onClose={() => setPreview(null)}
        onConfirm={() => void confirmDownload()}
      />
      <VerifyOobeDialog
        open={verifyImage !== null}
        image={verifyImage}
        onClose={() => setVerifyImage(null)}
        onVerified={handleVerified}
      />
    </div>
  );
}
