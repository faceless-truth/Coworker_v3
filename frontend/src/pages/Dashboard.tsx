import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, CheckSquare, Clock } from 'lucide-react'
import { approvals, ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

export default function Dashboard() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const [pendingCount, setPendingCount] = useState<number | null>(null)

  // Poll pending approvals count every 30s — the one live data point on this page
  useEffect(() => {
    let cancelled = false
    async function fetchCount() {
      try {
        const data = await approvals.list({ status: 'pending', limit: 1 })
        if (!cancelled) setPendingCount(data.total)
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return
        // silently ignore other errors — dashboard is best-effort
      }
    }
    fetchCount()
    const id = setInterval(fetchCount, 30_000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  const greeting = (() => {
    const h = new Date().getHours()
    if (h < 12) return 'Good morning'
    if (h < 17) return 'Good afternoon'
    return 'Good evening'
  })()

  const firstName = user?.display_name.split(' ')[0] ?? ''

  return (
    <div className="p-8 max-w-5xl">
      {/* Greeting */}
      <div className="mb-8">
        <h1
          className="text-4xl mb-1"
          style={{ color: '#142234', fontFamily: 'DM Serif Display, serif' }}
        >
          {greeting}{firstName ? `, ${firstName}` : ''}.
        </h1>
        <p className="text-sm" style={{ color: '#858481' }}>
          {new Date().toLocaleDateString('en-AU', {
            weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
          })}
        </p>
      </div>

      {/* Live: approvals count */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-5 mb-8">
        <div
          className="bg-white border-l-4 p-6 cursor-pointer"
          style={{
            borderColor: '#eb881f',
            borderTop: '1px solid #d9d8d8',
            borderRight: '1px solid #d9d8d8',
            borderBottom: '1px solid #d9d8d8',
          }}
          onClick={() => navigate('/approvals')}
        >
          <div
            className="text-5xl font-bold mb-2"
            style={{ color: '#eb881f', fontFamily: 'DM Serif Display, serif' }}
          >
            {pendingCount ?? '—'}
          </div>
          <div className="text-sm mb-3" style={{ color: '#34322d' }}>
            Awaiting approval
          </div>
          <div className="flex items-center gap-1 text-xs" style={{ color: '#eb881f' }}>
            <CheckSquare size={12} /> Review queue <ArrowRight size={11} />
          </div>
        </div>

        {/* Placeholder cards */}
        {[
          { label: 'Actions taken today', color: '#3080bc', endpoint: '/api/v1/dashboard/summary' },
          { label: 'Proactive findings',  color: '#e11d48', endpoint: '/api/v1/findings' },
        ].map(card => (
          <div
            key={card.label}
            className="bg-white border-l-4 p-6"
            style={{
              borderColor: card.color,
              borderTop: '1px solid #d9d8d8',
              borderRight: '1px solid #d9d8d8',
              borderBottom: '1px solid #d9d8d8',
              opacity: 0.5,
            }}
          >
            <div
              className="text-5xl font-bold mb-2"
              style={{ color: card.color, fontFamily: 'DM Serif Display, serif' }}
            >
              —
            </div>
            <div className="text-sm mb-3" style={{ color: '#34322d' }}>{card.label}</div>
            <div className="flex items-center gap-1 text-xs" style={{ color: '#858481' }}>
              <Clock size={11} /> Waiting for {card.endpoint}
            </div>
          </div>
        ))}
      </div>

      {/* Coming soon notice */}
      <div className="bg-white border p-6" style={{ borderColor: '#d9d8d8' }}>
        <div className="flex items-center gap-2 mb-3">
          <Clock size={15} style={{ color: '#3080bc' }} />
          <span className="text-sm font-semibold" style={{ color: '#142234' }}>
            Full dashboard coming soon
          </span>
        </div>
        <p className="text-sm mb-4" style={{ color: '#858481' }}>
          The backend endpoint <code className="text-xs px-1" style={{ background: '#f3f1ee', color: '#3080bc' }}>GET /api/v1/dashboard/summary</code> is not yet implemented.
          When it ships, this page will show today's schedule, live activity feed, token usage, and proactive findings.
        </p>
        <div className="grid grid-cols-2 gap-3 text-xs" style={{ color: '#858481' }}>
          <div>
            <div className="font-semibold mb-1" style={{ color: '#142234' }}>Live now</div>
            <div>✓ Approvals queue (real data)</div>
            <div>✓ Inbox read</div>
            <div>✓ Microsoft Entra auth</div>
          </div>
          <div>
            <div className="font-semibold mb-1" style={{ color: '#142234' }}>Coming next</div>
            <div>· Dashboard summary</div>
            <div>· Specialists</div>
            <div>· Activity log</div>
            <div>· Findings · Chat · Memory</div>
          </div>
        </div>
      </div>
    </div>
  )
}
