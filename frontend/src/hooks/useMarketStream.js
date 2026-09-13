import { useEffect, useRef, useState } from 'react'

const WS_URL =
  import.meta.env.VITE_WS_URL || 'ws://127.0.0.1:8000/ws'

export function useMarketStream() {
  const [event, setEvent] = useState(null)
  const [connected, setConnected] = useState(false)
  const [fills, setFills] = useState([])
  const wsRef = useRef(null)
  const reconnectRef = useRef(null)

  useEffect(() => {
    let stopped = false

    const connect = () => {
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
      }

      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data)
          setEvent(data)
          const incoming = data.new_fills ?? data.new_markers
          if (incoming && incoming.length) {
            setFills((prev) => {
              const next = [...prev, ...incoming]
              return next.slice(-60)
            })
          }
        } catch (e) {
        }
      }

      ws.onclose = () => {
        setConnected(false)
        if (!stopped) {
          reconnectRef.current = setTimeout(connect, 1500)
        }
      }

      ws.onerror = () => {
        ws.close()
      }
    }

    connect()

    return () => {
      stopped = true
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
      if (wsRef.current) wsRef.current.close()
    }
  }, [])

  return { event, connected, fills }
}
