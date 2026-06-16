import { type ReactNode } from "react";

// A single column definition for DataTable<T>.
//  - `key`     unique identifier for the column (used as React key).
//  - `header`  column heading content.
//  - `render`  cell renderer for a row; receives the row and its index.
//  - `align`   optional text alignment.
//  - `className` optional extra classes applied to every body cell.
export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T, index: number) => ReactNode;
  align?: "left" | "right" | "center";
  className?: string;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  // Stable row key extractor. Defaults to the row index when absent.
  rowKey?: (row: T, index: number) => string | number;
  // Optional row activation handler. When set, the table renders a real,
  // keyboard-accessible action control per row (and a pointer click anywhere on
  // the row is a mouse convenience) while keeping native table semantics.
  onRowClick?: (row: T, index: number) => void;
  // Accessible label for the per-row action control, given the row. Defaults to
  // a generic label. Only used when `onRowClick` is provided.
  rowActionLabel?: (row: T, index: number) => string;
  // Content shown when there are no rows.
  empty?: ReactNode;
  className?: string;
  // When true, the header sticks to the top of a scrolling container.
  stickyHeader?: boolean;
}

const ALIGN_CLASS: Record<NonNullable<Column<unknown>["align"]>, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
};

// Generic, typed table. Keeps presentation only — sorting/filtering/paging are
// the caller's responsibility (pass already-ordered `rows`).
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  rowActionLabel,
  empty = "No data.",
  className = "",
  stickyHeader = false,
}: DataTableProps<T>) {
  const clickable = typeof onRowClick === "function";
  // A leading action column is injected for clickable tables so keyboard users
  // get a focusable control; total column span includes it.
  const totalCols = columns.length + (clickable ? 1 : 0);

  return (
    <div className={`overflow-x-auto ${className}`}>
      <table className="w-full border-collapse">
        <thead className={stickyHeader ? "sticky top-0 z-10 bg-slate-50" : "bg-slate-50"}>
          <tr className="border-b border-slate-200">
            {clickable && (
              <th className="th w-px">
                <span className="sr-only">Action</span>
              </th>
            )}
            {columns.map((col) => (
              <th
                key={col.key}
                className={`th ${col.align ? ALIGN_CLASS[col.align] : ""}`}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td
                colSpan={totalCols}
                className="px-3 py-8 text-center text-sm text-slate-500"
              >
                {empty}
              </td>
            </tr>
          ) : (
            rows.map((row, index) => {
              const key = rowKey ? rowKey(row, index) : index;
              const label = rowActionLabel
                ? rowActionLabel(row, index)
                : "Open row";
              return (
                <tr
                  key={key}
                  className={[
                    "border-b border-slate-100 last:border-0",
                    clickable
                      ? "cursor-pointer hover:bg-slate-50 focus-within:bg-slate-50"
                      : "",
                  ].join(" ")}
                  // Pointer convenience only; the real keyboard path is the
                  // injected <button> below. Native <tr> semantics are kept.
                  onClick={clickable ? () => onRowClick?.(row, index) : undefined}
                >
                  {clickable && (
                    <td className="td">
                      <button
                        type="button"
                        className="grid h-6 w-6 place-items-center rounded text-slate-400 hover:bg-slate-100 hover:text-slate-700 focus:outline-none focus:ring-1 focus:ring-brand-500"
                        aria-label={label}
                        onClick={(e) => {
                          e.stopPropagation();
                          onRowClick?.(row, index);
                        }}
                      >
                        <span aria-hidden="true">›</span>
                      </button>
                    </td>
                  )}
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={[
                        "td",
                        col.align ? ALIGN_CLASS[col.align] : "",
                        col.className ?? "",
                      ].join(" ")}
                    >
                      {col.render(row, index)}
                    </td>
                  ))}
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
