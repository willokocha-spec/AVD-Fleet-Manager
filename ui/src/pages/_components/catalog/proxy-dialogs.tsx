/**
 * Proxy pool dialogs: add, bulk import, edit metadata and guest-egress
 * verification. Page-private to the Proxies page.
 *
 * Passwords are write-only: they may be typed into a password field or embedded
 * in a pasted URL, but they are never rendered back. Preview and error text is
 * sanitised with `redactProxySecrets`.
 */
import { useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import {
  Badge,
  Button,
  Checkbox,
  Field,
  InlineNotice,
  Input,
  Modal,
  Select,
  StatusPill,
  Textarea,
} from "@/lib/components";
import type { AvdInfo, ProxyImportResult, ProxyTestResult, ProxyView } from "@/lib/types";
import { formatDateTime } from "@/lib/utils";

import { DetailList, Hint, Mono, SubText, errorMessage, styles } from "./common";
import {
  countImportEntries,
  parseProxyInput,
  parseTagsInput,
  proxyEndpoint,
  proxyErrorMessage,
  proxyHostPort,
  redactProxySecrets,
} from "./proxy-utils";

/* -------------------------------------------------------------------------- */
/* Add single proxy                                                            */
/* -------------------------------------------------------------------------- */

export function AddProxyDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (proxy: ProxyView) => void;
}) {
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [tags, setTags] = useState("");
  const [password, setPassword] = useState("");
  const [urlError, setUrlError] = useState<string | undefined>();
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setUrl("");
    setName("");
    setTags("");
    setPassword("");
    setUrlError(undefined);
    setFormError(null);
    setSubmitting(false);
  }, [open]);

  const parsed = useMemo(() => {
    const trimmed = url.trim();
    if (!trimmed) return null;
    return parseProxyInput(trimmed);
  }, [url]);

  const preview = parsed && parsed.ok ? parsed.value : null;
  const passwordProvided = password.trim().length > 0;

  const submit = async (): Promise<void> => {
    const trimmed = url.trim();
    if (!trimmed) {
      setUrlError("Enter a proxy URL.");
      return;
    }
    const result = parseProxyInput(trimmed);
    if (!result.ok) {
      setUrlError(result.error);
      return;
    }
    setUrlError(undefined);
    setFormError(null);
    setSubmitting(true);
    try {
      const created = await api.createProxy({
        scheme: result.value.scheme,
        host: result.value.host,
        port: result.value.port,
        username: result.value.username,
        password: passwordProvided ? password : result.value.password,
        tags: parseTagsInput(tags),
        ...(name.trim() ? { name: name.trim() } : {}),
      });
      onCreated(created);
      onClose();
    } catch (caught) {
      setFormError(proxyErrorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add proxy"
      description="Add one authenticated HTTP, HTTPS or SOCKS5 proxy. Credentials are stored in the encrypted vault."
      size="md"
      dismissible={!submitting}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="primary" icon="plus" loading={submitting} onClick={() => void submit()}>
            Add proxy
          </Button>
        </>
      }
    >
      <div style={styles.stack}>
        {formError ? (
          <InlineNotice tone="danger" title="Could not add the proxy">
            <p>{formError}</p>
          </InlineNotice>
        ) : null}

        <Field
          label="Proxy URL"
          required
          error={urlError}
          hint="scheme://user:pass@host:port — a missing scheme defaults to http."
        >
          <Input
            value={url}
            leadingIcon="link"
            placeholder="http://user:pass@proxy.example.net:8080"
            invalid={Boolean(urlError)}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => {
              setUrl(event.target.value);
              setUrlError(undefined);
            }}
          />
        </Field>

        {parsed && !parsed.ok ? (
          <InlineNotice tone="warning" title="URL not understood">
            <p>{parsed.error}</p>
          </InlineNotice>
        ) : null}

        {preview ? (
          <div style={styles.previewBox}>
            <div style={styles.rowBetween}>
              <strong style={{ fontSize: "var(--fs-sm)" }}>Parsed preview</strong>
              <Badge tone="info">{preview.scheme.toUpperCase()}</Badge>
            </div>
            <DetailList
              items={[
                { label: "Endpoint", value: <Mono>{proxyEndpoint(preview)}</Mono> },
                { label: "Host", value: <Mono>{preview.host}</Mono> },
                { label: "Port", value: <Mono>{preview.port}</Mono> },
                { label: "Username", value: preview.username ? <Mono>{preview.username}</Mono> : "—" },
                {
                  label: "Credentials",
                  value:
                    passwordProvided || preview.hasCredentials ? (
                      <Badge tone="success" icon="key">
                        Stored in vault (hidden)
                      </Badge>
                    ) : (
                      <Badge tone="neutral">No credentials</Badge>
                    ),
                },
              ]}
            />
            <Hint>
              The password is never echoed back by the API. The URL is shown without its credential
              segment everywhere else in the console.
            </Hint>
          </div>
        ) : null}

        <div style={styles.fieldGridTwo}>
          <Field label="Name" hint="Optional friendly label, defaults to the host.">
            <Input
              value={name}
              placeholder="EU egress 01"
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field label="Tags" hint="Comma-separated labels, e.g. eu, http.">
            <Input
              value={tags}
              placeholder="eu, http"
              onChange={(event) => setTags(event.target.value)}
            />
          </Field>
        </div>

        <Field
          label="Password (optional)"
          hint="Only needed when the URL does not contain one. Sent once and stored write-only."
        >
          <Input
            value={password}
            type="password"
            autoComplete="new-password"
            placeholder="••••••••"
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* Bulk import                                                                 */
/* -------------------------------------------------------------------------- */

export function ImportProxiesDialog({
  open,
  onClose,
  onImported,
}: {
  open: boolean;
  onClose: () => void;
  onImported: (result: ProxyImportResult) => void;
}) {
  const [text, setText] = useState("");
  const [textError, setTextError] = useState<string | undefined>();
  const [result, setResult] = useState<ProxyImportResult | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setText("");
    setTextError(undefined);
    setResult(null);
    setFormError(null);
    setSubmitting(false);
  }, [open]);

  const entries = useMemo(() => countImportEntries(text), [text]);

  const submit = async (): Promise<void> => {
    if (entries === 0) {
      setTextError("Add at least one proxy URL, one per line.");
      return;
    }
    setTextError(undefined);
    setFormError(null);
    setSubmitting(true);
    try {
      const imported = await api.importProxies({ text, source: "paste" });
      setResult(imported);
      onImported(imported);
    } catch (caught) {
      setFormError(proxyErrorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  const errorCount = result?.errors?.length ?? 0;
  const addedCount = result?.added.length ?? 0;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Bulk import proxies"
      description="Paste one proxy URL per line. Blank lines and # comments are skipped."
      size="md"
      dismissible={!submitting}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            {result ? "Done" : "Cancel"}
          </Button>
          <Button variant="primary" icon="upload" loading={submitting} onClick={() => void submit()}>
            Import proxies
          </Button>
        </>
      }
    >
      <div style={styles.stack}>
        {formError ? (
          <InlineNotice tone="danger" title="Import failed">
            <p>{formError}</p>
          </InlineNotice>
        ) : null}

        <Field
          label="Proxy URLs"
          required
          error={textError}
          hint={`${entries} importable ${entries === 1 ? "line" : "lines"}. Accepted schemes: http, https, socks5.`}
        >
          <Textarea
            value={text}
            rows={7}
            invalid={Boolean(textError)}
            spellCheck={false}
            placeholder={
              "http://user:pass@proxy-eu-01.example.net:8080\nsocks5://user:pass@proxy-us-01.example.net:1080\n# comments and blank lines are skipped"
            }
            onChange={(event) => {
              setText(event.target.value);
              setTextError(undefined);
            }}
          />
        </Field>

        <Hint>
          Credentials are extracted on the daemon and stored in the encrypted vault. Import errors are
          shown with passwords redacted.
        </Hint>

        {result ? (
          <div style={styles.stackTight}>
            <div style={styles.rowBetween}>
              <strong style={{ fontSize: "var(--fs-sm)" }}>Import result</strong>
              <div style={styles.rowTight}>
                <Badge tone="success">{addedCount} added</Badge>
                <Badge tone="neutral">{result.skipped ?? 0} skipped</Badge>
                <Badge tone={errorCount > 0 ? "warning" : "neutral"}>{errorCount} errors</Badge>
              </div>
            </div>

            {addedCount > 0 ? (
              <div style={styles.scrollBox}>
                {result.added.map((proxy) => (
                  <div key={proxy.id} style={styles.rowBetween}>
                    <span style={styles.rowTight}>
                      <Badge tone="info">{(proxy.scheme ?? "http").toUpperCase()}</Badge>
                      <Mono>{proxyHostPort(proxy)}</Mono>
                      {proxy.username ? <SubText>{proxy.username}</SubText> : null}
                    </span>
                    <SubText>{proxy.tags?.length ? proxy.tags.join(", ") : "no tags"}</SubText>
                  </div>
                ))}
              </div>
            ) : (
              <InlineNotice tone="warning" title="Nothing was imported">
                <p>No proxy lines could be imported. Fix the errors below and try again.</p>
              </InlineNotice>
            )}

            {errorCount > 0 ? (
              <div style={styles.scrollBox} role="alert">
                {result.errors?.map((message, index) => (
                  <SubText key={`${message}-${index}`}>{redactProxySecrets(message)}</SubText>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* Edit proxy metadata                                                         */
/* -------------------------------------------------------------------------- */

export function EditProxyDialog({
  open,
  proxy,
  onClose,
  onSaved,
}: {
  open: boolean;
  proxy: ProxyView | null;
  onClose: () => void;
  onSaved: (proxy: ProxyView) => void;
}) {
  const [name, setName] = useState("");
  const [tags, setTags] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open || !proxy) return;
    setName(proxy.name ?? "");
    setTags((proxy.tags ?? []).join(", "));
    setEnabled(proxy.enabled !== false);
    setFormError(null);
    setSaving(false);
  }, [open, proxy]);

  if (!proxy) return null;

  const submit = async (): Promise<void> => {
    setFormError(null);
    setSaving(true);
    try {
      const updated = await api.updateProxy(proxy.id, {
        name: name.trim(),
        tags: parseTagsInput(tags),
        enabled,
      });
      onSaved(updated);
      onClose();
    } catch (caught) {
      setFormError(proxyErrorMessage(caught));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Edit proxy"
      description="Change the label, tags or enablement. Credentials cannot be read back from the vault."
      size="sm"
      dismissible={!saving}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button variant="primary" icon="check" loading={saving} onClick={() => void submit()}>
            Save changes
          </Button>
        </>
      }
    >
      <div style={styles.stack}>
        {formError ? (
          <InlineNotice tone="danger" title="Could not save the proxy">
            <p>{formError}</p>
          </InlineNotice>
        ) : null}

        <DetailList
          items={[
            { label: "Endpoint", value: <Mono>{proxyEndpoint(proxy)}</Mono> },
            { label: "Username", value: proxy.username ? <Mono>{proxy.username}</Mono> : "—" },
            {
              label: "Password",
              value: (
                <Badge tone={proxy.has_credentials ? "success" : "neutral"} icon={proxy.has_credentials ? "key" : undefined}>
                  {proxy.has_credentials ? "Stored in vault" : "Not set"}
                </Badge>
              ),
            },
          ]}
        />

        <Field label="Name" hint="Friendly label used in assignments.">
          <Input value={name} placeholder={proxy.host} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field label="Tags" hint="Comma-separated labels.">
          <Input
            value={tags}
            placeholder="eu, http"
            onChange={(event) => setTags(event.target.value)}
          />
        </Field>
        <Checkbox
          label="Enabled"
          hint="Disabled proxies are hidden from assignments but kept in the pool."
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />

        <InlineNotice tone="info" title="Rotating credentials">
          <p>
            The API never returns stored passwords. To rotate a password, add a replacement proxy and
            re-assign instances; delete the old entry afterwards.
          </p>
        </InlineNotice>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* Guest egress verification                                                   */
/* -------------------------------------------------------------------------- */

export function VerifyGuestDialog({
  open,
  instance,
  proxies,
  defaultProxyId,
  onClose,
  onVerified,
}: {
  open: boolean;
  instance: AvdInfo | null;
  proxies: ProxyView[];
  defaultProxyId?: string | null;
  onClose: () => void;
  onVerified: (result: ProxyTestResult) => void;
}) {
  const [proxyId, setProxyId] = useState("");
  const [tokenUrl, setTokenUrl] = useState("");
  const [timeoutS, setTimeoutS] = useState("120");
  const [tokenError, setTokenError] = useState<string | undefined>();
  const [proxyError, setProxyError] = useState<string | undefined>();
  const [formError, setFormError] = useState<string | null>(null);
  const [result, setResult] = useState<ProxyTestResult | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (!open || !instance) return;
    setProxyId(defaultProxyId ?? instance.proxy_id ?? "");
    setTokenUrl("");
    setTimeoutS("120");
    setTokenError(undefined);
    setProxyError(undefined);
    setFormError(null);
    setResult(null);
    setRunning(false);
  }, [open, instance, defaultProxyId]);

  if (!instance) return null;

  const run = async (): Promise<void> => {
    let invalid = false;
    setProxyError(undefined);
    setTokenError(undefined);
    setFormError(null);

    if (!proxyId) {
      setProxyError("Choose the proxy to verify.");
      invalid = true;
    }
    const trimmedUrl = tokenUrl.trim();
    if (trimmedUrl) {
      try {
        const parsed = new URL(trimmedUrl);
        if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
          setTokenError("Use an http:// or https:// URL.");
          invalid = true;
        }
      } catch {
        setTokenError("Enter a valid URL, for example https://api.ipify.org?format=json.");
        invalid = true;
      }
    }
    if (invalid) return;

    setRunning(true);
    try {
      const checked = await api.verifyGuestEgress({
        avd_name: instance.name,
        proxy_id: proxyId,
        token_url: trimmedUrl || null,
        timeout_s: Number(timeoutS),
      });
      setResult(checked);
      onVerified(checked);
    } catch (caught) {
      setFormError(errorMessage(caught));
    } finally {
      setRunning(false);
    }
  };

  const proxyOptions = proxies.map((proxy) => ({
    value: proxy.id,
    label: `${proxy.name ?? proxy.id} · ${proxy.scheme ?? "http"}://${proxy.host}:${proxy.port}${
      proxy.enabled === false ? " (disabled)" : ""
    }`,
  }));

  const footer = result ? (
    <>
      <Button variant="ghost" onClick={() => setResult(null)}>
        Verify again
      </Button>
      <Button variant="primary" onClick={onClose}>
        Close
      </Button>
    </>
  ) : (
    <>
      <Button variant="ghost" onClick={onClose} disabled={running}>
        Cancel
      </Button>
      <Button variant="primary" icon="wifi" loading={running} onClick={() => void run()}>
        Verify egress
      </Button>
    </>
  );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Verify guest egress · ${instance.name}`}
      description="Runs an HTTP(S) request from inside the guest through its assigned proxy and records the egress IP."
      size="md"
      dismissible={!running}
      footer={footer}
    >
      <div style={styles.stack}>
        {instance.state !== "running" ? (
          <InlineNotice tone="warning" title="Instance is not running">
            <p>
              The instance is currently <strong>{instance.state}</strong>. Guest egress verification
              requires a booted guest, so start it first.
            </p>
          </InlineNotice>
        ) : null}

        {formError ? (
          <InlineNotice tone="danger" title="Guest egress verification failed">
            <p>{formError}</p>
          </InlineNotice>
        ) : null}

        {result ? (
          <div style={styles.stack}>
            <InlineNotice
              tone={result.ok ? "success" : "danger"}
              title={result.ok ? "Guest egress confirmed" : "Guest egress failed"}
            >
              <p>
                {result.ok
                  ? "The request from inside the guest completed through the assigned proxy."
                  : "The guest request did not complete through the proxy. Inspect the details below."}
              </p>
            </InlineNotice>
            <DetailList
              items={[
                {
                  label: "Result",
                  value: <StatusPill status={result.ok ? "ok" : "failed"} label={result.ok ? "Reachable" : "Failed"} />,
                },
                { label: "Instance", value: <Mono>{instance.name}</Mono> },
                { label: "Proxy", value: <Mono>{proxyId}</Mono> },
                { label: "Egress IP", value: <Mono>{result.egress_ip || "—"}</Mono> },
                { label: "Latency", value: result.latency_ms === null || result.latency_ms === undefined ? "—" : `${result.latency_ms} ms` },
                { label: "HTTP status", value: result.status_code === null || result.status_code === undefined ? "—" : String(result.status_code) },
                { label: "Checked", value: formatDateTime(result.checked_at) },
                ...(result.error ? [{ label: "Error", value: redactProxySecrets(result.error) }] : []),
              ]}
            />
          </div>
        ) : (
          <>
            <Field label="Proxy" required error={proxyError} hint="The guest request is bound to exactly one proxy.">
              <Select
                value={proxyId}
                invalid={Boolean(proxyError)}
                placeholder="Choose a proxy"
                options={proxyOptions}
                onChange={(event) => setProxyId(event.target.value)}
              />
            </Field>

            <Field
              label="Token URL"
              error={tokenError}
              hint="Optional HTTPS endpoint echoed from inside the guest; defaults to the daemon egress check."
            >
              <Input
                value={tokenUrl}
                leadingIcon="link"
                placeholder="https://api.ipify.org?format=json"
                invalid={Boolean(tokenError)}
                spellCheck={false}
                onChange={(event) => setTokenUrl(event.target.value)}
              />
            </Field>

            <Field label="Timeout" hint="How long the daemon waits for the guest request.">
              <Select
                value={timeoutS}
                onChange={(event) => setTimeoutS(event.target.value)}
                options={[
                  { value: "30", label: "30 seconds" },
                  { value: "60", label: "60 seconds" },
                  { value: "120", label: "2 minutes" },
                  { value: "300", label: "5 minutes" },
                ]}
              />
            </Field>

            <Hint>
              Host-side proxy checks are only a smoke test. This action proves that traffic leaving the
              guest actually traverses the assigned proxy.
            </Hint>
          </>
        )}
      </div>
    </Modal>
  );
}
