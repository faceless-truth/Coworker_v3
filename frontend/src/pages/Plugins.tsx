import ComingSoon from '../components/ComingSoon'
export default function Plugins() {
  return <ComingSoon title="Plugins" description="Manage and toggle the 14 built-in plugins. Enable or disable each plugin, view run history, and trigger manual runs." endpoint="GET /api/v1/plugins" />
}
