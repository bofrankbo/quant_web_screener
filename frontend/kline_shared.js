(function () {
  const THEME = {
    background: '#0d1117',
    grid: '#161b22',
    border: '#30363d',
    text: '#e6edf3',
    up: '#f85149',
    down: '#3fb950',
    ma: '#ffa726',
    bb: '#ab47bc',
    volumeUp: 'rgba(248,81,73,0.45)',
    volumeDown: 'rgba(63,185,80,0.45)',
  };

  const chartState = new WeakMap();

  function destroy(container) {
    const state = chartState.get(container);
    if (!state) return;
    if (state.ro) state.ro.disconnect();
    if (state.chart) state.chart.remove();
    chartState.delete(container);
  }

  function render(container, rows, opts = {}) {
    destroy(container);

    const showBb = !!opts.showBb;
    const volumeTopMargin = opts.volumeTopMargin ?? 0.91;
    const defaultRangeMonths = opts.defaultRangeMonths ?? 3;
    const maColor = opts.maColor || THEME.ma;
    const bbColor = opts.bbColor || THEME.bb;
    const upColor = opts.upColor || THEME.up;
    const downColor = opts.downColor || THEME.down;

    const chart = LightweightCharts.createChart(container, {
      layout: { background: { color: THEME.background }, textColor: THEME.text },
      grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: { borderColor: THEME.border, timeVisible: false },
      width: container.clientWidth,
      height: container.clientHeight,
    });

    const candles = chart.addCandlestickSeries({
      upColor,
      downColor,
      borderUpColor: upColor,
      borderDownColor: downColor,
      wickUpColor: upColor,
      wickDownColor: downColor,
    });
    candles.setData(rows.map(r => ({
      time: r.date,
      open: r.open,
      high: r.high,
      low: r.low,
      close: r.close,
    })));

    const ma = chart.addLineSeries({ color: maColor, lineWidth: 1, priceLineVisible: false });
    ma.setData(rows.filter(r => r.ma != null).map(r => ({ time: r.date, value: r.ma })));

    let bbUpper = null;
    let bbLower = null;
    if (showBb) {
      bbUpper = chart.addLineSeries({
        color: bbColor,
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
      });
      bbUpper.setData(rows.filter(r => r.bb_upper != null).map(r => ({ time: r.date, value: r.bb_upper })));

      bbLower = chart.addLineSeries({
        color: bbColor,
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
      });
      bbLower.setData(rows.filter(r => r.bb_lower != null).map(r => ({ time: r.date, value: r.bb_lower })));
    }

    const vol = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      scaleMargins: { top: volumeTopMargin, bottom: 0 },
    });
    vol.setData(rows.map(r => ({
      time: r.date,
      value: r.volume,
      color: r.close >= r.open ? THEME.volumeUp : THEME.volumeDown,
    })));

    if (rows.length) {
      const lastDate = new Date(rows[rows.length - 1].date);
      if (defaultRangeMonths > 0 && rows.length > 1) {
        const fromDate = new Date(lastDate);
        fromDate.setMonth(fromDate.getMonth() - defaultRangeMonths);
        chart.timeScale().setVisibleRange({
          from: fromDate.toISOString().slice(0, 10),
          to: lastDate.toISOString().slice(0, 10),
        });
      } else {
        chart.timeScale().fitContent();
      }
    }

    const ro = new ResizeObserver(() => {
      chart.applyOptions({
        width: container.clientWidth,
        height: container.clientHeight,
      });
    });
    ro.observe(container);

    const state = { chart, ro, candles, ma, bbUpper, bbLower, vol };
    chartState.set(container, state);
    return state;
  }

  function updateLastCandle(container, bar) {
    const state = chartState.get(container);
    if (!state) return;
    state.candles.update({ time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close });
    if (bar.volume != null) {
      state.vol.update({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? THEME.volumeUp : THEME.volumeDown,
      });
    }
  }

  window.KlineChart = { render, destroy, updateLastCandle, theme: THEME };
})();
