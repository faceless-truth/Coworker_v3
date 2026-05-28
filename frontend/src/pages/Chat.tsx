import ComingSoon from '../components/ComingSoon'
export default function Chat() {
  return <ComingSoon title="Chat" description="Free-form conversation with CoWorker. Ask questions, request drafts, or run ad-hoc research across your client base." endpoint="POST /api/v1/chat/message" />
}
