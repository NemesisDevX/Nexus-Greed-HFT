/**
 * RiskMetrics — institutional risk strip for the command center.
 *
 * Cards: historical VaR(95) on the tick equity curve, annualized Sharpe,
 * the predatory engine's Squeeze counter (counterparties forced across our
 * +300% walls), completed cornering sweeps, and the live OBI of the
 * scarcest instrument on the wire.
 */
export default function RiskMetrics({ ledger, quotes }) {
  if (!ledger) {
    return (
      <div className="panel risk-panel">
        <div className="panel-title">Risk Engine</div>
      </div>
    )
  }

  // Live OBI of the tightest book — the instrument most exposed to a squeeze.
  let tightest = null
  if (quotes) {
    for (const r of Object.values(quotes)) {
      if (!tightest || r.market_saturation < tightest.market_saturation) tightest = r
    }
  }
  const obi = tightest?.obi ?? 0
  const obiCls = obi > 0.15 ? 'pos' : obi < -0.15 ? 'neg' : ''
  const sharpe = ledger.sharpe ?? 0
  const var95 = ledger.var_95 ?? 0
  const equity = ledger.equity || 1

  return (
    <div className="panel risk-panel">
      <div className="panel-title">Risk Engine · Predatory Liquidity</div>
      <div className="metric-cards">
        <div className="metric-card">
          <div className="mc-label">VaR 95%</div>
          <div className="mc-value amber">${var95.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
          <div className="mc-sub">{((var95 / equity) * 100).toFixed(2)}% of equity / tick</div>
        </div>
        <div className="metric-card">
          <div className="mc-label">Sharpe Ratio</div>
          <div className={`mc-value ${sharpe >= 1 ? 'pos' : sharpe < 0 ? 'neg' : ''}`}>
            {sharpe.toFixed(2)}
          </div>
          <div className="mc-sub">annualized · rolling window</div>
        </div>
        <div className="metric-card hot">
          <div className="mc-label">Agents Squeezed</div>
          <div className="mc-value squeeze">{ledger.agents_squeezed ?? 0}</div>
          <div className="mc-sub">crossed the +300% walls</div>
        </div>
        <div className="metric-card">
          <div className="mc-label">Cornering Sweeps</div>
          <div className="mc-value cyan">{ledger.sweeps_executed ?? 0}</div>
          <div className="mc-sub">depth fully lifted</div>
        </div>
        <div className="metric-card">
          <div className="mc-label">OBI · {tightest?.resource ?? '—'}</div>
          <div className={`mc-value ${obiCls}`}>{(obi * 100).toFixed(0)}%</div>
          <div className="mc-sub">{obi > 0 ? 'bid-heavy — squeeze risk' : 'offer-heavy'}</div>
        </div>
      </div>
    </div>
  )
}
