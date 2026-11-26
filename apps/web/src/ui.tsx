import { CircleAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import { errorMessage } from './format'

export function Feedback({ error, retry }: { error: Error; retry?: () => void }) {
  return (
    <div className="feedback" role="alert">
      <CircleAlert size={17} aria-hidden="true" />
      <span>{errorMessage(error)}</span>
      {retry && <button type="button" className="button secondary" onClick={retry}>重试</button>}
    </div>
  )
}

export function Loading({ children }: { children: ReactNode }) {
  return <p className="loading" role="status"><span className="spinner" aria-hidden="true" />{children}</p>
}
