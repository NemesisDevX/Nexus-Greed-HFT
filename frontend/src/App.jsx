import { useEffect, useRef, useState } from 'react'
import { useMarketStream } from './hooks/useMarketStream.js'
import PnLCounter from './components/PnLCounter.jsx'
import MarketStats from './components/MarketStats.jsx'
import PriceChart from './components/PriceChart.jsx'
import OrderBookDOM from './components/OrderBookDOM.jsx'
import PacketStream from './components/PacketStream.jsx'
import RiskMetrics from './components/RiskMetrics.jsx'

const CHARTED = ['cpu_cores', 'gpu_slices']

const FEED_TAGS = {
  CORNER: 'SWEEP', SQUEEZE: 'SQUEEZE', HOARD: 'HOARD SELL',
  SPOOF: 'SPOOF WALL', CANCEL: 'SPOOF CANCELLED', DIP: 'DIP BOUGHT',
}
const FEED_CLASSES = ['CORNER', 'SQUEEZE', 'HOARD', 'SPOOF', 'CANCEL', 'DIP']

function feedTag(f) {
  return FEED_TAGS[f.regime] || f.side
}

function feedClass(f) {
  return FEED_CLASSES.includes(f.regime) ? f.regime : f.side
}

const FLASH_CLASS = {
  CORNER: 'go-red', SQUEEZE: 'go-green', SPOOF: 'go-yellow',
  CANCEL: 'go-amber', DIP: 'go-lime',
}

export default function App() {
  const { event, connected, fills } = useMarketStream()
  const ledger = event?.ledger
  const quotes = event?.quotes
  const candles = event?.candles
  const markers = event?.markers
  const [flash, setFlash] = useState('')
  const lastFlashRef = useRef({})
  const flashTimerRef = useRef(null)

  // Full-screen combat flash per fill regime, rate-capped per colour so
  // stacked fills intensify instead of strobing to white.
  useEffect(() => {
    const last = fills[fills.length - 1]
    if (!last) return
    const cls = FLASH_CLASS[last.regime]
    if (!cls) return
    const now = performance.now()
    if (now - (lastFlashRef.current[cls] || 0) < 120) return
    lastFlashRef.current[cls] = now
    setFlash(cls)
    clearTimeout(flashTimerRef.current)
    flashTimerRef.current = setTimeout(() => setFlash(''), 520)
  }, [fills])

  return (
    <div className="app">
      <div className={`war-flash ${flash}`} />
      <header className="header">
        <div className="brand">
          <h1>
            NEXUS<span className="accent">·GREED</span>
          </h1>
          <span className="tag">Predatory Liquidity Engine</span>
        </div>
        <div className="status-pill">
          <span className={`status-dot ${connected ? 'live' : ''}`} />
          {connected ? 'STREAM LIVE' : 'RECONNECTING…'}
          {ledger && <span style={{ marginLeft: 8, color: 'var(--muted)' }}>· tick {ledger.tick}</span>}
        </div>
      </header>

      <div className="top-row">
        <PnLCounter ledger={ledger} />
        <MarketStats quotes={quotes} />
      </div>

      <RiskMetrics ledger={ledger} quotes={quotes} />

      <div className="charts">
        {CHARTED.map((r) => (
          <div className="panel chart-panel" key={r}>
            <div className="chart-header">
              <span className="title">{r}</span>
              <span className="legend">
                <span><span className="dot" style={{ background: '#00ff9c' }} />BUY</span>
                <span><span className="dot" style={{ background: '#00e0ff' }} />SWEEP</span>
                <span><span className="dot" style={{ background: '#ff2fd6' }} />SQZ</span>
                <span><span className="dot" style={{ background: '#b8ff00' }} />DIP</span>
                <span><span className="dot" style={{ background: '#ffd24a' }} />SELL</span>
              </span>
            </div>
            <PriceChart resource={r} candle={candles?.[r]} markers={markers} />
          </div>
        ))}
      </div>

      <div className="depth-row">
        {CHARTED.map((r) => {
          const nSpoof = quotes?.[r]?.book?.spoof_asks?.length ?? 0
          return (
            <div className="panel dom-panel" key={r}>
              <div className="panel-title">
                Level-2 DOM · {r}
                {nSpoof > 0 && (
                  <span className="spoof-badge">⚠ {nSpoof} PHANTOM WALLS</span>
                )}
              </div>
              <OrderBookDOM
                book={quotes?.[r]?.book}
                obi={quotes?.[r]?.obi}
                mid={quotes?.[r]?.mid_price}
              />
            </div>
          )
        })}

        <div className="panel feed-panel">
          <div className="panel-title">Live Trade Feed</div>
          <div className="log-panel">
            {fills.length === 0 && (
              <div className="log-line" style={{ color: 'var(--muted)' }}>
                waiting for fills…
              </div>
            )}
            {fills
              .slice()
              .reverse()
              .map((f, i) => (
                <div className={`log-line ${feedClass(f)}`} key={`${f.tick}-${i}`}>
                  [tick {f.tick}] {feedTag(f).padEnd(10)} {f.resource.padEnd(14)}{' '}
                  size={f.size.toFixed(2).padStart(8)} @ {f.price.toFixed(3).padStart(8)}  {f.rationale}
                </div>
              ))}
          </div>
        </div>
      </div>

      <div className="panel packet-panel">
        <div className="panel-title">
          Raw Wire · Order Ingress <span className="wire-hz">ws://edge:8000 · binary frames</span>
        </div>
        <PacketStream event={event} />
      </div>
    </div>
  )
}
