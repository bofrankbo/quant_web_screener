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

  function _normalizedRows(rows) {
    const byDate = new Map();
    for (const row of rows || []) {
      if (!row || !row.date) continue;
      byDate.set(String(row.date), {
        ...row,
        date: String(row.date),
        open: Number(row.open),
        high: Number(row.high),
        low: Number(row.low),
        close: Number(row.close),
        volume: Number(row.volume || 0),
        ma: row.ma == null ? null : Number(row.ma),
        bb_upper: row.bb_upper == null ? null : Number(row.bb_upper),
        bb_lower: row.bb_lower == null ? null : Number(row.bb_lower),
      });
    }
    return Array.from(byDate.values())
      .filter(r => Number.isFinite(r.open) && Number.isFinite(r.high) && Number.isFinite(r.low) && Number.isFinite(r.close))
      .sort((a, b) => a.date.localeCompare(b.date));
  }

  function _setAllSeriesData(state) {
    state.candles.setData(state.rows.map(r => ({
      time: r.date,
      open: r.open,
      high: r.high,
      low: r.low,
      close: r.close,
    })));
    state.ma.setData(state.rows.filter(r => r.ma != null).map(r => ({ time: r.date, value: r.ma })));
    if (state.bbUpper) {
      state.bbUpper.setData(state.rows.filter(r => r.bb_upper != null).map(r => ({ time: r.date, value: r.bb_upper })));
      state.bbLower.setData(state.rows.filter(r => r.bb_lower != null).map(r => ({ time: r.date, value: r.bb_lower })));
    }
    state.vol.setData(state.rows.map(r => ({
      time: r.date,
      value: r.volume,
      color: r.close >= r.open ? THEME.volumeUp : THEME.volumeDown,
    })));
  }

  function syncPriceSeries(state) {
    if (!state || !state.rows) return;
    state.candles.setData(state.rows.map(r => ({
      time: r.date,
      open: r.open,
      high: r.high,
      low: r.low,
      close: r.close,
    })));
    state.vol.setData(state.rows.map(r => ({
      time: r.date,
      value: r.volume,
      color: r.close >= r.open ? THEME.volumeUp : THEME.volumeDown,
    })));
  }

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
    const onLoadEarlier = opts.onLoadEarlier || null;

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
    const cleanRows = _normalizedRows(rows);

    candles.setData(cleanRows.map(r => ({
      time: r.date,
      open: r.open,
      high: r.high,
      low: r.low,
      close: r.close,
    })));

    const ma = chart.addLineSeries({ color: maColor, lineWidth: 1, priceLineVisible: false });
    ma.setData(cleanRows.filter(r => r.ma != null).map(r => ({ time: r.date, value: r.ma })));

    let bbUpper = null;
    let bbLower = null;
    if (showBb) {
      bbUpper = chart.addLineSeries({
        color: bbColor,
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
      });
      bbUpper.setData(cleanRows.filter(r => r.bb_upper != null).map(r => ({ time: r.date, value: r.bb_upper })));

      bbLower = chart.addLineSeries({
        color: bbColor,
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
      });
      bbLower.setData(cleanRows.filter(r => r.bb_lower != null).map(r => ({ time: r.date, value: r.bb_lower })));
    }

    const vol = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      scaleMargins: { top: volumeTopMargin, bottom: 0 },
    });
    vol.setData(cleanRows.map(r => ({
      time: r.date,
      value: r.volume,
      color: r.close >= r.open ? THEME.volumeUp : THEME.volumeDown,
    })));

    if (cleanRows.length) {
      const lastDate = new Date(cleanRows[cleanRows.length - 1].date);
      if (defaultRangeMonths > 0 && cleanRows.length > 1) {
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

    const state = {
      chart, ro, candles, ma, bbUpper, bbLower, vol,
      rows: cleanRows,
      earliestDate: cleanRows.length ? cleanRows[0].date : null,
      loadingMore: false,
      noMoreHistory: false,
    };
    chartState.set(container, state);

    if (onLoadEarlier) {
      // Suppress the initial setVisibleRange callback by waiting one tick
      let ready = false;
      setTimeout(() => { ready = true; }, 0);

      chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (!ready || !range || state.loadingMore || state.noMoreHistory) return;
        if (range.from <= 10 && state.earliestDate) {
          state.loadingMore = true;
          const previousEarliestDate = state.earliestDate;
          Promise.resolve(onLoadEarlier(previousEarliestDate)).then(olderRows => {
            const merged = _normalizedRows([...(olderRows || []), ...state.rows]);
            const addedCount = merged.filter(r => r.date < previousEarliestDate).length;
            if (!olderRows || olderRows.length === 0 || addedCount === 0) {
              state.noMoreHistory = true;
            } else {
              const currentRange = state.chart.timeScale().getVisibleLogicalRange();
              state.rows = merged;
              state.earliestDate = merged[0].date;
              _setAllSeriesData(state);
              if (currentRange) {
                state.chart.timeScale().setVisibleLogicalRange({
                  from: currentRange.from + addedCount,
                  to: currentRange.to + addedCount,
                });
              }
            }
          }).catch(() => {}).finally(() => {
            state.loadingMore = false;
          });
        }
      });
    }

    return state;
  }

  function updateLastCandle(container, bar) {
    const state = chartState.get(container);
    if (!state) return;
    if (!state.rows || !state.rows.length) return;
    const idx = state.rows.findIndex(r => r.date === bar.time);
    const lastIdx = state.rows.length - 1;
    const shouldAppend = idx < 0 && String(bar.time) > String(state.rows[lastIdx].date);
    const targetIdx = idx >= 0 ? idx : lastIdx;
    if (targetIdx < 0) return;
    const nextRow = {
      ...state.rows[targetIdx],
      date: bar.time,
      open: bar.open ?? state.rows[targetIdx].open,
      high: bar.high ?? state.rows[targetIdx].high,
      low: bar.low ?? state.rows[targetIdx].low,
      close: bar.close ?? state.rows[targetIdx].close,
      volume: bar.volume ?? state.rows[targetIdx].volume,
    };
    if (shouldAppend) {
      state.rows.push(nextRow);
    } else {
      state.rows[targetIdx] = nextRow;
    }
    syncPriceSeries(state);
    if (state.chart && state.chart.timeScale) {
      state.chart.timeScale().scrollToRealTime();
    }
  }

  window.KlineChart = { render, destroy, updateLastCandle, theme: THEME };
})();
