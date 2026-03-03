import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { get } from '../api'
import ChatPage from './ChatPage'

export default function HomePage() {
  const navigate = useNavigate()
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let cancelled = false
    get<{ total: number }>('/api/docs', { limit: 1 })
      .then((data) => {
        if (cancelled) return
        if (data.total === 0) {
          navigate('/upload', { replace: true })
        } else {
          setReady(true)
        }
      })
      .catch(() => {
        if (!cancelled) setReady(true) // on error, show chat anyway
      })
    return () => {
      cancelled = true
    }
  }, [navigate])

  if (!ready) return null
  return <ChatPage />
}
