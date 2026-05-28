import { Clock } from 'lucide-react'

interface ComingSoonProps {
  title: string
  description: string
  endpoint?: string
}

export default function ComingSoon({ title, description, endpoint }: ComingSoonProps) {
  return (
    <div className="flex flex-col items-center justify-center min-h-96 text-center px-8">
      <div
        className="w-16 h-16 rounded-full flex items-center justify-center mb-6"
        style={{ background: 'rgba(48,128,188,0.08)' }}
      >
        <Clock size={28} style={{ color: '#3080bc' }} />
      </div>
      <h2
        className="text-2xl mb-3"
        style={{ color: '#142234', fontFamily: 'DM Serif Display, serif' }}
      >
        {title}
      </h2>
      <p className="text-sm max-w-sm mb-6" style={{ color: '#858481' }}>
        {description}
      </p>
      {endpoint && (
        <div
          className="text-xs font-mono px-3 py-2 border"
          style={{ borderColor: '#d9d8d8', color: '#3080bc', background: 'white' }}
        >
          Waiting for: {endpoint}
        </div>
      )}
    </div>
  )
}
