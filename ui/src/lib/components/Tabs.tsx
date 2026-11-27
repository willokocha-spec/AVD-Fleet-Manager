import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { cx } from "../utils";
import { Icon, type IconName } from "./Icon";

export interface TabItem {
  id: string;
  label: ReactNode;
  icon?: IconName;
  badge?: ReactNode;
  disabled?: boolean;
  content: ReactNode;
}

export interface TabsProps {
  items: TabItem[];
  /** Controlled active tab id. */
  value?: string;
  /** Uncontrolled initial tab id (defaults to the first enabled item). */
  defaultValue?: string;
  onChange?: (id: string) => void;
  ariaLabel?: string;
  className?: string;
  panelClassName?: string;
}

/**
 * WAI-ARIA tabs with roving tabindex and arrow/Home/End keyboard support.
 * Panels are rendered eagerly but only the active one is exposed.
 */
export function Tabs({
  items,
  value,
  defaultValue,
  onChange,
  ariaLabel = "Sections",
  className,
  panelClassName,
}: TabsProps) {
  const baseId = useId();
  const enabledItems = items.filter((item) => !item.disabled);
  const fallback = enabledItems[0]?.id ?? "";
  const [internal, setInternal] = useState<string>(defaultValue ?? fallback);
  const activeId = value ?? internal;
  const active = items.find((item) => item.id === activeId) ?? items[0];
  const buttonRefs = useRef(new Map<string, HTMLButtonElement>());

  const select = (id: string): void => {
    if (value === undefined) setInternal(id);
    onChange?.(id);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number): void => {
    const candidates = items.filter((item) => !item.disabled);
    if (candidates.length === 0) return;
    const currentIndex = candidates.findIndex((item) => item.id === items[index]?.id);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (Math.max(currentIndex, 0) + 1) % candidates.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (Math.max(currentIndex, 0) - 1 + candidates.length) % candidates.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = candidates.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    const next = candidates[nextIndex];
    if (!next) return;
    select(next.id);
    buttonRefs.current.get(next.id)?.focus();
  };

  return (
    <div className={cx("ds-tabs", className)}>
      <div className="ds-tabs__list" role="tablist" aria-label={ariaLabel}>
        {items.map((item, index) => {
          const selected = item.id === active?.id;
          return (
            <button
              key={item.id}
              ref={(node) => {
                if (node) buttonRefs.current.set(item.id, node);
                else buttonRefs.current.delete(item.id);
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-${item.id}`}
              aria-selected={selected}
              aria-controls={`${baseId}-panel-${item.id}`}
              tabIndex={selected ? 0 : -1}
              disabled={item.disabled}
              className={cx("ds-tabs__tab", selected && "is-active")}
              onClick={() => select(item.id)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              {item.icon ? <Icon name={item.icon} size={15} aria-hidden="true" /> : null}
              <span>{item.label}</span>
              {item.badge ? <span className="ds-tabs__badge">{item.badge}</span> : null}
            </button>
          );
        })}
      </div>
      {items.map((item) => (
        <div
          key={item.id}
          role="tabpanel"
          id={`${baseId}-panel-${item.id}`}
          aria-labelledby={`${baseId}-tab-${item.id}`}
          hidden={item.id !== active?.id}
          tabIndex={0}
          className={cx("ds-tabs__panel", panelClassName)}
        >
          {item.id === active?.id ? item.content : null}
        </div>
      ))}
    </div>
  );
}
