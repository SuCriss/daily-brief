/* ==========================================================================
   K 线弹窗：纯 Canvas 渲染，无第三方依赖。
   功能：日/周/月/分钟 K 线、均线、成交量、十字光标、悬浮明细、弹窗内切换周期。
   ========================================================================== */
(function () {
  'use strict';

  var PERIODS = [
    { key: 'day', label: '日K' },
    { key: 'week', label: '周K' },
    { key: 'month', label: '月K' },
    { key: 'm60', label: '60分' },
    { key: 'm15', label: '15分' },
    { key: 'm5', label: '5分' }
  ];
  var MA_DEFS = [
    { key: 'ma5', label: 'MA5', color: '#e2b93b' },
    { key: 'ma10', label: 'MA10', color: '#6b96ff' },
    { key: 'ma20', label: 'MA20', color: '#c07bff' },
    { key: 'ma60', label: 'MA60', color: '#3ecf8e' }
  ];

  /* 颜色从 CSS 变量读取，自动跟随主题 */
  function themeColor(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name);
      return (v && v.trim()) || fallback;
    } catch (e) {
      return fallback;
    }
  }
  function theme() {
    return {
      up: themeColor('--up', '#f0616d'),
      down: themeColor('--down', '#3ecf8e'),
      ink300: themeColor('--ink-300', '#62666d'),
      ink400: themeColor('--ink-400', '#8a8f98'),
      ink500: themeColor('--ink-500', '#a3a9b3'),
      ink700: themeColor('--ink-700', '#e2e6ec'),
      surface: themeColor('--surface', '#0f1011'),
      line: themeColor('--line', '#23252a'),
      grid: themeColor('--line', '#23252a')
    };
  }

  /* 网格线用半透明，避免压过 K 线主体 */
  function gridColor() {
    var c = themeColor('--line', '#23252a');
    return c;
  }

  var state = {
    code: null,
    name: '',
    period: 'day',
    data: null,
    loading: false,
    hoverIndex: -1,
    visibleMa: { ma5: true, ma10: true, ma20: true, ma60: false }
  };

  /* ---------------- DOM ---------------- */
  var modal, canvas, ctx, titleEl, subEl, statsEl, periodsEl, legendEl, tipEl, errEl, chartWrap;
  var built = false;

  function buildModal() {
    if (built) return;
    modal = document.createElement('div');
    modal.className = 'kline-modal';
    modal.setAttribute('aria-hidden', 'true');
    modal.innerHTML = [
      '<div class="kline-backdrop" data-kline-close></div>',
      '<div class="kline-dialog" role="dialog" aria-modal="true" aria-label="股票K线详情">',
      '  <div class="kline-head">',
      '    <div class="kline-head-main">',
      '      <h2 class="kline-title" id="klineTitle">K线详情</h2>',
      '      <div class="kline-sub" id="klineSub"></div>',
      '    </div>',
      '    <button class="kline-close" type="button" data-kline-close aria-label="关闭">关闭</button>',
      '  </div>',
      '  <div class="kline-bar">',
      '    <div class="kline-periods" id="klinePeriods"></div>',
      '    <div class="kline-legend" id="klineLegend"></div>',
      '  </div>',
      '  <div class="kline-stats" id="klineStats"></div>',
      '  <div class="kline-chart-wrap" id="klineChartWrap">',
      '    <canvas id="klineCanvas"></canvas>',
      '    <div class="kline-tip" id="klineTip" hidden></div>',
      '    <div class="kline-err" id="klineErr" hidden></div>',
      '    <div class="kline-loading" id="klineLoading" hidden>加载中…</div>',
      '  </div>',
      '  <div class="kline-mentions" id="klineMentions"></div>',
      '</div>'
    ].join('');

    document.body.appendChild(modal);

    canvas = modal.querySelector('#klineCanvas');
    ctx = canvas.getContext('2d');
    titleEl = modal.querySelector('#klineTitle');
    subEl = modal.querySelector('#klineSub');
    statsEl = modal.querySelector('#klineStats');
    periodsEl = modal.querySelector('#klinePeriods');
    legendEl = modal.querySelector('#klineLegend');
    tipEl = modal.querySelector('#klineTip');
    errEl = modal.querySelector('#klineErr');
    chartWrap = modal.querySelector('#klineChartWrap');

    // 周期按钮
    periodsEl.innerHTML = PERIODS.map(function (p) {
      return '<button type="button" class="kline-period" data-period="' + p.key + '">' + p.label + '</button>';
    }).join('');
    periodsEl.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-period]');
      if (!btn) return;
      state.period = btn.dataset.period;
      renderPeriods();
      load();
    });

    // 均线图例（可点击切换）
    legendEl.innerHTML = MA_DEFS.map(function (m) {
      return '<button type="button" class="kline-ma" data-ma="' + m.key + '">' +
        '<i style="background:' + m.color + '"></i>' + m.label + '</button>';
    }).join('');
    legendEl.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-ma]');
      if (!btn) return;
      var k = btn.dataset.ma;
      state.visibleMa[k] = !state.visibleMa[k];
      renderLegend();
      draw();
    });

    modal.addEventListener('click', function (e) {
      if (e.target.closest('[data-kline-close]')) close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && modal.classList.contains('open')) close();
    });

    // 十字光标
    canvas.addEventListener('mousemove', onMove);
    canvas.addEventListener('mouseleave', function () {
      state.hoverIndex = -1;
      tipEl.hidden = true;
      draw();
    });
    // 触摸
    canvas.addEventListener('touchmove', function (e) {
      var t = e.touches[0];
      if (!t) return;
      onMove({ clientX: t.clientX, clientY: t.clientY });
      e.preventDefault();
    }, { passive: false });
    canvas.addEventListener('touchend', function () {
      state.hoverIndex = -1;
      tipEl.hidden = true;
      draw();
    });

    window.addEventListener('resize', function () {
      if (modal.classList.contains('open')) { resize(); draw(); }
    });
    // 系统亮/暗模式切换时重绘（Canvas 颜色取自 CSS 变量）
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onScheme = function () { if (modal.classList.contains('open')) draw(); };
      if (mq.addEventListener) mq.addEventListener('change', onScheme);
      else if (mq.addListener) mq.addListener(onScheme);
    }

    built = true;
  }

  function renderPeriods() {
    Array.prototype.forEach.call(periodsEl.children, function (b) {
      b.classList.toggle('on', b.dataset.period === state.period);
    });
  }

  function renderLegend() {
    Array.prototype.forEach.call(legendEl.children, function (b) {
      b.classList.toggle('off', !state.visibleMa[b.dataset.ma]);
    });
  }

  /* ---------------- 数据 ---------------- */
  function load() {
    if (!state.code) return;
    state.loading = true;
    errEl.hidden = true;
    modal.querySelector('#klineLoading').hidden = false;

    fetch('/api/kline?code=' + encodeURIComponent(state.code) +
          '&period=' + encodeURIComponent(state.period) + '&count=180')
      .then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
      })
      .then(function (res) {
        state.loading = false;
        modal.querySelector('#klineLoading').hidden = true;
        if (!res.ok) {
          var msg = (res.body && (res.body.detail || res.body.message)) || ('请求失败 ' + res.status);
          showError(msg);
          return;
        }
        state.data = res.body;
        state.name = res.body.name || state.code;
        titleEl.textContent = state.name + ' ' + state.code;
        renderStats();
        renderMentions(res.body.mentions || []);
        resize();
        draw();
      })
      .catch(function (e) {
        state.loading = false;
        modal.querySelector('#klineLoading').hidden = true;
        showError('网络错误：' + e.message);
      });
  }

  function showError(msg) {
    errEl.hidden = false;
    errEl.textContent = msg;
    ctx && ctx.clearRect(0, 0, canvas.width, canvas.height);
    statsEl.innerHTML = '';
    document.getElementById('klineMentions').innerHTML = '';
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '--';
    return Number(v).toFixed(digits === undefined ? 2 : digits);
  }

  function renderStats() {
    var d = state.data;
    if (!d || !d.bars || !d.bars.length) { statsEl.innerHTML = ''; return; }
    var bars = d.bars;
    var last = bars[bars.length - 1];
    var prev = bars.length > 1 ? bars[bars.length - 2] : last;
    var chg = last.close - prev.close;
    var pct = prev.close ? (chg / prev.close * 100) : 0;
    var cls = chg > 0 ? 'up' : (chg < 0 ? 'down' : 'flat');
    var sign = chg > 0 ? '+' : '';
    var label = d.period_label || d.period;

    statsEl.innerHTML = [
      '<div class="kstat"><span>最新价</span><strong class="' + cls + '">' + fmtNum(last.close) + '</strong></div>',
      '<div class="kstat"><span>涨跌</span><strong class="' + cls + '">' + sign + fmtNum(chg) +
        ' (' + sign + fmtNum(pct) + '%)</strong></div>',
      '<div class="kstat"><span>今开</span><strong>' + fmtNum(last.open) + '</strong></div>',
      '<div class="kstat"><span>最高</span><strong>' + fmtNum(last.high) + '</strong></div>',
      '<div class="kstat"><span>最低</span><strong>' + fmtNum(last.low) + '</strong></div>',
      '<div class="kstat"><span>成交量</span><strong>' + fmtVol(last.volume) + '</strong></div>',
      '<div class="kstat"><span>周期</span><strong>' + label + '</strong></div>',
      '<div class="kstat"><span>数据源</span><strong>' + (d.source || '-') + '</strong></div>'
    ].join('');
  }

  function fmtVol(v) {
    if (v === null || v === undefined || isNaN(v)) return '--';
    v = Number(v);
    if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (v >= 1e4) return (v / 1e4).toFixed(2) + '万';
    return String(Math.round(v));
  }

  function renderMentions(list) {
    var el = document.getElementById('klineMentions');
    if (!list || !list.length) { el.innerHTML = ''; return; }
    el.innerHTML = '<div class="kmentions-head">近72小时快讯提及 <strong>' + list.length + '</strong> 条</div>' +
      list.slice(0, 6).map(function (m) {
        return '<div class="kmention"><span class="kt">' + (m.published_at || '') + '</span>' +
          '<span class="kx">' + (m.title || '') + '</span></div>';
      }).join('');
  }

  /* ---------------- 绘制 ---------------- */
  function resize() {
    var w = chartWrap.clientWidth;
    var h = Math.max(300, Math.min(460, Math.round(w * 0.52)));
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    canvas._w = w;
    canvas._h = h;
  }

  function draw() {
    if (!ctx || !state.data || !state.data.bars || !state.data.bars.length) return;
    var bars = state.data.bars;
    var W = canvas._w || chartWrap.clientWidth;
    var H = canvas._h || 360;
    var T = theme();
    var UP = T.up;
    var DOWN = T.down;

    var padL = 8, padR = 62, padT = 10, padB = 18;
    var volH = Math.round(H * 0.22);
    var gap = 12;
    var priceH = H - padT - padB - volH - gap;
    var plotW = W - padL - padR;

    ctx.clearRect(0, 0, W, H);

    // 价格范围（含均线）
    var lo = Infinity, hi = -Infinity;
    bars.forEach(function (b) {
      lo = Math.min(lo, b.low);
      hi = Math.max(hi, b.high);
      MA_DEFS.forEach(function (m) {
        if (state.visibleMa[m.key] && b[m.key] != null) {
          lo = Math.min(lo, b[m.key]);
          hi = Math.max(hi, b[m.key]);
        }
      });
    });
    if (!isFinite(lo) || !isFinite(hi)) return;
    var pad = (hi - lo) * 0.06 || 1;
    lo -= pad; hi += pad;
    var range = hi - lo || 1;

    var yOf = function (v) { return padT + (hi - v) / range * priceH; };
    var n = bars.length;
    var step = plotW / n;
    var bw = Math.max(1.5, Math.min(step * 0.68, 14));
    var xOf = function (i) { return padL + step * (i + 0.5); };

    // 网格 + 价格刻度
    ctx.font = '11px -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif';
    ctx.textBaseline = 'middle';
    var gridN = 4;
    ctx.strokeStyle = T.grid;
    ctx.globalAlpha = 0.5;
    ctx.fillStyle = T.ink400;
    ctx.lineWidth = 1;
    for (var g = 0; g <= gridN; g++) {
      var v = hi - (range * g / gridN);
      var y = Math.round(yOf(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.textAlign = 'left';
      ctx.fillText(fmtNum(v), padL + plotW + 6, y);
    }
    ctx.globalAlpha = 1;

    // 成交量柱
    var vlo = 0, vhi = 0;
    bars.forEach(function (b) { vhi = Math.max(vhi, b.volume || 0); });
    var vTop = padT + priceH + gap;
    var vY = function (v) { return vTop + volH - (vhi ? (v / vhi) * volH : 0); };
    bars.forEach(function (b, i) {
      var up = b.close >= b.open;
      ctx.globalAlpha = 0.5;
      ctx.fillStyle = up ? UP : DOWN;
      var x = xOf(i) - bw / 2;
      var y = vY(b.volume || 0);
      ctx.fillRect(x, y, bw, vTop + volH - y);
      ctx.globalAlpha = 1;
    });

    // 日期刻度
    ctx.fillStyle = T.ink400;
    ctx.textAlign = 'center';
    var ticks = Math.min(5, n);
    for (var t = 0; t < ticks; t++) {
      var i = Math.round((n - 1) * t / (ticks - 1 || 1));
      var lbl = String(bars[i].date || '').slice(0, 10);
      if (lbl.length > 5 && state.period.indexOf('m') === 0) lbl = lbl.slice(5);
      ctx.fillText(lbl, xOf(i), H - 8);
    }

    // K 线实体
    bars.forEach(function (b, i) {
      var up = b.close >= b.open;
      var color = up ? UP : DOWN;
      var x = xOf(i);
      // 影线
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, yOf(b.high));
      ctx.lineTo(Math.round(x) + 0.5, yOf(b.low));
      ctx.stroke();
      // 实体
      var yo = yOf(b.open), yc = yOf(b.close);
      var top = Math.min(yo, yc);
      var hgt = Math.max(1, Math.abs(yc - yo));
      ctx.fillStyle = color;
      ctx.fillRect(x - bw / 2, top, bw, hgt);
      // 空心阳线更清晰（底色跟随主题，避免暗色模式下白块）
      if (up && bw > 3) {
        ctx.fillStyle = T.surface;
        ctx.fillRect(x - bw / 2 + 1, top + 1, Math.max(1, bw - 2), Math.max(0, hgt - 2));
        ctx.strokeStyle = color;
        ctx.strokeRect(x - bw / 2 + 0.5, top + 0.5, bw - 1, hgt - 1);
      }
    });

    // 均线
    MA_DEFS.forEach(function (m) {
      if (!state.visibleMa[m.key]) return;
      ctx.strokeStyle = m.color;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      var started = false;
      bars.forEach(function (b, i) {
        var v = b[m.key];
        if (v == null) { started = false; return; }
        var x = xOf(i), y = yOf(v);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });

    // 十字光标
    if (state.hoverIndex >= 0 && state.hoverIndex < n) {
      var i = state.hoverIndex;
      var b = bars[i];
      var hx = Math.round(xOf(i)) + 0.5;
      var hy = Math.round(yOf(b.close)) + 0.5;
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = T.ink400;
      ctx.globalAlpha = 0.7;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(hx, padT); ctx.lineTo(hx, padT + priceH); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(padL, hy); ctx.lineTo(padL + plotW, hy); ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      // 右侧价格标签
      ctx.fillStyle = T.ink700;
      var txt = fmtNum(b.close);
      var tw = ctx.measureText(txt).width + 8;
      ctx.fillRect(padL + plotW + 2, hy - 8, tw, 16);
      ctx.fillStyle = T.surface;
      ctx.textAlign = 'left';
      ctx.fillText(txt, padL + plotW + 6, hy);

      showTip(b, xOf(i), yOf(b.close));
    }
  }

  function showTip(b, x, y) {
    var prevPct = '';
    if (state.data.bars.length > 1) {
      var idx = state.data.bars.indexOf(b);
      if (idx > 0) {
        var p = state.data.bars[idx - 1].close;
        if (p) {
          var pc = (b.close - p) / p * 100;
          prevPct = (pc >= 0 ? '+' : '') + pc.toFixed(2) + '%';
        }
      }
    }
    var up = b.close >= b.open;
    tipEl.hidden = false;
    tipEl.innerHTML =
      '<div class="tip-date">' + (b.date || '') + '</div>' +
      '<div class="tip-grid">' +
      '<span>开</span><b>' + fmtNum(b.open) + '</b>' +
      '<span>高</span><b>' + fmtNum(b.high) + '</b>' +
      '<span>低</span><b>' + fmtNum(b.low) + '</b>' +
      '<span>收</span><b class="' + (up ? 'up' : 'down') + '">' + fmtNum(b.close) + '</b>' +
      '<span>涨跌</span><b class="' + (up ? 'up' : 'down') + '">' + prevPct + '</b>' +
      '<span>量</span><b>' + fmtVol(b.volume) + '</b>' +
      '</div>';

    var wrapRect = chartWrap.getBoundingClientRect();
    var tw = tipEl.offsetWidth || 150;
    var th = tipEl.offsetHeight || 90;
    var left = x + 14;
    if (left + tw > wrapRect.width) left = x - tw - 14;
    if (left < 0) left = 4;
    var top = Math.max(4, Math.min(y - th / 2, wrapRect.height - th - 4));
    tipEl.style.left = left + 'px';
    tipEl.style.top = top + 'px';
  }

  function onMove(e) {
    if (!state.data || !state.data.bars || !state.data.bars.length) return;
    var rect = canvas.getBoundingClientRect();
    var x = e.clientX - rect.left;
    var bars = state.data.bars;
    var W = canvas._w || rect.width;
    var padL = 8, padR = 62;
    var plotW = W - padL - padR;
    var step = plotW / bars.length;
    var i = Math.floor((x - padL) / step);
    if (i < 0) i = 0;
    if (i > bars.length - 1) i = bars.length - 1;
    if (i !== state.hoverIndex) {
      state.hoverIndex = i;
      draw();
    }
  }

  /* ---------------- 对外 ---------------- */
  function open(code, name) {
    buildModal();
    state.code = code;
    state.name = name || '';
    state.period = state.period || 'day';
    state.hoverIndex = -1;
    titleEl.textContent = (name || code) + ' ' + code;
    subEl.textContent = '数据来自公开行情接口，仅供参考';
    statsEl.innerHTML = '';
    document.getElementById('klineMentions').innerHTML = '';
    renderPeriods();
    renderLegend();
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('kline-open');
    resize();
    load();
  }

  function close() {
    if (!modal) return;
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('kline-open');
    tipEl.hidden = true;
  }

  // 事件委托：任何带 data-kline-code 的元素都能打开弹窗
  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-kline-code]');
    if (!el) return;
    // 若元素同时是链接且用户按住修饰键，走原生跳转
    if (e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    open(el.dataset.klineCode, el.dataset.klineName || '');
  });

  window.KlineModal = { open: open, close: close };
})();
