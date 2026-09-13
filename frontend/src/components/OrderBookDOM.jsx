// L2 DOM. spoof walls sit on top in flickering yellow — fake supply,
// flagged so we remember they're ours.
export default function OrderBookDOM({ book, obi, mid }) {
  const asks = book?.asks ?? []
  const bids = book?.bids ?? []
  const spoofs = book?.spoof_asks ?? []

  if (!asks.length && !bids.length && !spoofs.length) {
    return <div className="dom dom-empty">no depth</div>
  }

  const maxSize = Math.max(
    ...asks.map(([, s]) => s),
    ...bids.map(([, s]) => s),
    ...spoofs.map(([, s]) => s),
    1e-9,
  )

  const withCum = (levels) => {
    let cum = 0
    return levels.map(([p, s]) => {
      cum += s
      return { p, s, cum }
    })
  }
  const askRows = withCum(asks)
  const bidRows = withCum(bids)
  const spoofRows = spoofs.map(([p, s]) => ({ p, s, cum: s }))

  const heat = (size) => Math.min(1, Math.sqrt(size / maxSize))
  const obiPct = ((obi ?? 0) * 100).toFixed(0)
  const obiCls = (obi ?? 0) > 0.15 ? 'bid-heavy' : (obi ?? 0) < -0.15 ? 'ask-heavy' : ''

  const Row = ({ level, side }) => (
    <div className={`dom-row ${side}`}>
      <span className="dom-px">{level.p.toFixed(3)}</span>
      <span className="dom-cum">{level.cum.toFixed(0)}</span>
      <span className="dom-sz">{side === 'spoof' ? '⚠ SPOOF' : level.s.toFixed(0)}</span>
      <div
        className="dom-heat"
        style={{
          width: `${(level.s / maxSize) * 100}%`,
          opacity: side === 'spoof' ? 1 : 0.25 + heat(level.s) * 0.75,
        }}
      />
    </div>
  )

  return (
    <div className="dom">
      <div className="dom-colhead">
        <span>PX</span>
        <span>DEPTH</span>
        <span>SIZE</span>
      </div>
      {spoofRows.length > 0 && (
        <div className="dom-side dom-spoof-zone">
          {spoofRows.map((lv) => (
            <Row key={`s${lv.p}`} level={lv} side="spoof" />
          ))}
        </div>
      )}
      <div className="dom-side">
        {[...askRows].reverse().map((lv) => (
          <Row key={`a${lv.p}`} level={lv} side="ask" />
        ))}
      </div>
      <div className="dom-spread">
        <span>MID {mid != null ? mid.toFixed(3) : '—'}</span>
        <span className={`dom-obi ${obiCls}`}>OBI {obiPct}%</span>
      </div>
      <div className="dom-side">
        {bidRows.map((lv) => (
          <Row key={`b${lv.p}`} level={lv} side="bid" />
        ))}
      </div>
    </div>
  )
}
