import { Route, Routes } from "react-router-dom";

import { HealthPage } from "@/pages/HealthPage";

/**
 * Top-level router. Phase 10-1 ships only a /health page that
 * proves the dev-server proxy reaches the backend. Phase 10-2
 * lands the OAuth login wrapper; 10-3+ wires the approval queue.
 */
export function App() {
  return (
    <Routes>
      <Route path="/" element={<HealthPage />} />
      <Route path="*" element={<HealthPage />} />
    </Routes>
  );
}
