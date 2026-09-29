/* Small, local SVG charts: no external analytics, fonts, or chart CDN. */
(() => {
  'use strict';
  const colors = { teal: '#159286', blue: '#6289ce', violet: '#8272b0', amber: '#c89543', rose: '#bd6f7d', slate: '#9aadb5' };
  const readData = id => {
    const element = document.getElementById(id);
    if (!element) return null;
    try { return JSON.parse(element.textContent); } catch (_) { return null; }
  };
  const svgNode = (name, attrs = {}, label = null) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (label !== null) node.textContent = label;
    return node;
  };
  const activity = readData('meridian-activity-data');
  const chart = document.getElementById('meridian-activity-chart');
  const visible = new Set(['patients', 'appointments']);
  const seriesLabels = { patients: 'New patients', appointments: 'Appointments' };

  function renderActivity() {
    if (!chart || !activity || !Array.isArray(activity.labels)) return;
    const width = Math.max(290, chart.clientWidth);
    const height = window.innerWidth < 768 ? 200 : 220;
    const padding = { left: 30, top: 20, right: 15, bottom: 28 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const series = ['patients', 'appointments'].filter(key => Array.isArray(activity[key]) && visible.has(key));
    const values = series.flatMap(key => activity[key]);
    const maximum = Math.max(4, ...values);
    const step = Math.max(1, Math.ceil(maximum / 4));
    const ceiling = step * 4;
    const x = index => padding.left + index * plotWidth / Math.max(1, activity.labels.length - 1);
    const y = value => padding.top + plotHeight - value / ceiling * plotHeight;
    const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-labelledby': 'meridian-chart-title meridian-chart-description' });
    svg.append(svgNode('title', { id: 'meridian-chart-title' }, 'Weekly practice activity'));
    svg.append(svgNode('desc', { id: 'meridian-chart-description' }, 'New patient records and appointments by week. Exact values are available in the View chart data table below.'));
    const defs = svgNode('defs');
    const gradient = svgNode('linearGradient', { id: 'meridian-chart-fill', x1: 0, y1: 0, x2: 0, y2: 1 });
    gradient.append(svgNode('stop', { offset: '0%', 'stop-color': colors.teal, 'stop-opacity': '.16' }), svgNode('stop', { offset: '100%', 'stop-color': colors.teal, 'stop-opacity': '.01' }));
    defs.append(gradient);
    svg.append(defs);
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = tick * step;
      svg.append(svgNode('line', { x1: padding.left, x2: width - padding.right, y1: y(value), y2: y(value), class: 'md-chart-gridline' }));
      svg.append(svgNode('text', { x: padding.left - 10, y: y(value) + 3, 'text-anchor': 'end', class: 'md-chart-label' }, value));
    }
    const labelEvery = Math.max(1, Math.ceil(activity.labels.length / (width < 400 ? 4 : 7)));
    activity.labels.forEach((label, index) => {
      if (index % labelEvery !== 0 && index !== activity.labels.length - 1) return;
      // Prevent the final two dates from overlapping at narrow widths.
      if (index !== activity.labels.length - 1 && activity.labels.length - 1 - index < labelEvery) return;
      svg.append(svgNode('text', { x: x(index), y: height - 5, 'text-anchor': index === 0 ? 'start' : index === activity.labels.length - 1 ? 'end' : 'middle', class: 'md-chart-label' }, label));
    });
    const tooltip = document.createElement('p');
    tooltip.className = 'md-chart-tooltip';
    tooltip.textContent = activity.has_data ? 'Hover or focus a point to see its weekly total.' : 'Weekly totals will appear as records are added.';
    tooltip.setAttribute('aria-live', 'polite');
    series.forEach(key => {
      const color = key === 'patients' ? colors.teal : colors.blue;
      const points = activity[key].map((value, index) => `${x(index)},${y(value)}`).join(' ');
      if (key === 'patients' && activity[key].some(value => value > 0)) {
        svg.append(svgNode('polygon', { points: `${x(0)},${y(0)} ${points} ${x(activity.labels.length - 1)},${y(0)}`, fill: 'url(#meridian-chart-fill)' }));
      }
      svg.append(svgNode('polyline', { points, fill: 'none', stroke: color, 'stroke-width': 2.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', ...(key === 'appointments' ? { 'stroke-dasharray': '5 4' } : {}) }));
      activity[key].forEach((value, index) => {
        const label = `${activity.labels[index]}: ${seriesLabels[key]}, ${value}`;
        const point = svgNode('circle', { cx: x(index), cy: y(value), r: 4, fill: '#fff', stroke: color, 'stroke-width': 2, tabindex: '0', class: 'md-chart-point', 'aria-label': label });
        point.append(svgNode('title', {}, label));
        point.addEventListener('mouseenter', () => { tooltip.textContent = label; });
        point.addEventListener('focus', () => { tooltip.textContent = label; });
        svg.append(point);
      });
    });
    chart.replaceChildren(svg, tooltip);
  }
  document.querySelectorAll('[data-chart-series]').forEach(button => {
    button.addEventListener('click', () => {
      const key = button.dataset.chartSeries;
      if (visible.has(key)) visible.delete(key); else visible.add(key);
      button.setAttribute('aria-pressed', String(visible.has(key)));
      renderActivity();
    });
  });
  renderActivity();
  if (chart && typeof ResizeObserver !== 'undefined') {
    let previousWidth = chart.clientWidth;
    new ResizeObserver(() => {
      if (chart.clientWidth !== previousWidth) {
        previousWidth = chart.clientWidth;
        renderActivity();
      }
    }).observe(chart);
  }
  const statuses = readData('meridian-appointment-data');
  const donut = document.getElementById('meridian-appointment-chart');
  if (donut && Array.isArray(statuses)) {
    const total = statuses.reduce((sum, status) => sum + status.count, 0);
    let start = 0;
    if (total > 0) {
      const segments = statuses.filter(status => status.count > 0).map(status => {
        const end = start + status.count / total * 100;
        const segment = `${colors[status.tone] || colors.slate} ${start}% ${end}%`;
        start = end;
        return segment;
      });
      donut.style.background = `conic-gradient(${segments.join(',')})`;
    }
  }
  const directorySearch = document.getElementById('meridian-directory-search');
  if (directorySearch) {
    const groups = [...document.querySelectorAll('.md-directory-group')];
    let expandedBeforeSearch = null;
    directorySearch.addEventListener('input', () => {
      const query = directorySearch.value.trim().toLocaleLowerCase();
      if (query && expandedBeforeSearch === null) expandedBeforeSearch = groups.map(group => group.open);
      let matches = 0;
      groups.forEach((group, index) => {
        let groupMatches = 0;
        group.querySelectorAll('[data-section-name]').forEach(item => {
          item.hidden = !item.dataset.sectionName.includes(query);
          if (!item.hidden) groupMatches += 1;
        });
        group.hidden = groupMatches === 0;
        if (query) group.open = groupMatches > 0;
        else if (expandedBeforeSearch !== null) group.open = expandedBeforeSearch[index];
        matches += groupMatches;
      });
      if (!query) expandedBeforeSearch = null;
      document.getElementById('meridian-directory-empty').hidden = matches > 0;
    });
  }
  const sidebarToggle = document.querySelector('[data-lte-toggle="sidebar"]');
  if (sidebarToggle) sidebarToggle.setAttribute('aria-label', 'Toggle administration navigation');
})();
