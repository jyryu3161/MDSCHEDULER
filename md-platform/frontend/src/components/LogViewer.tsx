import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { JobLog } from "../types";

const LEVEL_STYLE: Record<JobLog["level"], string> = {
  info: "text-slate-300",
  warning: "text-amber-300",
  error: "text-red-400",
};

const LEVEL_TAG: Record<JobLog["level"], string> = {
  info: "INFO",
  warning: "WARN",
  error: "ERROR",
};

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString(undefined, {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

interface Props {
  logs: JobLog[];
  height?: number;
  // Render an empty hint when there are no logs yet.
  emptyHint?: string;
}

// Auto-scrolling, level-colored log console. Sticks to the bottom while the
// user is at the bottom; if they scroll up to read history, auto-scroll pauses
// and a "jump to latest" affordance appears.
export function LogViewer({
  logs,
  height = 320,
  emptyHint = "No log entries yet.",
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [stuckToBottom, setStuckToBottom] = useState(true);

  // Track whether the user is parked at the bottom (within a small threshold).
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight;
    setStuckToBottom(distanceFromBottom < 24);
  };

  // After new logs render, keep pinned to bottom if the user was there.
  useLayoutEffect(() => {
    if (!stuckToBottom) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logs, stuckToBottom]);

  // On first mount, start at the bottom.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  const jumpToLatest = () => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setStuckToBottom(true);
  };

  return (
    <div className="relative">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="overflow-y-auto rounded-md bg-slate-900 p-3 font-mono text-xs leading-relaxed"
        style={{ height }}
        role="log"
        aria-live="polite"
        aria-label="Job log"
      >
        {logs.length === 0 ? (
          <div className="text-slate-500">{emptyHint}</div>
        ) : (
          logs.map((log) => (
            <div key={log.id} className="flex gap-2 whitespace-pre-wrap break-words">
              <span className="shrink-0 text-slate-500">
                {formatTime(log.created_at)}
              </span>
              <span
                className={`shrink-0 font-semibold ${LEVEL_STYLE[log.level]}`}
              >
                {LEVEL_TAG[log.level]}
              </span>
              <span className="shrink-0 text-slate-500">[{log.step}]</span>
              <span className={LEVEL_STYLE[log.level]}>{log.message}</span>
            </div>
          ))
        )}
      </div>
      {!stuckToBottom && logs.length > 0 && (
        <button
          type="button"
          onClick={jumpToLatest}
          className="absolute bottom-3 right-3 rounded-full bg-brand-600 px-3 py-1 text-xs font-medium text-white shadow hover:bg-brand-700"
        >
          Jump to latest
        </button>
      )}
    </div>
  );
}
