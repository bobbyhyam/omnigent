import { useEffect, useState } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  getHostStatus,
  getLocalServerStatus,
  isElectronShell,
  onHostStatusChanged,
  type HostStatus,
  type LocalServerStatus,
} from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

/**
 * Tailwind color for the host dot: green when this machine is connected as a
 * host, amber while a host process is up but not yet tunneled, muted when off
 * or the omnigent CLI is missing.
 */
function dotTone(status: HostStatus): string {
  if (!status.cliInstalled) return "bg-muted-foreground/40";
  if (status.connected) return "bg-success";
  if (status.process === "online") return "bg-warning";
  return "bg-muted-foreground/40";
}

/** Compact sidebar label, e.g. "Host: connected · 2". */
function label(status: HostStatus): string {
  if (!status.cliInstalled) return "Host: CLI not found";
  if (status.connected) {
    return status.sessions > 0 ? `Host: connected · ${status.sessions}` : "Host: connected";
  }
  if (status.process === "online") return "Host: connecting…";
  return "Host: off";
}

/** Fuller explanation for the tooltip. */
function tooltip(status: HostStatus, server: LocalServerStatus | null): string {
  let head: string;
  if (!status.cliInstalled) head = "Omnigent CLI not found — install it to host on this machine.";
  else if (status.error) head = status.error;
  else if (status.connected) {
    const n = status.sessions;
    head =
      n > 0
        ? `This machine is hosting — ${n} active session${n === 1 ? "" : "s"}.`
        : "This machine is hosting for this server.";
  } else if (status.process === "online") head = "Connecting this machine as a host…";
  else head = "This machine is not hosting. Enable it from the connect screen.";

  if (server) {
    head += server.running
      ? `\nLocal server: running${server.liveSessions > 0 ? ` · ${server.liveSessions} active` : ""}.`
      : "\nLocal server: stopped.";
  }
  return head;
}

/**
 * Read-only host-status indicator for the desktop shell, shown in the sidebar
 * footer next to Settings. Hosting is enabled at connect time (the shell's
 * setup page); this just surfaces the live status — a colored dot plus a short
 * label — so you can see whether this machine is running agent work.
 *
 * Renders nothing outside the Electron shell or before the shell reports a
 * status (e.g. a foreign page). Desktop-only by layout (hidden on the narrow
 * mobile sidebar, where the footer is a floating icon button).
 */
export function HostStatusIndicator() {
  const [status, setStatus] = useState<HostStatus | null>(null);
  const [server, setServer] = useState<LocalServerStatus | null>(null);

  useEffect(() => {
    if (!isElectronShell()) return;
    let cancelled = false;
    const refreshServer = () => {
      void getLocalServerStatus().then((s) => {
        if (!cancelled) setServer(s);
      });
    };
    void getHostStatus().then((s) => {
      if (!cancelled) setStatus(s);
    });
    refreshServer();
    // The shell pushes host status on a timer; refetch the local-server line
    // alongside each push so both stay in sync.
    const unsubscribe = onHostStatusChanged((s) => {
      if (cancelled) return;
      setStatus(s);
      refreshServer();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  if (!status) return null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground max-md:hidden"
          data-testid="host-status-indicator"
        >
          <span aria-hidden className={cn("size-2 shrink-0 rounded-full", dotTone(status))} />
          <span className="truncate">{label(status)}</span>
        </div>
      </TooltipTrigger>
      <TooltipContent side="right" className="whitespace-pre-line">
        {tooltip(status, server)}
      </TooltipContent>
    </Tooltip>
  );
}
