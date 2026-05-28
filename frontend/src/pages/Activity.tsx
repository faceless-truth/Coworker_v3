import ComingSoon from '../components/ComingSoon'
export default function Activity() {
  return <ComingSoon title="Activity Log" description="Full chronological trace of every agent action, tool call, and plugin run. The complete audit trail." endpoint="GET /api/v1/activity" />
}
