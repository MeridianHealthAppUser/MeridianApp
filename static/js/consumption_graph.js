(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('consumption_graph', (lifecycle) => {
  const host = document.querySelector('[data-consumption-graph]');
  if (!host) return;
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  const fromDate = host.querySelector('input[name="date_from"]');
  const toDate = host.querySelector('input[name="date_to"]');
  const validateDates = () => {
    toDate.setCustomValidity(fromDate.value && toDate.value && fromDate.value > toDate.value
      ? 'Choose a to date on or after the from date.' : '');
  };
  for (const input of [fromDate, toDate]) {
    listen(input, 'input', validateDates);
    listen(input, 'change', validateDates);
  }
  validateDates();
  const picker = host.querySelector('[data-product-picker]');
  const choices = [...picker.querySelectorAll('input[name="products"]')];
  const summary = picker.querySelector('[data-product-summary]');
  const count = picker.querySelector('[data-product-count]');
  const search = picker.querySelector('[data-product-search]');
  const updateSelection = () => {
    const selected = choices.filter(input => input.checked);
    summary.textContent = selected.length === 0 ? 'Choose products'
      : selected.length === choices.length ? `All products (${selected.length})`
      : selected.length === 1 ? selected[0].nextElementSibling.textContent : `${selected.length} products selected`;
    count.textContent = `${selected.length} selected`;
  };
  updateSelection();
  listen(picker, 'change', updateSelection);
  listen(picker.querySelector('[data-products-all]'), 'click', () => {
    choices.forEach(input => { input.checked = true; });
    updateSelection();
  });
  listen(picker.querySelector('[data-products-clear]'), 'click', () => {
    choices.forEach(input => { input.checked = false; });
    updateSelection();
  });
  listen(search, 'input', () => {
    const query = search.value.trim().toLocaleLowerCase();
    const options = [...picker.querySelectorAll('[data-product-option]')];
    options.forEach(option => { option.hidden = !option.textContent.toLocaleLowerCase().includes(query); });
    picker.querySelector('[data-product-no-match]').hidden = !options.length || options.some(option => !option.hidden);
  });
  listen(document, 'click', event => { if (!picker.contains(event.target)) picker.open = false; });
  listen(picker, 'keydown', event => {
    if (event.key === 'Escape' && picker.open) {
      event.preventDefault();
      picker.open = false;
      picker.querySelector('summary').focus();
    }
  });

  const source = document.getElementById('consumption-graph-data');
  const plot = host.querySelector('[data-graph-plot]');
  if (!source || !plot) return;
  let data;
  try { data = JSON.parse(source.textContent); } catch (_) { return; }
  if (!data.series?.length || !data.periods?.length) return;

  const NS = 'http://www.w3.org/2000/svg';
  const palette = ['#287f9a', '#c27526', '#8661bb', '#25896d', '#c25879', '#5876c3', '#9b8323', '#659347'];
  const colour = index => palette[index % palette.length];
  const dash = index => ['', '7 4', '2 4'][Math.floor(index / palette.length) % 3];
  const number = value => new Intl.NumberFormat('en-ZA').format(value);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const svg = (tag, attrs, text) => {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const axisDate = period => new Intl.DateTimeFormat('en-GB', {
    timeZone: 'UTC', month: 'short', ...(data.grouping === 'months' ? {year: 'numeric'} : {day: 'numeric'}),
  }).format(new Date(`${period.start}T00:00:00Z`));
  const legend = host.querySelector('[data-graph-legend]');
  const hiddenSeries = new Set();
  data.series.forEach((series, index) => {
    const item = make('button', 'consumption-graph-key');
    item.type = 'button';
    item.setAttribute('aria-pressed', 'true');
    item.setAttribute('aria-controls', 'consumption-usage-plot');
    item.title = `Hide ${series.name}`;
    const sample = svg('svg', {width: 24, height: 12, viewBox: '0 0 24 12', 'aria-hidden': 'true'});
    sample.append(svg('line', {x1: 1, x2: 23, y1: 6, y2: 6, stroke: colour(index), 'stroke-width': 3, 'stroke-dasharray': dash(index)}));
    item.append(sample, make('span', '', series.name));
    legend.append(item);
    listen(item, 'click', () => {
      if (hiddenSeries.has(series.id)) hiddenSeries.delete(series.id);
      else hiddenSeries.add(series.id);
      const visible = !hiddenSeries.has(series.id);
      item.setAttribute('aria-pressed', String(visible));
      item.title = `${visible ? 'Hide' : 'Show'} ${series.name}`;
      draw();
    });
  });

  const tooltip = make('div', 'consumption-graph-tooltip');
  tooltip.hidden = true;
  tooltip.setAttribute('role', 'status');
  tooltip.setAttribute('aria-live', 'polite');
  plot.append(tooltip);
  const empty = make('div', 'consumption-graph-hidden-message', 'All products are hidden. Click a product name above to show its line.');
  empty.hidden = true;
  empty.setAttribute('role', 'status');
  plot.append(empty);
  let active = -1;
  let chart, guide, dots, geometry;
  const hide = () => {
    active = -1;
    tooltip.hidden = true;
    guide?.setAttribute('visibility', 'hidden');
    dots?.setAttribute('visibility', 'hidden');
  };
  function show(index) {
    if (hiddenSeries.size === data.series.length) { hide(); return; }
    index = Math.max(0, Math.min(data.periods.length - 1, index));
    if (active === index) return;
    active = index;
    const {x, y, width, left, right} = geometry;
    guide.setAttribute('x1', x(index));
    guide.setAttribute('x2', x(index));
    guide.setAttribute('visibility', 'visible');
    dots.replaceChildren();
    dots.setAttribute('visibility', 'visible');
    tooltip.replaceChildren(make('strong', '', data.periods[index].label));
    data.series.forEach((series, seriesIndex) => {
      if (hiddenSeries.has(series.id)) return;
      const value = series.values[index];
      dots.append(svg('circle', {cx: x(index), cy: y(value), r: 5, fill: colour(seriesIndex), class: 'consumption-graph-dot'}));
      const row = make('div', 'consumption-graph-tooltip-row');
      const swatch = make('i');
      swatch.style.backgroundColor = colour(seriesIndex);
      row.append(swatch, make('span', '', series.name), make('b', '', number(value)));
      tooltip.append(row);
    });
    tooltip.hidden = false;
    const tooltipWidth = tooltip.offsetWidth;
    const toRight = x(index) + 16;
    const target = toRight + tooltipWidth <= width - right ? toRight : x(index) - tooltipWidth - 16;
    tooltip.style.left = `${Math.max(left, Math.min(width - tooltipWidth - 4, target))}px`;
  }
  function draw() {
    const previous = active;
    const width = Math.max(640, Math.round(plot.parentElement.clientWidth));
    const height = 340;
    const left = 58, right = 24, top = 24, bottom = 60;
    let maximum = 0;
    data.series.forEach(series => {
      if (!hiddenSeries.has(series.id)) series.values.forEach(value => { maximum = Math.max(maximum, value); });
    });
    const rough = Math.max(1, maximum) / 4;
    const power = 10 ** Math.floor(Math.log10(rough));
    const step = Math.max(1, [1, 2, 5, 10].map(factor => factor * power).find(value => value >= rough));
    const ceiling = Math.max(step, Math.ceil(maximum / step) * step);
    const innerWidth = width - left - right;
    const x = index => data.periods.length === 1 ? left + innerWidth / 2 : left + index * innerWidth / (data.periods.length - 1);
    const y = value => top + (height - top - bottom) * (1 - value / ceiling);
    geometry = {x, y, width, left, right};
    chart = svg('svg', {width, height, viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true', focusable: 'false'});
    chart.append(svg('text', {x: left, y: 12, class: 'consumption-graph-axis-title'}, 'Units'));
    for (let value = 0; value <= ceiling; value += step) {
      chart.append(svg('line', {x1: left, x2: width - right, y1: y(value), y2: y(value), class: value ? 'consumption-graph-grid' : 'consumption-graph-axis'}));
      chart.append(svg('text', {x: left - 12, y: y(value) + 4, 'text-anchor': 'end', class: 'consumption-graph-tick'}, number(value)));
    }
    const every = Math.max(1, Math.ceil(data.periods.length / Math.floor(innerWidth / 90)));
    const labels = [];
    for (let index = 0; index < data.periods.length; index += every) labels.push(index);
    const last = data.periods.length - 1;
    if (last - labels[labels.length - 1] >= every * .6) labels.push(last);
    labels.forEach(index => chart.append(svg('text', {
      x: x(index), y: height - bottom + 24,
      'text-anchor': index === 0 && data.periods.length > 1 ? 'start' : index === last && data.periods.length > 1 ? 'end' : 'middle',
      class: 'consumption-graph-tick',
    }, axisDate(data.periods[index]))));
    chart.append(svg('text', {x: left + innerWidth / 2, y: height - 6, 'text-anchor': 'middle', class: 'consumption-graph-axis-title'}, data.grouping === 'months' ? 'Month' : 'Week starting'));
    data.series.forEach((series, index) => {
      if (hiddenSeries.has(series.id)) return;
      chart.append(svg('polyline', {
        points: series.values.map((value, point) => `${x(point)},${y(value)}`).join(' '),
        fill: 'none', stroke: colour(index), 'stroke-width': 2.5, 'stroke-linejoin': 'round',
        'stroke-linecap': 'round', 'stroke-dasharray': dash(index), class: 'consumption-graph-line',
      }));
      if (data.periods.length <= 20) series.values.forEach((value, point) => {
        chart.append(svg('circle', {cx: x(point), cy: y(value), r: 3.5, fill: colour(index), class: 'consumption-graph-dot'}));
      });
    });
    guide = svg('line', {x1: left, x2: left, y1: top, y2: height - bottom, class: 'consumption-graph-guide', visibility: 'hidden'});
    dots = svg('g', {visibility: 'hidden'});
    chart.append(guide, dots);
    plot.querySelector('svg')?.remove();
    plot.querySelector('.consumption-graph-loading')?.remove();
    plot.prepend(chart);
    plot.style.width = `${width}px`;
    const visibleCount = data.series.length - hiddenSeries.size;
    empty.hidden = visibleCount > 0;
    plot.setAttribute('aria-label', `Product usage line graph, ${visibleCount} of ${data.series.length} products shown`);
    hide();
    if (previous >= 0 && visibleCount) show(previous);
  }
  listen(plot, 'pointermove', event => {
    if (tooltip.contains(event.target)) return;
    const bounds = plot.getBoundingClientRect();
    const fraction = (event.clientX - bounds.left - geometry.left) / (geometry.width - geometry.left - geometry.right);
    show(Math.round(fraction * (data.periods.length - 1)));
  });
  listen(plot, 'pointerleave', () => { if (document.activeElement !== plot) hide(); });
  listen(plot, 'focus', () => show(active >= 0 ? active : 0));
  listen(plot, 'blur', hide);
  listen(plot, 'keydown', event => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) event.preventDefault();
    if (event.key === 'ArrowLeft') show(active < 0 ? 0 : active - 1);
    if (event.key === 'ArrowRight') show(active < 0 ? 0 : active + 1);
    if (event.key === 'Home') show(0);
    if (event.key === 'End') show(data.periods.length - 1);
    if (event.key === 'Escape') hide();
  });
  draw();
  if (typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver(() => {
      const width = Math.max(640, Math.round(plot.parentElement.clientWidth));
      if (width !== geometry.width) draw();
    });
    observer.observe(plot.parentElement);
    lifecycle?.cleanup(() => observer.disconnect());
  } else listen(window, 'resize', draw);
});
