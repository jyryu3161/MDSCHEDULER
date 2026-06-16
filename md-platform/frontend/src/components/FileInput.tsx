import {
  useId,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
} from "react";

interface Props {
  label: string;
  // Accept attribute, e.g. ".pdbqt" or ".sdf,.mol2".
  accept?: string;
  // Currently selected file (controlled), or null when none.
  value: File | null;
  onChange: (file: File | null) => void;
  // Short hint shown under the label (e.g. expected formats).
  hint?: string;
  required?: boolean;
  disabled?: boolean;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

// Accessible single-file picker with drag-and-drop.
//  - The native <input type=file> is the labeled, focusable control; its
//    accessible name comes from the visible <label htmlFor> and its description
//    from the hint via aria-describedby.
//  - The drop zone is a plain element (not a <label>) so no interactive control
//    is nested inside a label; it forwards activation to the input.
//  - The Remove button lives outside the label association.
export function FileInput({
  label,
  accept,
  value,
  onChange,
  hint,
  required = false,
  disabled = false,
}: Props) {
  const inputId = useId();
  const labelId = useId();
  const hintId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const openPicker = () => {
    if (!disabled) inputRef.current?.click();
  };

  const onDropZoneKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openPicker();
    }
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    const file = e.dataTransfer.files?.[0];
    if (file) onChange(file);
  };

  const clear = () => {
    onChange(null);
    if (inputRef.current) inputRef.current.value = "";
  };

  return (
    <div>
      <label
        id={labelId}
        htmlFor={inputId}
        className="label flex items-center gap-1"
      >
        <span>{label}</span>
        {required && (
          <span className="text-red-500" aria-hidden="true">
            *
          </span>
        )}
      </label>

      <div className="flex items-stretch gap-2">
        <div
          role="button"
          tabIndex={disabled ? -1 : 0}
          aria-disabled={disabled || undefined}
          aria-labelledby={labelId}
          aria-describedby={hint ? hintId : undefined}
          onClick={openPicker}
          onKeyDown={onDropZoneKeyDown}
          onDragOver={(e) => {
            e.preventDefault();
            if (!disabled) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={[
            "flex flex-1 items-center gap-3 rounded-md border border-dashed px-3 py-3 text-sm transition-colors focus:outline-none focus:ring-1 focus:ring-brand-500",
            disabled
              ? "cursor-not-allowed border-slate-200 bg-slate-100 text-slate-400"
              : dragging
                ? "cursor-pointer border-brand-500 bg-brand-50 text-brand-700"
                : "cursor-pointer border-slate-300 bg-white text-slate-600 hover:border-brand-400",
          ].join(" ")}
        >
          {value ? (
            <span className="min-w-0 flex-1 truncate">
              <span className="font-medium text-slate-800">{value.name}</span>
              <span className="ml-2 text-xs text-slate-500">
                {formatSize(value.size)}
              </span>
            </span>
          ) : (
            <span className="flex-1">
              Drop a file here or{" "}
              <span className="text-brand-600">browse</span>
              {accept && (
                <span className="ml-1 text-xs text-slate-400">({accept})</span>
              )}
            </span>
          )}
        </div>

        {value && !disabled && (
          <button
            type="button"
            onClick={clear}
            className="shrink-0 rounded-md border border-slate-300 px-3 text-xs font-medium text-slate-600 hover:bg-slate-50 hover:text-red-600"
          >
            Remove
          </button>
        )}
      </div>

      {/* The file input carries the form value but is removed from the tab
          order (tabIndex=-1) and aria-hidden: the visible drop zone above is
          the keyboard-accessible, labeled control. The input is reached only
          via the drop zone's programmatic click. */}
      <input
        id={inputId}
        ref={inputRef}
        type="file"
        accept={accept}
        required={required && !value}
        disabled={disabled}
        tabIndex={-1}
        aria-hidden="true"
        className="sr-only"
        onChange={(e) => onChange(e.target.files?.[0] ?? null)}
      />

      {hint && (
        <p id={hintId} className="mt-1 text-xs text-slate-500">
          {hint}
        </p>
      )}
    </div>
  );
}
