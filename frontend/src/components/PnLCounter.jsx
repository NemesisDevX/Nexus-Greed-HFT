export default function PnLCounter({ ledger }) {
  if (!ledger) {
    return (
      <div className="panel pnl-panel">
        <div className="panel-title">Unrealized P&L</div>
        <div className="pnl-value">—</div>
      </div>
    )
  }

  const roi = ledger.roi_pct
  const profit = roi >= 0
  const sign = profit ? '+' : ''

  return (
    <div className="panel pnl-panel">
      <div className="panel-title">Command Center · Unrealized P&L</div>
      <div className={`pnl-value ${profit ? 'profit' : 'loss'}`}>
        {sign}
        {roi.toFixed(2)}%
      </div>
      <div className="pnl-sub">
        <div className="metric">
          <div className="k">Equity (MTM)</div>
          <div className="v">${ledger.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
        </div>
        <div className="metric">
          <div className="k">Realized</div>
          <div className={`v ${ledger.realized_pnl >= 0 ? 'pos' : 'neg'}`}>
            {ledger.realized_pnl >= 0 ? '+' : ''}
            {ledger.realized_pnl.toLocaleString(undefined, { maximumFractionDigits: 0 })}
          </div>
        </div>
        <div className="metric">
          <div className="k">Cash</div>
          <div className="v">${ledger.cash.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
        </div>
        <div className="metric">
          <div className="k">Trades</div>
          <div className="v">{ledger.num_trades}</div>
        </div>
        <div className="metric">
          <div className="k">Epsilon</div>
          <div className="v">{ledger.epsilon.toFixed(3)}</div>
        </div>
      </div>
    </div>
  )
}
