import { useQuery } from "@tanstack/react-query";

import { fetchHealth, type Health } from "@/api/health";

/**
 * Sanity page for Phase 10-1: confirms the Vite dev-server proxy
 * forwards /health to the FastAPI backend. The real principal
 * surface starts in Phase 10-3 with the approval queue.
 */
export function HealthPage() {
  const { data, isPending, isError, error } = useQuery<Health, Error>({
    queryKey: ["health"],
    queryFn: fetchHealth,
  });

  return (
    <main className="mx-auto max-w-2xl px-6 py-12 font-sans">
      <h1 className="text-2xl font-semibold tracking-tight">
        MC &amp; S CoWorker
      </h1>
      <p className="mt-2 text-sm text-neutral-500">
        Frontend scaffold — Phase 10-1.
      </p>

      <section className="mt-8 rounded-lg border border-neutral-200 bg-neutral-50 p-6">
        <h2 className="text-sm font-medium uppercase tracking-wider text-neutral-700">
          Backend health
        </h2>
        {isPending && (
          <p className="mt-2 text-sm text-neutral-500">checking…</p>
        )}
        {isError && (
          <p className="mt-2 text-sm text-red-700">
            backend unreachable: {error.message}
          </p>
        )}
        {data && (
          <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-neutral-500">status</dt>
            <dd className="font-mono">{data.status}</dd>
            <dt className="text-neutral-500">service</dt>
            <dd className="font-mono">{data.service}</dd>
            <dt className="text-neutral-500">version</dt>
            <dd className="font-mono">{data.version}</dd>
            <dt className="text-neutral-500">shadow_mode</dt>
            <dd className="font-mono">{data.shadow_mode}</dd>
          </dl>
        )}
      </section>
    </main>
  );
}
