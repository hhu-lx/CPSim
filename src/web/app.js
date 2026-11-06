(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    configured: false, timer: null, history: [], longHistory: [], latestStats: {},
    activeMode: 'heuristic',
    modeSelections: {
      heuristic: { objective: 'min_cost' },
      local: { dispatch: 'lowest_price', objective: 'min_cost' },
      global: { objective: 'min_cost' },
    },
  };
  const message = (text, kind = '') => { const el = $('configMessage'); el.textContent = text; el.className = `message ${kind}`; };
  const selectedFile = (id) => $(id).files[0] || null;
  const formatTime = (seconds) => { const s = Math.max(0, Number(seconds || 0)); return [Math.floor(s / 3600), Math.floor(s / 60) % 60, Math.floor(s) % 60].map(v => String(v).padStart(2, '0')).join(':'); };
  const number = (value) => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '0.00';
  const measurement = (value, unit) => value !== null && value !== undefined && Number.isFinite(Number(value)) ? `${number(value)} ${unit}` : `— ${unit}`;

  function renderRuntimeCards(stats) {
    const runtime = stats.datacenter_runtime || {};
    const entries = Object.entries(runtime);
    const resourceList = $('resourceList');
    resourceList.replaceChildren();
    if (!entries.length) {
      resourceList.innerHTML = '<p>Waiting for simulation data</p>';
    } else {
      entries.forEach(([id, data], index) => {
        const row = document.createElement('div'); row.className = 'resource-row';
        const name = document.createElement('strong'); name.textContent = `Datacenter ${String.fromCharCode(65 + index)}`;
        const value = document.createElement('span'); value.textContent = `${number(data.compute_utilization * 100)}%`;
        const bar = document.createElement('div'); bar.className = 'resource-bar';
        const fill = document.createElement('i'); fill.style.width = `${Math.min(100, Math.max(0, Number(data.compute_utilization || 0) * 100))}%`; bar.append(fill);
        const meta = document.createElement('small'); meta.textContent = `${number(data.used_compute)} / ${number(data.total_compute)} TFLOPS · ${data.running_tasks || 0} tasks · ${number(data.power_mw)} MW`;
        row.append(name, value, bar, meta); resourceList.append(row);
      });
    }
    const summary = stats.task_summary || {};
    const total = Number(summary.submitted || 0);
    $('queueTotal').textContent = String(total || 0);
    $('queueRunning').textContent = String(summary.running || 0);
    $('queueScheduling').textContent = String((summary.queued || 0) + (summary.scheduling || 0));
    $('queueCompleted').textContent = String(summary.completed || 0);
    $('queueCancelled').textContent = String(summary.cancelled || 0);
    const rejectedNode = $('queueRejected');
    if (rejectedNode) rejectedNode.textContent = String((summary.rejected || 0) + (summary.unclassified || 0));
    const alerts = [...GridStatusView.buildGridStatusView(stats).alerts];
    if (stats.renewable_dispatch_status === 'grid_infeasible') alerts.push('No feasible operating point under current grid constraints; selected the renewable output with minimum constraint violation');
    if (Number(stats.renewable_curtailed_mw || 0) > 0.001) alerts.push(`Renewable curtailment ${number(stats.renewable_curtailed_mw)} MW, current utilization ${number(Number(stats.renewable_consumption_rate || 0) * 100)}%`);
    if (stats.grid_health?.level === 'critical') alerts.push(`Grid Health Index ${number(stats.grid_health.score)}，currently at critical risk`);
    if (entries.some(([, value]) => Number(value.compute_utilization || 0) > 0.9)) alerts.push('Datacenter compute utilization exceeds 90%');
    const alertList = $('alertList'); alertList.replaceChildren();
    if (!alerts.length) { alertList.innerHTML = '<p class="no-alert">No alerts</p>'; }
    else alerts.forEach(text => { const item = document.createElement('p'); item.className = 'alert-item'; item.textContent = `! ${text}`; alertList.append(item); });
  }

  function renderHealth(stats) {
    const view = GridStatusView.buildGridStatusView(stats);
    const cpf = stats.cpf_result || {};
    const converged = cpf.converged;
    const marginAvailable = cpf.voltage_margin_pct !== null && cpf.voltage_margin_pct !== undefined && Number.isFinite(Number(cpf.voltage_margin_pct));
    $('healthConverged').textContent = view.statusLines.flow;
    $('healthVoltage').textContent = view.statusLines.voltage;
    $('healthLine').textContent = view.statusLines.line;
    $('healthReserve').textContent = view.statusLines.reserve;
    const compositeScore = stats.grid_health?.score;
    $('healthScore').textContent = compositeScore !== null && compositeScore !== undefined && Number.isFinite(Number(compositeScore))
      ? number(compositeScore)
      : converged === false ? '0' : converged === true && marginAvailable ? number(Math.max(0, Math.min(100, cpf.voltage_margin_pct))) : '—';
  }

  function renderOverviewConfiguration(configuration) {
    const centers = configuration?.effective_data_centers || [];
    centers.slice(0, 3).forEach((center, index) => {
      const label = ['A', 'B', 'C'][index];
      const node = document.querySelector(`#dcFlow${label} strong`);
      if (node) node.textContent = `Datacenter ${label}`;
    });
  }

  function lineChart(target, series) {
    const width = 520, height = 145, pad = { l: 34, r: 12, t: 9, b: 24 };
    const values = series.flatMap(s => s.values); const max = Math.max(1, ...values); const count = Math.max(2, ...series.map(s => s.values.length));
    const points = (values) => values.map((v, i) => `${pad.l + i * (width - pad.l - pad.r) / (count - 1)},${height - pad.b - Number(v || 0) / max * (height - pad.t - pad.b)}`).join(' ');
    const grid = [0, .5, 1].map(r => `<line x1="${pad.l}" y1="${height - pad.b - r * (height - pad.t - pad.b)}" x2="${width - pad.r}" y2="${height - pad.b - r * (height - pad.t - pad.b)}" stroke="#e5edf5"/>`).join('');
    const paths = series.map(s => `<polyline fill="none" stroke="${s.color}" stroke-width="2.5" points="${points(s.values)}"/>`).join('');
    const legend = series.map((s, i) => `<text x="${pad.l + i * 105}" y="${height - 5}" fill="${s.color}" font-size="10">● ${s.label}</text>`).join('');
    target.insertAdjacentHTML('beforeend', `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${target.querySelector('h3').textContent}">${grid}<line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${height-pad.b}" stroke="#9eb0c2"/><line x1="${pad.l}" y1="${height-pad.b}" x2="${width-pad.r}" y2="${height-pad.b}" stroke="#9eb0c2"/>${paths}${legend}</svg>`);
  }

  function stackedBarChart(target, records) {
    const width = 520, height = 145, pad = { l: 34, r: 12, t: 9, b: 28 };
    const names = [...new Set(records.flatMap(record => Object.keys(record.running_compute_by_datacenter || {})))];
    const colors = ['#1478e8', '#159d93', '#e3a33d', '#8064a2', '#df6b5b', '#5e9acb'];
    const max = Math.max(1, ...records.map(record => names.reduce((sum, id) => sum + Number(record.running_compute_by_datacenter?.[id]?.compute_tflops || 0), 0)));
    const chartWidth = width - pad.l - pad.r, chartHeight = height - pad.t - pad.b;
    const barWidth = Math.max(2, Math.min(18, chartWidth / Math.max(records.length * 1.5, 1)));
    const bars = records.map((record, index) => {
      const x = pad.l + (index + 0.5) * chartWidth / Math.max(records.length, 1) - barWidth / 2;
      let y = height - pad.b;
      const segments = names.map((id, i) => {
        const value = Number(record.running_compute_by_datacenter?.[id]?.compute_tflops || 0);
        const h = value / max * chartHeight;
        y -= h;
        return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${h.toFixed(2)}" fill="${colors[i % colors.length]}"/>`;
      }).join('');
      return segments;
    }).join('');
    const grid = [0, .5, 1].map(r => `<line x1="${pad.l}" y1="${height - pad.b - r * chartHeight}" x2="${width - pad.r}" y2="${height - pad.b - r * chartHeight}" stroke="#e5edf5"/>`).join('');
    const legend = names.map((id, i) => {
      const name = records.find(record => record.running_compute_by_datacenter?.[id])?.running_compute_by_datacenter?.[id]?.name || id;
      return `<text x="${pad.l + i * 105}" y="${height - 5}" fill="${colors[i % colors.length]}" font-size="10">● ${name}</text>`;
    }).join('');
    target.insertAdjacentHTML('beforeend', `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${target.querySelector('h3').textContent}">${grid}<line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${height-pad.b}" stroke="#9eb0c2"/><line x1="${pad.l}" y1="${height-pad.b}" x2="${width-pad.r}" y2="${height-pad.b}" stroke="#9eb0c2"/>${bars}${legend}</svg>`);
  }

  function combinedComputeChart(target, records) {
    const overview = TrendCharts.buildComputeOverview(records);
    const width = 520;
    const legendItems = overview.centers.length + 1;
    const legendRows = Math.max(1, Math.ceil(legendItems / 4));
    const height = 145 + Math.max(0, legendRows - 1) * 12;
    const pad = { l: 38, r: 38, t: 9, b: 20 + legendRows * 12 };
    const chartWidth = width - pad.l - pad.r;
    const chartHeight = height - pad.t - pad.b;
    const count = Math.max(1, records.length);
    const colors = overview.centers.map((_, index) => `hsl(${(index * 67 + 205) % 360} 62% 45%)`);
    const computeTotals = records.map((_, index) => overview.centers.reduce(
      (sum, center) => sum + Number(overview.computeByCenter[center.id]?.[index] || 0), 0,
    ));
    const computeMax = Math.max(1, ...computeTotals);
    const powerMax = Math.max(1, ...overview.powerMw);
    const barWidth = Math.max(1.5, Math.min(16, chartWidth / Math.max(count * 1.35, 1)));
    const bars = records.map((_, recordIndex) => {
      const x = pad.l + (recordIndex + 0.5) * chartWidth / count - barWidth / 2;
      let y = height - pad.b;
      return overview.centers.map((center, centerIndex) => {
        const value = Number(overview.computeByCenter[center.id]?.[recordIndex] || 0);
        const segmentHeight = value / computeMax * chartHeight;
        y -= segmentHeight;
        return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${segmentHeight.toFixed(2)}" fill="${colors[centerIndex]}"/>`;
      }).join('');
    }).join('');
    const powerPoints = overview.powerMw.map((value, index) => {
      const x = count === 1 ? pad.l + chartWidth / 2 : pad.l + index * chartWidth / (count - 1);
      const y = height - pad.b - Number(value || 0) / powerMax * chartHeight;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(' ');
    const grid = [0, .5, 1].map(ratio => `<line x1="${pad.l}" y1="${height - pad.b - ratio * chartHeight}" x2="${width - pad.r}" y2="${height - pad.b - ratio * chartHeight}" stroke="#e5edf5"/>`).join('');
    const legend = [
      ...overview.centers.map((center, index) => ({ label: center.name, color: colors[index] })),
      { label: 'Compute Power', color: '#6d4acb' },
    ].map((item, index) => {
      const column = index % 4;
      const row = Math.floor(index / 4);
      return `<text x="${pad.l + column * 112}" y="${height - 6 - (legendRows - 1 - row) * 11}" fill="${item.color}" font-size="9">● ${item.label}</text>`;
    }).join('');
    target.insertAdjacentHTML('beforeend', `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${target.querySelector('h3').textContent}">${grid}<line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${height-pad.b}" stroke="#9eb0c2"/><line x1="${width-pad.r}" y1="${pad.t}" x2="${width-pad.r}" y2="${height-pad.b}" stroke="#9eb0c2"/><line x1="${pad.l}" y1="${height-pad.b}" x2="${width-pad.r}" y2="${height-pad.b}" stroke="#9eb0c2"/>${bars}<polyline fill="none" stroke="#6d4acb" stroke-width="2.3" points="${powerPoints}"/><text x="4" y="13" fill="#6b7f92" font-size="9">TFLOPS</text><text x="${width-20}" y="13" fill="#6d4acb" font-size="9">MW</text>${legend}</svg>`);
  }

  function longTermPowerChart(target, records) {
    const source = records.length ? records : [{ total_load: 0, total_generation: 0 }];
    const series = TrendCharts.buildLongTermPowerSeries(source);
    const width = 520;
    const legendItems = [...series.generation, ...series.lines];
    const legendRows = Math.max(1, Math.ceil(legendItems.length / 4));
    const height = 145 + Math.max(0, legendRows - 1) * 12;
    const pad = { l: 38, r: 12, t: 9, b: 20 + legendRows * 12 };
    const chartWidth = width - pad.l - pad.r;
    const chartHeight = height - pad.t - pad.b;
    const count = Math.max(1, source.length);
    const generationTotals = source.map((_, index) => series.generation.reduce((sum, item) => sum + Number(item.values[index] || 0), 0));
    const max = Math.max(1, ...generationTotals, ...series.lines.flatMap(item => item.values.map(value => Math.max(0, Number(value || 0)))));
    const xAt = index => count === 1 ? pad.l + chartWidth / 2 : pad.l + index * chartWidth / (count - 1);
    const yAt = value => height - pad.b - Math.max(0, Number(value || 0)) / max * chartHeight;
    const cumulative = Array(count).fill(0);
    const areas = series.generation.map(item => {
      const lower = cumulative.slice();
      item.values.forEach((value, index) => { cumulative[index] += Math.max(0, Number(value || 0)); });
      const upperPoints = cumulative.map((value, index) => `${xAt(index).toFixed(2)},${yAt(value).toFixed(2)}`);
      const lowerPoints = lower.map((value, index) => `${xAt(index).toFixed(2)},${yAt(value).toFixed(2)}`).reverse();
      return `<polygon points="${[...upperPoints, ...lowerPoints].join(' ')}" fill="${item.color}" fill-opacity="0.32" stroke="${item.color}" stroke-width="0.7"/>`;
    }).join('');
    const lines = series.lines.map(item => `<polyline fill="none" stroke="${item.color}" stroke-width="2.3" points="${item.values.map((value, index) => `${xAt(index).toFixed(2)},${yAt(value).toFixed(2)}`).join(' ')}"/>`).join('');
    const grid = [0, .5, 1].map(ratio => `<line x1="${pad.l}" y1="${height - pad.b - ratio * chartHeight}" x2="${width - pad.r}" y2="${height - pad.b - ratio * chartHeight}" stroke="#e5edf5"/>`).join('');
    const legend = legendItems.map((item, index) => {
      const column = index % 4;
      const row = Math.floor(index / 4);
      return `<text x="${pad.l + column * 112}" y="${height - 6 - (legendRows - 1 - row) * 11}" fill="${item.color}" font-size="9">● ${item.label}</text>`;
    }).join('');
    target.insertAdjacentHTML('beforeend', `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${target.querySelector('h3').textContent}">${grid}<line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${height-pad.b}" stroke="#9eb0c2"/><line x1="${pad.l}" y1="${height-pad.b}" x2="${width-pad.r}" y2="${height-pad.b}" stroke="#9eb0c2"/>${areas}${lines}<text x="4" y="13" fill="#6b7f92" font-size="9">MW</text>${legend}</svg>`);
  }

  function renderCharts(records, longRecords = []) {
    state.history = records.length ? records : [{ total_load: 0, total_generation: 0, compute_power_mw: 0, task_summary: { submitted: 0, completed: 0, running: 0 } }];
    const trendSeries = TrendCharts.buildTrendSeries(state.history);
    document.querySelectorAll('.chart svg').forEach(svg => svg.remove());
    lineChart($('generationChart'), trendSeries.power);
    lineChart($('loadChart'), trendSeries.renewable);
    combinedComputeChart($('taskChart'), state.history);
    longTermPowerChart($('computeChart'), longRecords);
  }

  function renderStatus(payload) {
    const incomingStats = payload.latest_stats || {};
    if (Object.keys(incomingStats).length) {
      const previousCpf = state.latestStats.cpf_result;
      state.latestStats = { ...state.latestStats, ...incomingStats };
      if (incomingStats.cpf_result == null && previousCpf) state.latestStats.cpf_result = previousCpf;
    }
    const stats = state.latestStats; const carbon = payload.carbon || {};
    $('runStatus').textContent = payload.status === 'running' ? 'Running' : payload.status === 'stopped' ? 'Stopped' : payload.status === 'configured' ? 'Configured' : 'Stopped';
    $('simulationTime').textContent = formatTime(payload.simulation_second);
    $('totalLoad').innerHTML = `${number(stats.total_load)} <i>MW</i>`;
    $('totalGeneration').innerHTML = `${number(stats.total_generation)} <i>MW</i>`;
    $('totalCarbon').innerHTML = `${number(carbon.total_emission_ton)} <i>tCO₂</i>`;
    $('runningTasks').textContent = String(stats.running_tasks || 0);
    const cumulativeRenewableRate = Number(stats.renewable_energy_consumption_rate);
    $('totalCompute').innerHTML = Number.isFinite(cumulativeRenewableRate)
      ? `${number(cumulativeRenewableRate * 100)} <i>%</i>`
      : '— <i>%</i>';
    const optimizationLabel = ModeCapabilities.statusLabel(payload.optimization || {});
    $('connectionState').textContent = payload.status === 'running'
      ? `Simulation running · ${optimizationLabel}`
      : payload.status === 'configured' ? `Service connected · ${optimizationLabel}` : 'Service connected';
    renderRuntimeCards(stats);
    renderHealth(stats);
    renderOverviewConfiguration(payload.configuration);
    const latestRuntime = Object.values(stats.datacenter_runtime || {});
    ['dcFlowA', 'dcFlowB', 'dcFlowC'].forEach((id, index) => { const value = latestRuntime[index]; $(id).textContent = value ? measurement(value.power_mw, 'MW') : '— MW'; });
    $('windOutput').textContent = measurement(stats.wind_generation_mw, 'MW');
    $('pvOutput').textContent = measurement(stats.pv_generation_mw, 'MW');
    const renewableNodes = document.querySelectorAll('.flow-node.renewable small');
    if (renewableNodes[0]) renewableNodes[0].textContent = `Curtailed ${measurement(stats.wind_curtailed_mw, 'MW')}`;
    if (renewableNodes[1]) renewableNodes[1].textContent = `Curtailed ${measurement(stats.pv_curtailed_mw, 'MW')}`;
    $('gridExchange').textContent = measurement(stats.grid_exchange_mw, 'MW');
    $('storageOutput').textContent = measurement(stats.storage_power_mw, 'MW');
    document.querySelector('.flow-node.storage small').textContent = stats.storage_soc_pct !== null && stats.storage_soc_pct !== undefined
      ? `SOC ${number(stats.storage_soc_pct)}%`
      : 'SOC —';
    if (Object.keys(incomingStats).length) {
      const second = Number(incomingStats.second);
      state.history = TrendCharts.upsertRollingHistory(state.history, incomingStats, 180);
      state.longHistory = TrendCharts.upsertLongTermHistory(state.longHistory, incomingStats, 300, 288);
      renderCharts(state.history, state.longHistory);
    }
    if (payload.status !== 'running' && state.timer) { clearInterval(state.timer); state.timer = null; }
  }
  async function refresh() { try { const response = await fetch('/simulation/status'); if (!response.ok) throw new Error('Status request failed'); renderStatus(await response.json()); } catch (error) { $('connectionState').textContent = 'Connection failed'; message(error.message, 'error'); } }
  function startPolling() { if (!state.timer) state.timer = setInterval(refresh, 1000); refresh(); }

  const dispatchConfigFromForm = () => CustomStrategyConfig.dispatchConfigFromValues({
    name: 'price_load_mix', price: $('customWeightPrice').value, load: $('customWeightLoad').value,
    carbon: $('customWeightCarbon').value, latency: $('customWeightLatency').value,
    allowCrossRegion: $('customAllowCrossRegion').checked,
  });
  const pricingConfigFromForm = () => CustomStrategyConfig.pricingConfigFromValues({
    name: 'grid_responsive_price', basePrice: $('customBasePrice').value,
    localLoad: $('customLocalLoad').value, computeLoad: $('customComputeLoad').value,
    voltageDeviation: $('customVoltageDeviation').value, minimumPrice: $('customMinimumPrice').value,
    maximumPrice: $('customMaximumPrice').value,
  });
  const objectiveConfigFromForm = () => CustomStrategyConfig.objectiveConfigFromValues({
    name: 'custom_multi_objective', cost: $('objectiveCost').value,
    carbon: $('objectiveCarbon').value, delay: $('objectiveDelay').value,
    peakShaving: $('objectivePeakShaving').value,
    renewableCurtailment: $('objectiveCurtailment').value,
    storageDegradation: $('objectiveStorageDegradation').value,
  });
  const replaceOptions = (select, values, labels) => {
    select.replaceChildren(...values.map(value => {
      const option = document.createElement('option'); option.value = value; option.textContent = labels[value]; return option;
    }));
  };
  const rememberModeSelection = (mode = state.activeMode) => {
    if (!mode) return;
    state.modeSelections[mode] = {
      dispatch: $('dispatchStrategy').value || undefined,
      objective: $('objectiveStrategy').value || undefined,
    };
  };
  const renderModeControls = (mode) => {
    const capabilities = ModeCapabilities.capabilitiesFor(mode);
    const selection = ModeCapabilities.selectionForMode(state.modeSelections, mode);
    const managed = Boolean(capabilities.fixedDispatch);
    $('dispatchStrategy').hidden = managed;
    $('dispatchManagedCard').hidden = !managed;
    if (managed) {
      $('dispatchManagedTitle').textContent = 'Managed by the co-optimization model';
      $('dispatchManagedReason').textContent = 'Task placement, generators, renewables, and storage are solved in one model';
    } else {
      replaceOptions($('dispatchStrategy'), capabilities.dispatchStrategies, ModeCapabilities.dispatchLabels);
      $('dispatchStrategy').value = selection.dispatch;
    }
    replaceOptions($('objectiveStrategy'), capabilities.objectives, ModeCapabilities.objectiveLabels);
    $('objectiveStrategy').value = selection.objective;
    $('objectiveDelay').disabled = mode === 'heuristic';
    $('objectiveDelay').closest('label').classList.toggle('field-disabled', mode === 'heuristic');
    state.activeMode = mode;
    toggleCustomPanels();
  };
  const toggleCustomPanels = () => {
    const mode = $('optimizationModel').value;
    $('customDispatchPanel').classList.toggle('visible', (mode === 'heuristic' || mode === 'local') && $('dispatchStrategy').value === 'custom');
    $('customPricingPanel').classList.toggle('visible', $('pricingStrategy').value === 'custom');
    $('customObjectivePanel').classList.toggle('visible', $('objectiveStrategy').value === 'custom');
  };
  $('optimizationModel').addEventListener('change', () => {
    rememberModeSelection();
    renderModeControls($('optimizationModel').value);
  });
  $('dispatchStrategy').addEventListener('change', () => { rememberModeSelection(); toggleCustomPanels(); });
  $('pricingStrategy').addEventListener('change', toggleCustomPanels);
  $('objectiveStrategy').addEventListener('change', () => { rememberModeSelection(); toggleCustomPanels(); });
  const updateFileLabel = (id, hint, emptyText) => {
    const input = $(id); const file = selectedFile(id); const label = input.closest('.file-drop');
    if (label) label.querySelector('b').textContent = file ? `Selected: ${file.name}` : hint;
    const small = label?.querySelector('small'); if (small) small.textContent = file ? 'File staged; click “Apply Configuration” to submit' : emptyText;
  };
  $('taskFile').addEventListener('change', () => updateFileLabel('taskFile', 'Upload Task File', 'Supports .csv / .xlsx / .json'));
  $('userConfig').addEventListener('change', () => updateFileLabel('userConfig', 'Submit User Configuration', 'Supports .yaml / .yml / .json'));
  $('topologyFile').addEventListener('change', () => { updateFileLabel('topologyFile', 'Upload Power Topology JSON', 'Uses built-in IEEE39 when not uploaded'); $('topologyState').textContent = selectedFile('topologyFile') ? `Pending validation: ${selectedFile('topologyFile').name}` : 'Current: built-in IEEE39 topology'; });
  $('pricingCsv').addEventListener('change', () => { updateFileLabel('pricingCsv', 'Import TOU Pricing CSV', 'Fields: start_hour, end_hour, price'); $('pricingState').textContent = selectedFile('pricingCsv') ? `Selected: ${selectedFile('pricingCsv').name}, click “Apply Configuration” to submit` : 'No pricing file selected'; });
  $('customDispatchFile').addEventListener('change', () => { updateFileLabel('customDispatchFile', 'Upload Dispatch Strategy', 'JSON / YAML; file parameters take precedence'); $('customDispatchState').textContent = selectedFile('customDispatchFile') ? `Will use ${selectedFile('customDispatchFile').name}` : 'Using page weights'; });
  $('customPricingFile').addEventListener('change', () => { updateFileLabel('customPricingFile', 'Upload Pricing Strategy', 'JSON / YAML; file parameters take precedence'); $('customPricingState').textContent = selectedFile('customPricingFile') ? `Will use ${selectedFile('customPricingFile').name}` : 'Using page parameters'; });
  $('customObjectiveFile').addEventListener('change', () => { updateFileLabel('customObjectiveFile', 'Upload Objective Configuration', 'JSON / YAML; file parameters take precedence'); $('customObjectiveState').textContent = selectedFile('customObjectiveFile') ? `Will use ${selectedFile('customObjectiveFile').name}` : 'Using page weights'; });
  $('applyButton').addEventListener('click', async () => {
    const pricing = selectedFile('pricingCsv');
    if ($('pricingStrategy').value === 'fixed_tou' && !pricing) { message('TOU pricing requires a TOU pricing CSV file', 'error'); return; }
    try {
      const modePayload = ModeCapabilities.payloadForMode({
        mode: $('optimizationModel').value,
        dispatch: $('dispatchStrategy').value,
        objective: $('objectiveStrategy').value,
        customDispatchConfig: $('dispatchStrategy').value === 'custom' ? dispatchConfigFromForm() : undefined,
        customDispatchFile: selectedFile('customDispatchFile'),
        customObjectiveConfig: $('objectiveStrategy').value === 'custom' ? objectiveConfigFromForm() : undefined,
        customObjectiveFile: selectedFile('customObjectiveFile'),
      });
      const form = new FormData();
      form.append('dispatch_strategy', modePayload.dispatch);
      form.append('pricing_strategy', $('pricingStrategy').value);
      form.append('optimization_model', modePayload.mode);
      form.append('objective_strategy', modePayload.objective);
      if (selectedFile('topologyFile')) form.append('topology_file', selectedFile('topologyFile'));
      if (pricing) form.append('pricing_csv', pricing);
      if (selectedFile('userConfig')) form.append('user_config', selectedFile('userConfig'));
      if (selectedFile('taskFile')) form.append('task_file', selectedFile('taskFile'));
      if (modePayload.customDispatchConfig) {
        form.append('custom_dispatch_config', JSON.stringify(modePayload.customDispatchConfig));
        if (modePayload.customDispatchFile) form.append('custom_dispatch_file', modePayload.customDispatchFile);
      }
      if ($('pricingStrategy').value === 'custom') {
        form.append('custom_pricing_config', JSON.stringify(pricingConfigFromForm()));
        if (selectedFile('customPricingFile')) form.append('custom_pricing_file', selectedFile('customPricingFile'));
      }
      if (modePayload.customObjectiveConfig) {
        form.append('custom_objective_config', JSON.stringify(modePayload.customObjectiveConfig));
        if (modePayload.customObjectiveFile) form.append('custom_objective_file', modePayload.customObjectiveFile);
      }
      const response = await fetch('/config/apply', { method: 'POST', body: form });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Failed to apply configuration');
      state.configured = true; state.history = []; state.longHistory = []; state.latestStats = {}; renderCharts([], []);
      $('totalCompute').innerHTML = '— <i>%</i>'; $('startButton').disabled = false;
      const topologySummary = payload.configuration?.topology_summary;
      if (topologySummary) {
        const source = payload.configuration?.topology_source === 'user_upload' ? 'User upload' : 'Built-in';
        $('topologyState').textContent = `${source}：${topologySummary.name} · ${topologySummary.bus_count} buses · ${topologySummary.line_count} lines · ${topologySummary.transformer_count} transformers`;
      }
      const effectiveDispatch = payload.configuration?.custom_dispatch_strategy;
      if (effectiveDispatch) {
        const w = effectiveDispatch.weights;
        const values = CustomStrategyConfig.dispatchValuesFromConfig(effectiveDispatch);
        $('customWeightPrice').value = values.price;
        $('customWeightLoad').value = values.load;
        $('customWeightCarbon').value = values.carbon;
        $('customWeightLatency').value = values.latency;
        $('customAllowCrossRegion').checked = values.allowCrossRegion;
        $('customDispatchState').textContent = `Effective weights: Price ${number(w.price)} / Load ${number(w.load)} / Carbon ${number(w.carbon)} / Latency ${number(w.latency)}`;
      }
      const effectivePricing = payload.configuration?.custom_pricing_strategy;
      if (effectivePricing) {
        const values = CustomStrategyConfig.pricingValuesFromConfig(effectivePricing);
        $('customBasePrice').value = values.basePrice;
        $('customLocalLoad').value = values.localLoad;
        $('customComputeLoad').value = values.computeLoad;
        $('customVoltageDeviation').value = values.voltageDeviation;
        $('customMinimumPrice').value = values.minimumPrice;
        $('customMaximumPrice').value = values.maximumPrice;
        $('customPricingState').textContent = `Effective price range: ${number(effectivePricing.minimum_price)}–${number(effectivePricing.maximum_price)} yuan/kWh`;
      }
      const effectiveObjective = payload.configuration?.custom_objective;
      if (effectiveObjective) {
        const values = CustomStrategyConfig.objectiveValuesFromConfig(effectiveObjective);
        $('objectiveCost').value = values.cost;
        $('objectiveCarbon').value = values.carbon;
        $('objectiveDelay').value = values.delay;
        $('objectivePeakShaving').value = values.peakShaving;
        $('objectiveCurtailment').value = values.renewableCurtailment;
        $('objectiveStorageDegradation').value = values.storageDegradation;
        $('customObjectiveState').textContent = `Effective: Cost ${number(values.cost)} / Carbon ${number(values.carbon)} / Latency ${number(values.delay)} / Peak Shaving ${number(values.peakShaving)} / Curtailed Renewable ${number(values.renewableCurtailment)} / Degradation ${number(values.storageDegradation)}`;
      }
      const customNote = payload.configuration?.custom_dispatch_strategy || effectiveObjective ? ', custom parameters applied' : '';
      message(`Configuration validated and applied${customNote}`, 'success');
      renderStatus({ status: 'configured', simulation_second: 0, latest_stats: {}, carbon: {}, configuration: payload.configuration });
    } catch (error) { message(error.message, 'error'); }
  });
  $('startButton').addEventListener('click', async () => { state.history = []; state.longHistory = []; state.latestStats = {}; renderCharts([], []); const response = await fetch('/simulation/start', { method: 'POST' }); const payload = await response.json(); if (!response.ok) { message(payload.detail || 'Failed to start simulation', 'error'); return; } message('Simulation started', 'success'); startPolling(); });
  $('stopButton').addEventListener('click', async () => { await fetch('/simulation/stop', { method: 'POST' }); if (state.timer) { clearInterval(state.timer); state.timer = null; } await refresh(); message('Simulation stopped', 'success'); });
  $('exportButton').addEventListener('click', () => { if (!state.configured) { message('Apply the configuration before exporting', 'error'); return; } window.location.href = `/export?format=${encodeURIComponent($('exportFormat').value)}`; });
  renderModeControls($('optimizationModel').value); renderCharts([]); refresh();
})();


