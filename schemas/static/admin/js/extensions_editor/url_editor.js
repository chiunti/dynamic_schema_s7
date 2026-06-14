/**
 * URL Editor - Specialized editor for URL properties
 * 
 * This module provides a custom editor for URL data_type with:
 * - URL input with validation
 * - Button to open URL in new tab
 * 
 * Pattern is detected by data_type === 'url'.
 */

// Detect if props contain URL properties
function isUrlPattern(props) {
  return props.some(p => p.data_type === 'url');
}

function renderUrlEditor(props) {
  const container = document.getElementById('props');
  container.innerHTML = '';

  const table = document.createElement('table');
  table.className = 's7-table';
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Property</th><th>Type</th><th>Value</th></tr>';
  table.appendChild(thead);
  const tbody = document.createElement('tbody');

  props.forEach(p => {
    if (p.data_type === 'url') {
      // URL input with validation and open button
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
      wrapper.style.alignItems = 'center';
      wrapper.style.gap = '6px';

      const input = document.createElement('input');
      input.type = 'url';
      input.style.flex = '1';
      input.value = current.value_string ?? '';
      input.placeholder = 'https://example.com';
      input.dataset.jsonKey = p.json_key;
      input.addEventListener('input', window.s7NodeEditor?.checkPropsChanges);
      input.addEventListener('change', window.s7NodeEditor?.checkPropsChanges);

      const openBtn = document.createElement('button');
      openBtn.textContent = '🔗';
      openBtn.title = 'Open URL';
      openBtn.style.padding = '4px 8px';
      openBtn.style.cursor = 'pointer';
      openBtn.addEventListener('click', () => {
        if (input.value) {
          window.open(input.value, '_blank');
        }
      });

      wrapper.appendChild(input);
      wrapper.appendChild(openBtn);
      tdVal.appendChild(wrapper);
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
  window.s7Editors.registerRenderer(isUrlPattern, renderUrlEditor);
}
