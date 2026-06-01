/**
 * Derive a conversation title from the first user message.
 *
 * Backend stores `title` as nullable and never auto-populates it.
 * The Chat sidebar shows the derived title so the user can pick out
 * conversations without us round-tripping a backend change.
 */
export function deriveTitle(firstUserMessage: string | null | undefined): string {
  if (!firstUserMessage) return 'New conversation';
  const cleaned = firstUserMessage.trim().split('\n')[0];
  if (!cleaned) return 'New conversation';
  return cleaned.length > 50 ? `${cleaned.slice(0, 47)}…` : cleaned;
}

/**
 * Human-readable relative time. Locale-aware fallback once over a week.
 */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const ms = now - t;
  if (ms < 0) return 'just now';
  const min = Math.floor(ms / 60_000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d ago`;
  return new Date(iso).toLocaleDateString();
}
