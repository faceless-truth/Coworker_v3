import { auth } from '../api/client';

export default function Login() {
  return (
    <div
      className="min-h-screen flex flex-col items-center justify-center"
      style={{ background: '#f3f1ee' }}
    >
      {/* Logo / wordmark */}
      <div className="mb-10 text-center">
        <div
          className="text-3xl font-bold tracking-tight mb-1"
          style={{ fontFamily: 'DM Serif Display, serif', color: '#142234' }}
        >
          MC &amp; S CoWorker
        </div>
        <div className="text-sm" style={{ color: '#858481' }}>
          Your AI-powered accounting assistant
        </div>
      </div>

      {/* Card */}
      <div
        className="bg-white border p-10 w-full max-w-sm text-center"
        style={{ borderColor: '#d9d8d8' }}
      >
        <div
          className="text-lg font-semibold mb-1"
          style={{ color: '#142234', fontFamily: 'DM Serif Display, serif' }}
        >
          Sign in to CoWorker
        </div>
        <p className="text-sm mb-8" style={{ color: '#858481' }}>
          Use your MC&amp;S Microsoft 365 account
        </p>

        {/* Microsoft sign-in button — follows Microsoft brand guidelines */}
        <button
          onClick={() => auth.startMicrosoftLogin()}
          className="w-full flex items-center justify-center gap-3 border py-3 px-5 text-sm font-medium transition-colors hover:bg-gray-50"
          style={{
            borderColor: '#8c8c8c',
            color: '#5e5e5e',
            background: '#fff',
          }}
        >
          {/* Microsoft logo SVG (official mark) */}
          <svg width="21" height="21" viewBox="0 0 21 21" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect x="1" y="1" width="9" height="9" fill="#F25022" />
            <rect x="11" y="1" width="9" height="9" fill="#7FBA00" />
            <rect x="1" y="11" width="9" height="9" fill="#00A4EF" />
            <rect x="11" y="11" width="9" height="9" fill="#FFB900" />
          </svg>
          Sign in with Microsoft
        </button>
      </div>

      <p className="mt-8 text-xs" style={{ color: '#b0aea9' }}>
        MC &amp; S Accountants · CoWorker v3
      </p>
    </div>
  );
}
