import ComingSoon from '../components/ComingSoon'
export default function Settings() {
  return <ComingSoon title="Settings" description="Firm configuration, shadow mode toggle, user profile, and token usage limits." endpoint="GET /api/v1/settings/firm" />
}
