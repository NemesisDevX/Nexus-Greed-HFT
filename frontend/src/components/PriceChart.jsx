import { createChart, CrosshairMode } from 'lightweight-charts'
import { useEffect, useRef } from 'react'

function markerStyle(regime, side) {
  if (regime === 'CORNER') {
    return { color: '#00e0ff', position: 'belowBar', shape: 'arrowUp', text: 'SWEEP' }
  }
  if (regime === 'DIP') {
    return { color: '#b8ff00', position: 'belowBar', shape: 'arrowUp', text: 'DIP' }
  }
  if (side === 'BUY') {
    return { color: '#00ff9c', position: 'belowBar', shape: 'arrowUp', text: 'BUY' }
  }
  if (regime === 'SQUEEZE') {
    return { color: '#ff2fd6', position: 'aboveBar', shape: 'arrowDown', text: 'SQZ' }
  }
  if (regime === 'HOARD') {
    return { color: '#ff3b5c', position: 'aboveBar', shape: 'arrowDown', text: 'HOARD' }
  }
  return { color: '#ffd24a', position: 'aboveBar', shape: 'circle', text: 'SELL' }
}

export default function PriceChart({ resource, candle, markers }) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef(null)
  const priceLineRef = useRef(null)
  const lastCandleTimeRef = useRef(0)
  const seededRef = useRef(false)

  useEffect(() => {
    if (!containerRef.current) return
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: 'solid', color: '#04060a' },
        textColor: '#5a7290',
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(42, 74, 107, 0.18)', style: 2 },
        horzLines: { color: 'rgba(42, 74, 107, 0.18)', style: 2 },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: '#ff9e1b', width: 1, style: 2, labelBackgroundColor: '#1a2a3d' },
        horzLine: { color: '#ff9e1b', width: 1, style: 2, labelBackgroundColor: '#1a2a3d' },
      },
      rightPriceScale: {
        borderColor: '#1a2a3d',
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      timeScale: {
        borderColor: '#1a2a3d',
        timeVisible: true,
        secondsVisible: true,
        rightOffset: 4,
      },
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
    })
    const series = chart.addCandlestickSeries({
      upColor: '#00ff9c',
      downColor: '#ff3b5c',
      borderUpColor: '#00ff9c',
      borderDownColor: '#ff3b5c',
      wickUpColor: '#00ff9c',
      wickDownColor: '#ff3b5c',
      priceLineVisible: false,
    })
    chartRef.current = chart
    seriesRef.current = series

    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        chart.applyOptions({ width: e.contentRect.width, height: e.contentRect.height })
      }
    })
    ro.observe(containerRef.current)

    return () => {
      ro.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      priceLineRef.current = null
      seededRef.current = false
    }
  }, [resource])

  useEffect(() => {
    if (!seriesRef.current || !candle) return
    seriesRef.current.update(candle)
    if (!priceLineRef.current) {
      priceLineRef.current = seriesRef.current.createPriceLine({
        price: candle.close,
        color: '#00e0ff',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: '',
      })
    } else {
      priceLineRef.current.applyOptions({ price: candle.close })
    }
    if (!seededRef.current) {
      seededRef.current = true
      lastCandleTimeRef.current = candle.time
    }
    if (candle.time !== lastCandleTimeRef.current) {
      lastCandleTimeRef.current = candle.time
      if (chartRef.current) chartRef.current.timeScale().scrollToRealTime()
    }
  }, [candle])

  useEffect(() => {
    if (!seriesRef.current || !markers) return
    const styled = markers
      .filter((m) => m.resource === resource)
      .map((m) => ({
        time: m.time,
        ...markerStyle(m.regime, m.side),
        text: `${m.text || markerStyle(m.regime, m.side).text} ${m.size.toFixed(0)}@${m.price.toFixed(1)}`,
      }))
    styled.sort((a, b) => a.time - b.time)  // lib wants ascending
    try {
      seriesRef.current.setMarkers(styled)
    } catch (e) {
      // marker before first candle throws — whatever
    }
  }, [markers, resource])

  return <div className="chart-wrap" ref={containerRef} />
}
