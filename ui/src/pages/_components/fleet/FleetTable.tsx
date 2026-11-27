import { useEffect, useRef, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { ButtonLink, IconButton, Skeleton } from "@/lib/components";
import type { AvdInfo, ProxyView } from "@/lib/types";
import { cx } from "@/lib/utils";

import {
  InstanceStatePill,
  LiveUptime,
  ProxyBadge,
  isRunning,
  portEntries,
} from "./fleet-utils";

export interface FleetViewProps {
  instances: readonly AvdInfo[];
  proxies: readonly ProxyView[] | null;
  selected: ReadonlySet<string>;
  dense: boolean;
  empty: ReactNode;
  onToggle: (name: string) => void;
  onToggleAll: () => void;
  onScreenshot: (instance: AvdInfo) => void;
}

/* -------------------------------------------------------------------------- */
/* Selection helpers                                                           */
/* -------------------------------------------------------------------------- */

interface SelectAllCheckboxProps {
  checked: boolean;
  indeterminate: boolean;
  disabled?: boolean;
  onChange: () => void;
}

function SelectAllCheckbox({ checked, indeterminate, disabled, onChange }: SelectAllCheckboxProps) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate && !checked;
  }, [checked, indeterminate]);

  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label="Select all shown instances"
      checked={checked}
      disabled={disabled}
      onChange={onChange}
      onClick={(event) => event.stopPropagation()}
      style={{ width: 16, height: 16, accentColor: "var(--accent)", cursor: "pointer" }}
    />
  );
}

function selectionState(instances: readonly AvdInfo[], selected: ReadonlySet<string>) {
  const selectable = instances.map((instance) => instance.name);
  const selectedCount = selectable.filter((name) => selected.has(name)).length;
  return {
    allSelected: selectable.length > 0 && selectedCount === selectable.length,
    someSelected: selectedCount > 0 && selectedCount < selectable.length,
  };
}

/* -------------------------------------------------------------------------- */
/* Cells shared by the table and the grid                                      */
/* -------------------------------------------------------------------------- */

function InstanceName({ instance }: { instance: AvdInfo }) {
  const subtitle = [instance.image_name, instance.device_id].filter(Boolean).join(" · ");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Link
        to={`/fleet/${encodeURIComponent(instance.name)}`}
        onClick={(event) => event.stopPropagation()}
        style={{ fontWeight: 600 }}
      >
        {instance.name}
      </Link>
      {subtitle ? (
        <span className="muted small" style={{ fontSize: "var(--fs-xs)" }}>
          {subtitle}
        </span>
      ) : null}
    </div>
  );
}

function SerialCell({ instance }: { instance: AvdInfo }) {
  const ports = instance.ports;
  const pair =
    ports && ports.console !== undefined && ports.adb !== undefined
      ? `${ports.console} / ${ports.adb}`
      : "—";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <code style={{ fontSize: "var(--fs-xs)" }}>{instance.adb_serial ?? "—"}</code>
      <span className="muted small" style={{ fontSize: "var(--fs-xs)" }}>
        console / adb: {pair}
      </span>
    </div>
  );
}

