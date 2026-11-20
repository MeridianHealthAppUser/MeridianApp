(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('insights_charts', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* Insights and Cohorts charts (templates/portal/insights*.html, static/css/insights.css), Nov 2026. Each
   [data-insight-chart] is drawn from its spec in the page's insight-charts JSON (portal/insights_views.py): lines, bars
   or stacked bars by month (or by month of subscription), a legend for two or more series, and its numbers as a table.
   Pointing at a chart, or the arrow keys on it, reads out every series at that month. The practice's notes
   (insight-chart-notes) mark their month with a dashed line and a button that shows the note. Names go in with
   textContent only. The chart redraws to its width. */
(() => {
  const read = (id) => {
    const node = document.getElementById(id);
    try { return node ? JSON.parse(node.textContent) : null; } catch (error) { return null; }
  };
  const specs = read('insight-charts');
  if (!specs) return;
  const notes = read('insight-chart-notes') || [];
  const NS = 'http://www.w3.org/2000/svg';
  const SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const grouped = (number) => String(number).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  const tenth = (value) => String(Math.round(value * 10) / 10);
  const FORMAT = {
    int: (value) => grouped(Math.round(value)),
    rand: (value) => `R${grouped(Math.round(value))}`,
    pct: (value) => `${tenth(value)}%`,
    kg: (value) => `${tenth(value)} kg`,
  };
  const make = (tag, className) => Object.assign(document.createElement(tag), className ? { className } : {});
  const svg = (tag, attributes = {}) => {
    const node = document.createElementNS(NS, tag);
    Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
    return node;
  };
  const monthly = (spec) => spec.axis !== 'months';
  const parts = (key) => key.split('-').map(Number);
  function longLabel(spec, key) {
    if (!monthly(spec)) return `Month ${key.slice(1)}`;
    const [year, month] = parts(key);
    return `${LONG[month - 1]} ${year}`;
  }
  const noteDate = (iso) => {
    const [year, month, day] = parts(iso);
    return `${day} ${SHORT[month - 1]} ${year}`;
  };

  // Round ticks from 0 (or below, if a value is) to just above the largest value.
  function scale(spec, low, high) {
    if (spec.max) return { bottom: 0, top: spec.max, step: spec.max / 4 };
    if (high <= low) high = low + 1;
    const rough = (high - Math.min(0, low)) / 4;
    const power = 10 ** Math.floor(Math.log10(rough));
    let step = [1, 2, 2.5, 5, 10].map((factor) => factor * power).find((candidate) => candidate >= rough);
    if (spec.format === 'int' || spec.format === 'rand') step = Math.max(1, Math.ceil(step));
    return { bottom: Math.min(0, Math.floor(low / step) * step), top: Math.ceil(high / step) * step, step };
  }

  function table(spec, host) {
    const box = host.querySelector('.insight-chart__table > div');
    if (!box) return;
    const sheet = make('table', 'insights-table');
    const head = sheet.createTHead().insertRow();
    [monthly(spec) ? 'Month' : 'Month of subscription', ...spec.series.map((series) => series.label)].forEach((text) => {
      const cell = make('th');
      cell.scope = 'col';
      cell.textContent = text;
      head.append(cell);
    });
    const body = sheet.createTBody();
    spec.x.forEach((key, index) => {
      const row = body.insertRow();
      const name = make('th');
      name.scope = 'row';
      name.textContent = longLabel(spec, key);
      row.append(name);
      spec.series.forEach((series) => {
        const value = series.values[index];
        row.insertCell().textContent = value === null || value === undefined ? '—' : FORMAT[spec.format](value)
          + (series.counts && value !== null ? ` (${series.counts[index]})` : '');
      });
    });
    box.replaceChildren(make('div', 'insights-table-scroll'));
    box.firstChild.append(sheet);
  }

  function legend(spec, host) {
    const box = host.querySelector('.insight-chart__legend');
    if (!box) return;
    box.replaceChildren();
    if (spec.series.length < 2) return;
    spec.series.forEach((series) => {
      const item = make('span', 'insight-chart__key');
      const swatch = make('i', `insight-tone insight-tone--${series.tone}${spec.kind === 'line' ? ' insight-chart__swatch-line' : ''}`);
      swatch.setAttribute('aria-hidden', 'true');
      item.append(swatch, document.createTextNode(series.label));
      box.append(item);
    });
  }

  function draw(spec, host) {
    const plot = host.querySelector('.insight-chart__plot');
    const width = Math.max(260, Math.round(plot.clientWidth));
    const height = width < 480 ? 200 : 236;
    const stacked = spec.kind === 'stacked';
    const n = spec.x.length;
    let low = 0;
    let high = 0;
    spec.x.forEach((_, index) => {
      const values = spec.series.map((series) => series.values[index]).filter((value) => value !== null && value !== undefined);
      if (stacked) high = Math.max(high, values.reduce((sum, value) => sum + value, 0));
      else values.forEach((value) => { high = Math.max(high, value); low = Math.min(low, value); });
    });
    const y = scale(spec, low, high);
    const format = FORMAT[spec.format];
    const ticks = [];
    for (let value = y.bottom; value <= y.top + y.step / 1000; value += y.step) ticks.push(value);
    const left = Math.max(...ticks.map((value) => format(value).length)) * 6.6 + 12;
    const marked = spec.notes ? notes.filter((note) => spec.x.includes(note.month)) : [];
    const top = marked.length ? 20 : 10;
    const right = spec.ends ? 78 : 12;
    const bottom = 26;
    const inner = { width: width - left - right, height: height - top - bottom };
    const band = inner.width / Math.max(1, n);
    const cx = (index) => left + band * (index + 0.5);
    const cy = (value) => top + inner.height * (1 - (value - y.bottom) / (y.top - y.bottom));

    const chart = svg('svg', { width, height, viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true', focusable: 'false', class: 'insight-chart__svg' });
    ticks.forEach((value) => {
      chart.append(svg('line', { x1: left, x2: width - right, y1: cy(value), y2: cy(value), class: value === 0 ? 'insight-axis' : 'insight-grid' }));
      const label = svg('text', { x: left - 8, y: cy(value) + 4, 'text-anchor': 'end', class: 'insight-tick' });
      label.textContent = format(value);
      chart.append(label);
    });

    // Month labels as far apart as they need to be, on round months (Jan, Apr, Jul, Oct...).
    const room = monthly(spec) ? 50 : 26;
    const every = [1, 2, 3, 6, 12, 24].find((step) => step * band >= room) || 24;
    spec.x.forEach((key, index) => {
      let text = key;
      if (monthly(spec)) {
        const [year, month] = parts(key);
        if ((month - 1) % every !== 0 && !(every > 12 && index === 0)) return;
        text = month === 1 || index < every ? `${SHORT[month - 1]} ${year}` : SHORT[month - 1];
      } else if (index % every !== 0) return;
      const label = svg('text', { x: cx(index), y: height - 8, 'text-anchor': 'middle', class: 'insight-tick' });
      label.textContent = text;
      chart.append(label);
    });

    const hover = svg('rect', { x: left, y: top, width: inner.width, height: inner.height, class: 'insight-band', visibility: 'hidden' });
    chart.append(hover);
    const cross = svg('line', { x1: 0, x2: 0, y1: top, y2: top + inner.height, class: 'insight-cross', visibility: 'hidden' });
    if (spec.kind === 'line') chart.append(cross);

    // Notes: a dashed line at their month.
    const noteMonths = [...new Set(marked.map((note) => note.month))];
    noteMonths.forEach((month) => {
      const x = cx(spec.x.indexOf(month));
      chart.append(svg('line', { x1: x, x2: x, y1: top - 6, y2: top + inner.height, class: 'insight-note-line' }));
    });

    if (spec.kind === 'line') {
      // The All line first, under the coloured ones.
      const order = [...spec.series].sort((a, b) => (a.tone === 'total' ? -1 : 0) - (b.tone === 'total' ? -1 : 0));
      order.forEach((series) => {
        const group = svg('g', { class: `insight-tone insight-tone--${series.tone}` });
        let path = '';
        let drawing = false;
        series.values.forEach((value, index) => {
          if (value === null || value === undefined) { drawing = false; return; }
          path += `${drawing ? 'L' : 'M'}${cx(index).toFixed(1)} ${cy(value).toFixed(1)} `;
          drawing = true;
        });
        if (path) group.append(svg('path', { d: path, class: 'insight-line' }));
        const points = series.values.map((value, index) => [value, index]).filter(([value]) => value !== null && value !== undefined);
        const dots = n <= 16 ? points : points.slice(-1);
        dots.forEach(([value, index]) => group.append(svg('circle', { cx: cx(index), cy: cy(value), r: 4, class: 'insight-dot' })));
        if (spec.ends && points.length) {
          const [value, index] = points[points.length - 1];
          const label = svg('text', { x: cx(index) + 8, y: cy(value) + 4, class: 'insight-end' });
          label.textContent = `${series.label}: ${format(value)}`;
          group.append(label);
        }
        chart.append(group);
      });
    } else {
      const thick = Math.max(3, Math.min(24, band * 0.66));
      spec.x.forEach((_, index) => {
        let base = 0;
        const segments = spec.series.map((series) => [series, series.values[index] || 0]).filter(([, value]) => value > 0);
        segments.forEach(([series, value], position) => {
          const lower = cy(base);
          const upper = cy(base + value);
          base += value;
          const last = position === segments.length - 1;
          // A 2px gap of the surface between stacked segments.
          const height2 = Math.max(0.5, lower - upper - (position ? 2 : 0));
          const x = cx(index) - thick / 2;
          const yTop = lower - (position ? 2 : 0) - height2;
          const radius = last ? Math.min(4, thick / 2, height2) : 0;
          const d = `M${x} ${yTop + height2}V${yTop + radius}a${radius} ${radius} 0 0 1 ${radius} -${radius}H${x + thick - radius}a${radius} ${radius} 0 0 1 ${radius} ${radius}V${yTop + height2}Z`;
          chart.append(svg('path', { d, class: `insight-bar insight-tone insight-tone--${series.tone}` }));
        });
      });
    }

    plot.replaceChildren(chart);
    plot.style.height = `${height}px`;

    // Note buttons over the dashed lines.
    noteMonths.forEach((month) => {
      const index = spec.x.indexOf(month);
      const here = marked.filter((note) => note.month === month);
      const button = make('button', 'insight-note-marker');
      button.type = 'button';
      button.style.left = `${cx(index)}px`;
      button.style.top = `${top - 14}px`;
      button.setAttribute('aria-label', `Note: ${here.map((note) => `${noteDate(note.on)}, ${note.label}`).join('; ')}`);
      button.setAttribute('aria-expanded', 'false');
      plot.append(button);
      listen(button, 'click', (event) => {
        event.stopPropagation();
        const open = plot.querySelector('.insight-note');
        const mine = open?.dataset.month === month;
        closeNotes();
        if (mine) return;
        const card = make('div', 'insight-note');
        card.dataset.month = month;
        card.setAttribute('role', 'note');
        here.forEach((note) => {
          const line = make('p');
          const when = make('strong');
          when.textContent = noteDate(note.on);
          line.append(when, document.createTextNode(` ${note.label}`));
          card.append(line);
        });
        plot.append(card);
        const x = cx(index);
        card.style.left = `${Math.min(Math.max(8, x - card.offsetWidth / 2), width - card.offsetWidth - 8)}px`;
        card.style.top = `${top + 4}px`;
        button.setAttribute('aria-expanded', 'true');
      });
    });

    // The read-out: every series at the month pointed at.
    const tip = make('div', 'insight-tip');
    tip.hidden = true;
    plot.append(tip);
    let at = null;
    function show(index) {
      at = Math.max(0, Math.min(n - 1, index));
      const x = cx(at);
      if (spec.kind === 'line') {
        cross.setAttribute('x1', x);
        cross.setAttribute('x2', x);
        cross.setAttribute('visibility', 'visible');
      } else {
        hover.setAttribute('x', x - band / 2);
        hover.setAttribute('width', band);
        hover.setAttribute('visibility', 'visible');
      }
      const title = make('p', 'insight-tip__title');
      title.textContent = longLabel(spec, spec.x[at]);
      const rows = spec.series.map((series) => {
        const value = series.values[at];
        const row = make('p', 'insight-tip__row');
        const key = make('i', `insight-tone insight-tone--${series.tone}`);
        key.setAttribute('aria-hidden', 'true');
        const amount = make('strong');
        amount.textContent = value === null || value === undefined ? '—' : format(value);
        const name = make('span');
        name.textContent = series.counts && value !== null ? `${series.label} (${series.counts[at]} patients)` : series.label;
        row.append(key, amount, name);
        return row;
      });
      if (stacked) {
        const total = make('p', 'insight-tip__row insight-tip__total');
        const amount = make('strong');
        amount.textContent = format(spec.series.reduce((sum, series) => sum + (series.values[at] || 0), 0));
        const name = make('span');
        name.textContent = 'In all';
        total.append(make('i'), amount, name);
        rows.push(total);
      }
      tip.replaceChildren(title, ...rows);
      tip.hidden = false;
      const offset = x + 14 + tip.offsetWidth > width ? x - 14 - tip.offsetWidth : x + 14;
      tip.style.left = `${Math.max(4, offset)}px`;
      tip.style.top = `${top}px`;
    }
    function hide() {
      at = null;
      tip.hidden = true;
      cross.setAttribute('visibility', 'hidden');
      hover.setAttribute('visibility', 'hidden');
    }
    const pick = (event) => {
      const box = chart.getBoundingClientRect();
      return Math.floor((event.clientX - box.left - left) / band);
    };
    listen(chart, 'pointermove', (event) => {
      const box = chart.getBoundingClientRect();
      const x = event.clientX - box.left;
      if (x < left - 4 || x > width - right + 4) { hide(); return; }
      show(pick(event));
    });
    listen(chart, 'pointerleave', hide);
    plot.tabIndex = 0;
    plot.setAttribute('role', 'img');
    plot.setAttribute('aria-label', `${host.dataset.title || 'Chart'}. Use the left and right arrow keys to read each ${monthly(spec) ? 'month' : 'month of subscription'}; the numbers are also in the table below.`);
    plot.onkeydown = (event) => {
      if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
        event.preventDefault();
        show(at === null ? n - 1 : at + (event.key === 'ArrowRight' ? 1 : -1));
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault();
        show(event.key === 'Home' ? 0 : n - 1);
      } else if (event.key === 'Escape') hide();
    };
    plot.onblur = hide;
  }

  function closeNotes() {
    document.querySelectorAll('.insight-note').forEach((card) => card.remove());
    document.querySelectorAll('.insight-note-marker[aria-expanded="true"]').forEach((button) => button.setAttribute('aria-expanded', 'false'));
  }
  listen(document, 'click', (event) => { if (!event.target.closest?.('.insight-note')) closeNotes(); });
  listen(document, 'keydown', (event) => { if (event.key === 'Escape') closeNotes(); });

  const watching = [];
  document.querySelectorAll('[data-insight-chart]').forEach((host) => {
    const spec = specs[host.dataset.insightChart];
    if (!spec) return;
    const empty = !spec.series.length || spec.series.every((series) => series.values.every((value) => !value));
    if (empty) {
      const note = make('p', 'insights-empty');
      note.textContent = 'Nothing to show for this period yet.';
      host.querySelector('.insight-chart__plot').replaceChildren(note);
      host.querySelector('.insight-chart__table')?.remove();
      return;
    }
    legend(spec, host);
    table(spec, host);
    let width = 0;
    const redraw = () => {
      const now = host.querySelector('.insight-chart__plot').clientWidth;
      if (now && now !== width) {
        width = now;
        draw(spec, host);
      }
    };
    redraw();
    if ('ResizeObserver' in window) {
      const observer = new ResizeObserver(() => window.requestAnimationFrame(redraw));
      observer.observe(host.querySelector('.insight-chart__plot'));
      watching.push(observer);
    } else listen(window, 'resize', redraw);
  });
  lifecycle?.cleanup(() => watching.forEach((observer) => observer.disconnect()));
})();

});
