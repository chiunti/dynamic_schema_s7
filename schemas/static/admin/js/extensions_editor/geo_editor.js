/**
 * Geo Point Editor - Specialized editor for geographic coordinates
 * 
 * This module provides a custom editor for geo_point data_type with:
 * - Latitude and longitude inputs
 * - JSON storage format: {lat: number, lng: number}
 * 
 * Pattern is detected by data_type === 'geo_point'.
 */

// Detect if props contain geo_point properties
function isGeoPointPattern(props) {
  return props.some(p => p.data_type === 'geo_point');
}

function renderGeoPointEditor(props) {
  const container = document.getElementById('props');
  container.innerHTML = '';

  const table = document.createElement('table');
  table.className = 's7-table';
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Property</th><th>Type</th><th>Value</th></tr>';
  table.appendChild(thead);
  const tbody = document.createElement('tbody');

  props.forEach(p => {
    if (p.data_type === 'geo_point') {
      // Geographic point: latitude and longitude
      const tr = document.createElement('tr');

      const tdKey = document.createElement('td');
      tdKey.textContent = p.is_required ? `${p.json_key} *` : p.json_key;
      if (p.is_required) {
        tdKey.style.fontWeight = 'bold';
        tdKey.style.color = 'var(--error-fg, #a94442)';
      }
      tr.appendChild(tdKey);

      const tdType = document.createElement('td');
      tdType.textContent = p.domain ? `${p.data_type} (${p.domain})` : p.data_type;
      tr.appendChild(tdType);

      const tdVal = document.createElement('td');

      const current = p.value || {};
      const wrapper = document.createElement('div');
      wrapper.style.display = 'flex';
      wrapper.style.flexDirection = 'column';
      wrapper.style.gap = '4px';

      const raw = current.value_json;
      const lat = (raw && typeof raw === 'object') ? raw.lat : null;
      const lng = (raw && typeof raw === 'object') ? raw.lng : null;

      const latRow = document.createElement('div');
      latRow.style.display = 'flex';
      latRow.style.alignItems = 'center';
      latRow.style.gap = '4px';
      const latLabel = document.createElement('label');
      latLabel.textContent = 'Lat:';
      latLabel.style.fontSize = '0.8em';
      latLabel.style.color = 'var(--body-quiet-color,#666)';
      const latInput = document.createElement('input');
      latInput.type = 'number';
      latInput.step = 'any';
      latInput.style.flex = '1';
      latInput.value = lat ?? '';
      latInput.dataset.geoPart = 'lat';
      latRow.appendChild(latLabel);
      latRow.appendChild(latInput);

      const lngRow = document.createElement('div');
      lngRow.style.display = 'flex';
      lngRow.style.alignItems = 'center';
      lngRow.style.gap = '4px';
      const lngLabel = document.createElement('label');
      lngLabel.textContent = 'Lng:';
      lngLabel.style.fontSize = '0.8em';
      lngLabel.style.color = 'var(--body-quiet-color,#666)';
      const lngInput = document.createElement('input');
      lngInput.type = 'number';
      lngInput.step = 'any';
      lngInput.style.flex = '1';
      lngInput.value = lng ?? '';
      lngInput.dataset.geoPart = 'lng';
      lngRow.appendChild(lngLabel);
      lngRow.appendChild(lngInput);

      wrapper.appendChild(latRow);
      wrapper.appendChild(lngRow);

      // Hidden textarea that holds the JSON object
      const input = document.createElement('textarea');
      input.style.display = 'none';
      input.dataset.json = '1';
      input.dataset.geoWidget = '1';
      input.dataset.jsonKey = p.json_key;
      input.value = raw != null ? JSON.stringify(raw) : '';

      const syncGeo = () => {
        const latVal = latInput.value !== '' ? parseFloat(latInput.value) : null;
        const lngVal = lngInput.value !== '' ? parseFloat(lngInput.value) : null;
        if (latVal !== null || lngVal !== null) {
          input.value = JSON.stringify({ lat: latVal, lng: lngVal });
        } else {
          input.value = '';
        }
        window.s7NodeEditor?.checkPropsChanges();
      };

      latInput.addEventListener('input', syncGeo);
      latInput.addEventListener('change', syncGeo);
      lngInput.addEventListener('input', syncGeo);
      lngInput.addEventListener('change', syncGeo);

      tdVal.appendChild(wrapper);
      tdVal.appendChild(input);
      tr.appendChild(tdVal);
      tbody.appendChild(tr);
    } else {
      // Use shared renderPropRow for other data types
      window.s7NodeEditor?.renderPropRow(p, tbody, window.s7NodeEditor?.checkPropsChanges);
    }
  });

  table.appendChild(tbody);
  container.appendChild(table);
}

// Register this editor with the node editor system
if (window.s7Editors) {
  window.s7Editors.registerRenderer(isGeoPointPattern, renderGeoPointEditor);
}
