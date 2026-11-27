import { useMemo, useState, type ReactNode } from "react";

import { cx } from "../utils";
import { EmptyState, Skeleton } from "./Feedback";
import { Icon } from "./Icon";

export interface Column<T> {
  key: string;
  header: ReactNode;
  /** Cell renderer; defaults to `row[column.key]` when it is a scalar. */
  render?: (row: T, index: number) => ReactNode;
  /** Value used for sorting. Omit (and skip `sortable`) for non-sortable columns. */
  sortValue?: (row: T) => string | number | null | undefined;
  sortable?: boolean;
  align?: "left" | "right" | "center";
  width?: string;
  className?: string;
}

export interface TableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  caption?: string;
  /** Shown when there are no rows. */
  empty?: ReactNode;
  loading?: boolean;
  skeletonRows?: number;
  onRowClick?: (row: T) => void;
  rowAriaLabel?: (row: T) => string;
  selectedKey?: string | null;
  dense?: boolean;
  className?: string;
}

type SortState = { key: string; direction: "ascending" | "descending" } | null;

function defaultCell<T>(row: T, column: Column<T>): ReactNode {
  const value = (row as Record<string, unknown>)[column.key];
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string" || typeof value === "number") return String(value);
  return "—";
}

function compare(a: string | number | null | undefined, b: string | number | null | undefined): number {
  if (a === b) return 0;
  if (a === null || a === undefined) return 1;
  if (b === null || b === undefined) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/**
 * Accessible data table with client-side sorting, loading skeletons, empty
 * state and optional keyboard-activatable rows.
 */
export function Table<T>({
  columns,
  rows,
  rowKey,
  caption,
  empty,
  loading = false,
  skeletonRows = 4,
  onRowClick,
  rowAriaLabel,
  selectedKey = null,
  dense = false,
  className,
}: TableProps<T>) {
  const [sort, setSort] = useState<SortState>(null);

  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    const column = columns.find((candidate) => candidate.key === sort.key);
    if (!column?.sortValue) return rows;
    const factor = sort.direction === "ascending" ? 1 : -1;
    return [...rows].sort((left, right) => factor * compare(column.sortValue!(left), column.sortValue!(right)));
  }, [columns, rows, sort]);

  const toggleSort = (column: Column<T>): void => {
    if (!column.sortable || !column.sortValue) return;
    setSort((current) => {
      if (!current || current.key !== column.key) {
        return { key: column.key, direction: "ascending" };
      }
      return current.direction === "ascending"
        ? { key: column.key, direction: "descending" }
        : null;
    });
  };

  const ariaSortFor = (column: Column<T>): "ascending" | "descending" | undefined => {
    if (sort?.key !== column.key) return undefined;
    return sort.direction;
  };

  const interactive = Boolean(onRowClick);

  return (
    <div className={cx("ds-table-wrap", className)}>
      <table className={cx("ds-table", dense && "ds-table--dense")}>
        {caption ? <caption className="ds-table__caption">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((column) => {
              const isSorted = sort?.key === column.key;
              return (
                <th
                  key={column.key}
                  scope="col"
                  style={column.width ? { width: column.width } : undefined}
                  className={cx(
                    column.align && `ds-table__cell--${column.align}`,
                    column.sortable && "ds-table__cell--sortable",
                    column.className,
                  )}
                  aria-sort={ariaSortFor(column)}
                >
                  {column.sortable && column.sortValue ? (
                    <button
                      type="button"
                      className="ds-table__sort"
                      onClick={() => toggleSort(column)}
                      title={`Sort by ${typeof column.header === "string" ? column.header : column.key}`}
                    >
                      <span>{column.header}</span>
                      <Icon
                        name={isSorted && sort?.direction === "descending" ? "chevron-down" : "chevron-up"}
                        size={13}
                        className={cx("ds-table__sort-icon", !isSorted && "is-idle")}
                      />
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {loading
            ? Array.from({ length: skeletonRows }, (_, rowIndex) => (
                <tr key={`skeleton-${rowIndex}`} className="ds-table__row ds-table__row--skeleton">
                  {columns.map((column) => (
                    <td key={column.key} className={column.align ? `ds-table__cell--${column.align}` : undefined}>
                      <Skeleton width={rowIndex % 2 === 0 ? "72%" : "54%"} />
                    </td>
                  ))}
                </tr>
              ))
            : sortedRows.map((row) => {
                const key = rowKey(row);
                const selected = selectedKey === key;
                return (
                  <tr
                    key={key}
                    className={cx(
                      "ds-table__row",
                      interactive && "ds-table__row--clickable",
                      selected && "ds-table__row--selected",
                    )}
                    tabIndex={interactive ? 0 : undefined}
                    aria-label={interactive ? rowAriaLabel?.(row) : undefined}
                    onClick={interactive ? () => onRowClick?.(row) : undefined}
                    onKeyDown={
                      interactive
                        ? (event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              onRowClick?.(row);
                            }
                          }
                        : undefined
                    }
                  >
                    {columns.map((column) => (
                      <td
                        key={column.key}
                        className={cx(
                          column.align && `ds-table__cell--${column.align}`,
                          column.className,
                        )}
                      >
                        {column.render ? column.render(row, sortedRows.indexOf(row)) : defaultCell(row, column)}
                      </td>
                    ))}
                  </tr>
                );
              })}
          {!loading && sortedRows.length === 0 ? (
            <tr className="ds-table__row ds-table__row--empty">
              <td colSpan={columns.length}>
                {empty ?? <EmptyState compact title="Nothing here yet" icon="info" />}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}