function ScreenshotButton({ instance, onScreenshot }: { instance: AvdInfo; onScreenshot: (instance: AvdInfo) => void }) {
  const enabled = isRunning(instance.state);
  return (
    <IconButton
      label={`Capture screenshot of ${instance.name}`}
      icon="camera"
      size="sm"
      disabled={!enabled}
      title={enabled ? "Open the live screen" : "Screenshot requires a running instance"}
      onClick={(event) => {
        event.stopPropagation();
        onScreenshot(instance);
      }}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Table view                                                                  */
/* -------------------------------------------------------------------------- */

export function FleetTable({
  instances,
  proxies,
  selected,
  dense,
  empty,
  onToggle,
  onToggleAll,
  onScreenshot,
}: FleetViewProps) {
  const { allSelected, someSelected } = selectionState(instances, selected);

  return (
    <div className="ds-table-wrap">
      <table
        className={cx("ds-table", dense && "ds-table--dense")}
        aria-describedby="fleet-table-help"
      >
        <caption className="ds-table__caption">
          Fleet instances — click a row or its checkbox to select. The instance name opens details.
        </caption>
        <thead>
          <tr>
            <th scope="col" style={{ width: 44 }}>
              <SelectAllCheckbox
                checked={allSelected}
                indeterminate={someSelected}
                disabled={instances.length === 0}
                onChange={onToggleAll}
              />
            </th>
            <th scope="col">Instance</th>
            <th scope="col" style={{ width: 120 }}>
              State
            </th>
            <th scope="col" style={{ width: 150 }}>
              Proxy
            </th>
            <th scope="col" style={{ width: 170 }}>
              ADB serial / ports
            </th>
            <th scope="col" style={{ width: 110 }}>
              Uptime
            </th>
            <th scope="col" style={{ width: 64, textAlign: "right" }}>
              <span className="visually-hidden">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {instances.length === 0 ? (
            <tr className="ds-table__row ds-table__row--empty">
              <td colSpan={7}>{empty}</td>
            </tr>
          ) : (
            instances.map((instance) => {
              const isSelected = selected.has(instance.name);
              return (
                <tr
                  key={instance.name}
                  className={cx("ds-table__row", "ds-table__row--clickable", isSelected && "ds-table__row--selected")}
                  tabIndex={0}
                  onClick={() => onToggle(instance.name)}
                  onKeyDown={(event) => {
                    if (event.target !== event.currentTarget) return;
                    if (event.key === " " || event.key === "Enter") {
                      event.preventDefault();
                      onToggle(instance.name);
                    }
                  }}
                >
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${instance.name}`}
                      checked={isSelected}
                      onChange={() => onToggle(instance.name)}
                      onClick={(event) => event.stopPropagation()}
                      style={{ width: 16, height: 16, accentColor: "var(--accent)", cursor: "pointer" }}
                    />
                  </td>
                  <td>
                    <InstanceName instance={instance} />
                  </td>
                  <td>
                    <InstanceStatePill instance={instance} />
                  </td>
                  <td>
                    <ProxyBadge proxyId={instance.proxy_id} proxies={proxies} />
                  </td>
                  <td>
                    <SerialCell instance={instance} />
                  </td>
                  <td>
                    <LiveUptime instance={instance} />
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <ScreenshotButton instance={instance} onScreenshot={onScreenshot} />
                  </td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
      <p id="fleet-table-help" className="visually-hidden">
        Rows can be selected with the Space or Enter key when focused. Use the instance name link to
        open its detail page.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Grid view                                                                   */
/* -------------------------------------------------------------------------- */

export function FleetGrid({
  instances,
  proxies,
  selected,
  empty,
  onToggle,
  onScreenshot,
}: FleetViewProps) {
  if (instances.length === 0) return <>{empty}</>;

  return (
    <ul
      aria-label="Instances"
      style={{
        listStyle: "none",
        padding: 0,
        margin: 0,
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
        gap: "var(--space-4)",
      }}
    >
      {instances.map((instance) => {
        const isSelected = selected.has(instance.name);
        const entries = portEntries(instance).filter(([label]) => label === "Console" || label === "ADB");
        return (
          <li key={instance.name}>
            <div
              role="group"
              aria-label={instance.name}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "var(--space-3)",
                padding: "var(--space-4)",
                border: `1px solid ${isSelected ? "var(--accent)" : "var(--border)"}`,
                borderRadius: "var(--radius-lg)",
                background: isSelected ? "var(--accent-soft)" : "var(--surface)",
                height: "100%",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
                <input
                  type="checkbox"
                  aria-label={`Select ${instance.name}`}
                  checked={isSelected}
                  onChange={() => onToggle(instance.name)}
                  style={{ width: 16, height: 16, accentColor: "var(--accent)", cursor: "pointer" }}
                />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <InstanceName instance={instance} />
                </div>
                <InstanceStatePill instance={instance} />
              </div>

              <dl className="kv-list" style={{ margin: 0 }}>
                <div className="kv-list__row">
                  <dt>ADB</dt>
                  <dd>
                    <code style={{ fontSize: "var(--fs-xs)" }}>{instance.adb_serial ?? "—"}</code>
                  </dd>
                </div>
                <div className="kv-list__row">
                  <dt>Ports</dt>
                  <dd>{entries.map(([, value]) => value).join(" / ") || "—"}</dd>
                </div>
                <div className="kv-list__row">
                  <dt>Uptime</dt>
                  <dd>
                    <LiveUptime instance={instance} />
                  </dd>
                </div>
                <div className="kv-list__row">
                  <dt>Proxy</dt>
                  <dd>
                    <ProxyBadge proxyId={instance.proxy_id} proxies={proxies} />
                  </dd>
                </div>
              </dl>

              <div style={{ display: "flex", gap: "var(--space-2)", marginTop: "auto" }}>
                <ButtonLink to={`/fleet/${encodeURIComponent(instance.name)}`} size="sm" variant="secondary">
                  Open
                </ButtonLink>
                <ScreenshotButton instance={instance} onScreenshot={onScreenshot} />
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/* -------------------------------------------------------------------------- */
/* Loading skeleton                                                            */
/* -------------------------------------------------------------------------- */

const COLUMNS = 7;

export function FleetTableSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="ds-table-wrap">
      <table className="ds-table" aria-busy="true" aria-label="Loading fleet">
        <thead>
          <tr>
            <th scope="col" style={{ width: 44 }} />
            <th scope="col">Instance</th>
            <th scope="col">State</th>
            <th scope="col">Proxy</th>
            <th scope="col">ADB serial / ports</th>
            <th scope="col">Uptime</th>
            <th scope="col">
              <span className="visually-hidden">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }, (_, index) => (
            <tr key={index} className="ds-table__row ds-table__row--skeleton">
              {Array.from({ length: COLUMNS }, (__, columnIndex) => (
                <td key={columnIndex}>
                  <Skeleton width={columnIndex === 0 ? 16 : `${50 + ((index * 13 + columnIndex * 7) % 40)}%`} height={14} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
