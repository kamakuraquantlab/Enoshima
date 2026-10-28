import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { LineChart, BarChart, ScatterChart, HeatmapChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
  VisualMapComponent, MarkLineComponent, AxisPointerComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart, BarChart, ScatterChart, HeatmapChart,
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
  VisualMapComponent, MarkLineComponent, AxisPointerComponent, CanvasRenderer,
])

// One ECharts instance per mount, resized with its box. `option` is replaced
// whole on every change, so a chart never keeps a series it no longer has.
// `group` links charts' zoom and crosshair, e.g. an equity curve and its drawdown.
// `onZoom` hears the window a person zooms or slides to, as axis values, once it
// rests; a new `option` sets the window without calling it. It is read late on
// purpose: a connected chart fires before it has applied its partner's window.
export default function Chart({ option, height = 260, onClick, onZoom, group }) {
  const ref = useRef(null)
  const chart = useRef(null)

  useEffect(() => {
    chart.current = echarts.init(ref.current, null, { renderer: 'canvas' })
    if (group) { chart.current.group = group; echarts.connect(group) }
    const observer = new ResizeObserver(() => chart.current?.resize())
    observer.observe(ref.current)
    return () => { observer.disconnect(); chart.current.dispose(); chart.current = null }
  }, [])

  useEffect(() => { chart.current?.setOption(option, true) }, [option])

  useEffect(() => {
    if (!onClick || !chart.current) return
    chart.current.on('click', onClick)
    return () => chart.current?.off('click', onClick)
  }, [onClick])

  useEffect(() => {
    if (!onZoom || !chart.current) return
    let timer
    const handler = () => {
      clearTimeout(timer)
      timer = setTimeout(() => {
        const zoom = chart.current?.getOption().dataZoom?.[0]
        if (zoom) onZoom(zoom.startValue, zoom.endValue)
      }, 250)
    }
    chart.current.on('datazoom', handler)
    return () => { clearTimeout(timer); chart.current?.off('datazoom', handler) }
  }, [onZoom])

  return <div ref={ref} className="chart" style={{ height }} />
}
