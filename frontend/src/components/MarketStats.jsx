const RESOURCES = ['cpu_cores', 'gpu_slices', 'ram_pages', 'bandwidth_mbps']

function saturationColor(sat) {
  // Low saturation (scarce) -> red; high saturation (well-supplied) -> green.
  if (sat < 0.18) return '#ff3b5c'
  if (sat < 0.4) return '#ff9e1b'
  return '#00ff9c'
}

/**
 * MarketStats — grid of per-resource quote cards with mid-price and a
 * market-saturation bar. The bar turns red when supply enters the hoarding
 * regime (< 18%), matching the agent's scarcity threshold.
 */
export default function MarketStats({ quotes }) {
  if (!quotes) return <div className="panel"><div className="panel-title">Market Saturation</div></div>
  return (
    <div className="panel">
      <div className="panel-title">Market Saturation · Live Quotes</div>
      <div className="stats-grid">
        {RESOURCES.map((r) => {
          const q = quotes[r]
          if (!q) return null
          const satPct = (q.market_saturation * 100).toFixed(0)
          const obiPct = ((q.obi ?? 0) * 100).toFixed(0)
          const obiCls = q.obi > 0.15 ? 'pos' : q.obi < -0.15 ? 'neg' : ''
          return (
            <div className="stat-card" key={r}>
              <div className="name">{r.replace(/_/g, ' ')}</div>
              <div className="price">${q.mid_price.toFixed(3)}</div>
              <div className="sat-bar">
                <span
                  style={{
                    width: `${q.market_saturation * 100}%`,
                    background: saturationColor(q.market_saturation),
                  }}
                />
              </div>
              <div className="sat-label">
                sat {satPct}% · supply {q.supply.toFixed(0)}/{q.capacity.toFixed(0)}
                {' · '}
                <span className={`obi ${obiCls}`}>obi {obiPct}%</span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
