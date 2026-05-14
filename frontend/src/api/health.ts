/**
 * Backend /health endpoint shape. Mirrors the JSON the FastAPI
 * route returns; the frontend doesn't generate this from an
 * OpenAPI schema yet because the surface is small enough to
 * hand-author. Phase 10-3 may revisit if the approval payload
 * shapes grow.
 */
export type Health = {
  status: string;
  service: string;
  version: string;
  shadow_mode: string;
};

export async function fetchHealth(): Promise<Health> {
  const response = await fetch("/health", {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`/health returned HTTP ${response.status}`);
  }
  return (await response.json()) as Health;
}
