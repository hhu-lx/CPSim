(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TrendCharts = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  function buildTrendSeries(records) {
    const source = Array.isArray(records) ? records : [];
    const values = (key) => source.map(record => Number(record?.[key] || 0));
    return {
      power: [
        { label: 'Total Load', color: '#0057b8', values: values('total_load') },
        { label: 'Total Generation', color: '#d62828', values: values('total_generation') },
      ],
      renewable: [
        { label: 'Available Renewable', color: '#0066cc', values: values('renewable_available_mw') },
        { label: 'Consumed Renewable', color: '#009e73', values: values('renewable_consumed_mw') },
        { label: 'Curtailed Renewable', color: '#f08a00', values: values('renewable_curtailed_mw') },
      ],
    };
  }

  function upsertRollingHistory(records, incoming, limit = 180) {
    const next = Array.isArray(records) ? records.slice() : [];
    const second = Number(incoming?.second || 0);
    const index = next.findIndex(record => Number(record?.second || 0) === second);
    if (index >= 0) next[index] = incoming;
    else next.push(incoming);
    next.sort((left, right) => Number(left?.second || 0) - Number(right?.second || 0));
    return next.slice(-Math.max(1, Number(limit) || 1));
  }

  function upsertLongTermHistory(records, incoming, bucketSeconds = 300, limit = 288) {
    const fields = [
      'total_load', 'total_generation', 'grid_exchange_mw',
      'base_generation_mw', 'conventional_generation_mw', 'grid_import_mw',
      'wind_generation_mw', 'pv_generation_mw', 'storage_power_mw',
    ];
    const next = Array.isArray(records) ? records.map(record => ({ ...record, _totals: { ...(record._totals || {}) } })) : [];
    const second = Number(incoming?.second || 0);
    const width = Math.max(1, Number(bucketSeconds) || 300);
    const bucketStart = Math.floor(second / width) * width;
    let bucket = next.find(record => Number(record.bucket_start) === bucketStart);
    if (!bucket) {
      bucket = { bucket_start: bucketStart, second: bucketStart, samples: 0, max_total_load: 0, _totals: {} };
      for (const field of fields) bucket._totals[field] = 0;
      next.push(bucket);
    }

    const values = Object.fromEntries(fields.map(field => [field, Number(incoming?.[field] || 0)]));
    if (bucket._last_second === second && bucket._last_values) {
      for (const field of fields) bucket._totals[field] += values[field] - Number(bucket._last_values[field] || 0);
    } else {
      bucket.samples += 1;
      for (const field of fields) bucket._totals[field] += values[field];
    }
    bucket._last_second = second;
    bucket._last_values = values;
    bucket.max_total_load = Math.max(Number(bucket.max_total_load || 0), values.total_load);
    for (const field of fields) bucket[field] = bucket._totals[field] / Math.max(1, bucket.samples);

    next.sort((left, right) => Number(left.bucket_start) - Number(right.bucket_start));
    return next.slice(-Math.max(1, Number(limit) || 1));
  }

  function buildComputeOverview(records) {
    const source = Array.isArray(records) ? records : [];
    const centers = [];
    const seen = new Set();
    for (const record of source) {
      for (const [id, center] of Object.entries(record?.running_compute_by_datacenter || {})) {
        if (seen.has(id)) continue;
        seen.add(id);
        centers.push({ id, name: `Datacenter ${String.fromCharCode(65 + centers.length)}` });
      }
    }
    const computeByCenter = Object.fromEntries(centers.map(center => [
      center.id,
      source.map(record => Number(record?.running_compute_by_datacenter?.[center.id]?.compute_tflops || 0)),
    ]));
    return {
      centers,
      computeByCenter,
      powerMw: source.map(record => Number(record?.compute_power_mw || 0)),
    };
  }

  function buildLongTermPowerSeries(records) {
    const source = Array.isArray(records) ? records : [];
    const values = (key) => source.map(record => Number(record?.[key] || 0));
    const wind = values('wind_generation_mw');
    const pv = values('pv_generation_mw');
    const storage = values('storage_power_mw').map(value => Math.max(0, value));
    return {
      generation: [
        { label: 'Fixed Base Generation', color: '#4d4d4d', values: values('base_generation_mw') },
        { label: 'Dispatchable Generation', color: '#1f77b4', values: values('conventional_generation_mw') },
        { label: 'External Grid Import', color: '#d62828', values: values('grid_import_mw') },
        { label: 'Wind', color: '#008f5a', values: wind },
        { label: 'Solar', color: '#d97706', values: pv },
        { label: 'Storage Discharge', color: '#b39ddb', values: storage },
      ],
      lines: [
        { label: 'Total Load', color: '#111827', values: values('total_load') },
        { label: 'Grid Exchange', color: '#d62828', values: values('grid_exchange_mw') },
      ],
    };
  }

  return {
    buildTrendSeries,
    upsertRollingHistory,
    upsertLongTermHistory,
    buildComputeOverview,
    buildLongTermPowerSeries,
  };
});


