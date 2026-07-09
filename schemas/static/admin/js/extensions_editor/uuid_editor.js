/**
 * UUID Editor - Specialized editor for UUID properties
 * 
 * This module provides a custom editor for UUID data_type with:
 * - Display of current UUID value
 * - Button to generate a new random UUID
 * - Button to copy UUID to clipboard
 * 
 * Pattern is detected by data_type === 'uuid'.
 */

// Detect if props contain UUID properties.
// Prefer editor_extension; fall back to data_type name during transition.
function isUuidPattern(props) {
  const hasExtension = props.some(p => p.editor_extension === 'uuid_editor');
  if (hasExtension) return true;
  return props.some(p => p.data_type === 'uuid');
}

function renderUuidEditor(props) {
  const container = document.getElementById('props');
  container.innerHTML = '';

  // Build a map of properties by json_key for easy access
  const propMap = {};
  props.forEach(p => {
    propMap[p.json_key] = p;
  });

  const table = document.createElement('table');
  table.className = 's7-table';
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Property</th><th>Type</th><th>Value</th></tr>';
  table.appendChild(thead);
  const tbody = document.createElement('tbody');

  props.forEach(p => {
    if (p.data_type === 'uuid') {
      // Special UUID editor with generate and copy buttons
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
      input.type = 'text';
      input.value = current.value_string ?? '';
      input.style.flex = '1';
      input.dataset.jsonKey = p.json_key;
      input.addEventListener('input', window.s7NodeEditor?.checkPropsChanges);
      input.addEventListener('change', window.s7NodeEditor?.checkPropsChanges);

      // Generate new UUID button
      const generateBtn = document.createElement('button');
      generateBtn.textContent = '🎲';
      generateBtn.title = 'Generate new UUID';
      generateBtn.style.padding = '4px 8px';
      generateBtn.style.cursor = 'pointer';
      generateBtn.addEventListener('click', () => {
        const newUuid = crypto.randomUUID();
        input.value = newUuid;
        window.s7NodeEditor?.checkPropsChanges();
      });

      // Copy to clipboard button
      const copyBtn = document.createElement('button');
      copyBtn.textContent = '📋';
      copyBtn.title = 'Copy to clipboard';
      copyBtn.style.padding = '4px 8px';
      copyBtn.style.cursor = 'pointer';
      copyBtn.addEventListener('click', () => {
        if (input.value) {
          navigator.clipboard.writeText(input.value).then(() => {
            copyBtn.textContent = '✅';
            setTimeout(() => {
              copyBtn.textContent = '📋';
            }, 1000);
          });
        }
      });

      wrapper.appendChild(input);
      wrapper.appendChild(generateBtn);
      wrapper.appendChild(copyBtn);
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
  window.s7Editors.registerRenderer(isUuidPattern, renderUuidEditor);
}
