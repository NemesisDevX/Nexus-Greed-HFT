import { useEffect, useRef } from 'react'

// raw-wire tape. pure theatre — zero cost to the trading path.

const HEX = '0123456789abcdef'
const rnd = (n) => (Math.random() * n) | 0
const hexByte = () => HEX[rnd(16)] + HEX[rnd(16)]
const hexRun = (n) => Array.from({ length: n }, hexByte).join(' ')

function asciiOf(str) {
  return str
    .split('')
    .map((ch) => (/[ -~]/.test(ch) ? ch : '.'))
    .join('')
    .slice(0, 16)
    .padEnd(16, '.')
}

// one event -> fake frame header + 16-byte rows; gutter embeds real fill fields
function dumpEvent(ev) {
  const lines = []
  const t = (performance.now() * 1000) | 0
  const payloadLen = JSON.stringify(ev).length
  lines.push(
    `◤ ${String(t).slice(-8)}  OP 0x81  FIN  LEN ${String(payloadLen).padStart(6, '0')}  ws://ingress`,
  )
  const fields = (ev.new_fills || []).slice(0, 3).map(
    (f) => `${f.regime || f.side} ${f.resource} ${Number(f.size).toFixed(0)}@${Number(f.price).toFixed(2)}`,
  )
  const seed = fields.length ? fields : [`tick ${ev.tick}`]
  for (const s of seed) {
    const addr = `0x${hexByte()}${hexByte()}${hexByte()}`.toUpperCase()
    lines.push(`  ${addr}  ${hexRun(16)}  |${asciiOf(s)}|`)
  }
  return lines
}

export default function PacketStream({ event }) {
  const boxRef = useRef(null)
  const linesRef = useRef([])
  const pendingRef = useRef([])

  // queue lines, don't re-render per event — interval below paints
  useEffect(() => {
    if (event) pendingRef.current.push(...dumpEvent(event))
    if (pendingRef.current.length > 400) {
      pendingRef.current = pendingRef.current.slice(-200)
    }
  }, [event])

  useEffect(() => {
    const id = setInterval(() => {
      const el = boxRef.current
      if (!el) return
      // FIXME: fabricating idle rows when the queue drains is a hack,
      // but a stalling tape looks worse than a fake one
      for (let i = 0; i < 3; i++) {
        const line = pendingRef.current.shift()
          || `  0x${hexByte()}${hexByte()}${hexByte()}`.toUpperCase()
            + `  ${hexRun(16)}  |${asciiOf('idle' + rnd(99))}|`
        linesRef.current.push(line)
      }
      if (linesRef.current.length > 46) {
        linesRef.current = linesRef.current.slice(-46)
      }
      el.textContent = linesRef.current.join('\n')
      el.scrollTop = el.scrollHeight
    }, 45)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="packet-stream">
      <pre ref={boxRef} className="packet-tape" />
    </div>
  )
}
