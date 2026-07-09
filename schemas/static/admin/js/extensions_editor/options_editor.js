/**
 * Options Editor - Specialized editor for options array pattern
 *
 * This module provides a custom editor for properties that follow the options pattern:
 * - Array of objects with "label" and "value" keys
 * - Example: [{ "label": "Option 1", "value": "option1" }, ...]
 *
 * Pattern is detected by data_type === 'options_field'.
 * This extension is registered for individual properties, not for all properties.
 */

// Detect if a single property is an options field.
// Prefer editor_extension; fall back to data_type name during transition.
function isOptionsPattern(props) {
  if (props.length !== 1) return false;
  const prop = props[0];
  if (prop.editor_extension) {
    return prop.editor_extension === 'options_editor';
  }
  return prop.data_type === 'options_field';
}

function renderOptionsEditor(props, tbody) {
  props.forEach(p => {
    renderCustomOptionsRow(p, tbody);
  });
}

function renderCustomOptionsRow(p, tbody) {
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
  const currentOptions = current.value_json || [];
  const optionsArray = Array.isArray(currentOptions) ? currentOptions : [];

  // Create inline options editor
  const wrapper = document.createElement('div');
  wrapper.style.display = 'flex';
  wrapper.style.flexDirection = 'column';
  wrapper.style.gap = '4px';

  // Create table for options
  const optionsTable = document.createElement('table');
  optionsTable.style.width = '100%';
  optionsTable.style.borderCollapse = 'collapse';
  optionsTable.style.marginBottom = '8px';
  optionsTable.style.fontSize = '0.85em';

  const optionsThead = document.createElement('thead');
  optionsThead.innerHTML = `
    <tr style="background-color: var(--darkened-bg, #f5f5f5);">
      <th style="padding: 4px; text-align: left; border-bottom: 1px solid var(--border-color, #ddd);">Label</th>
      <th style="padding: 4px; text-align: left; border-bottom: 1px solid var(--border-color, #ddd);">Value</th>
      <th style="padding: 4px; text-align: center; border-bottom: 1px solid var(--border-color, #ddd); width: 40px;"></th>
    </tr>
  `;
  optionsTable.appendChild(optionsThead);

  const optionsTbody = document.createElement('tbody');
  optionsTable.appendChild(optionsTbody);
  wrapper.appendChild(optionsTable);

  // Hidden textarea to store the JSON array
  const hiddenInput = document.createElement('textarea');
  hiddenInput.style.display = 'none';
  hiddenInput.dataset.json = '1';
  hiddenInput.dataset.jsonKey = 'options';
  hiddenInput.value = JSON.stringify(optionsArray);
  wrapper.appendChild(hiddenInput);

  // Function to render a single option row
  const renderOptionRow = (index, label = '', value = '') => {
    const tr = document.createElement('tr');

    const tdLabel = document.createElement('td');
    tdLabel.style.padding = '3px';
    tdLabel.style.borderBottom = '1px solid var(--border-color, #eee)';

    const labelInput = document.createElement('input');
    labelInput.type = 'text';
    labelInput.value = label;
    labelInput.style.width = '100%';
    labelInput.style.padding = '3px';
    labelInput.style.boxSizing = 'border-box';
    labelInput.style.fontSize = '0.85em';
    labelInput.dataset.optionField = 'label';
    labelInput.addEventListener('input', () => {
      syncOptions();
      window.s7NodeEditor?.checkPropsChanges();
    });

    tdLabel.appendChild(labelInput);
    tr.appendChild(tdLabel);

    const tdValue = document.createElement('td');
    tdValue.style.padding = '3px';
    tdValue.style.borderBottom = '1px solid var(--border-color, #eee)';

    const valueInput = document.createElement('input');
    valueInput.type = 'text';
    valueInput.value = value;
    valueInput.style.width = '100%';
    valueInput.style.padding = '3px';
    valueInput.style.boxSizing = 'border-box';
    valueInput.style.fontSize = '0.85em';
    valueInput.dataset.optionField = 'value';
    valueInput.addEventListener('input', () => {
      syncOptions();
      window.s7NodeEditor?.checkPropsChanges();
    });

    tdValue.appendChild(valueInput);
    tr.appendChild(tdValue);

    const tdActions = document.createElement('td');
    tdActions.style.padding = '3px';
    tdActions.style.textAlign = 'center';
    tdActions.style.borderBottom = '1px solid var(--border-color, #eee)';

    const deleteBtn = document.createElement('button');
    deleteBtn.textContent = '×';
    deleteBtn.title = 'Remove option';
    deleteBtn.style.padding = '2px 6px';
    deleteBtn.style.cursor = 'pointer';
    deleteBtn.style.border = '1px solid var(--border-color, #ddd)';
    deleteBtn.style.borderRadius = '3px';
    deleteBtn.style.backgroundColor = 'var(--button-bg, #fff)';
    deleteBtn.style.fontSize = '0.85em';
    deleteBtn.addEventListener('click', () => {
      tr.remove();
      syncOptions();
      window.s7NodeEditor?.checkPropsChanges();
    });

    tdActions.appendChild(deleteBtn);
    tr.appendChild(tdActions);

    return tr;
  };

  // Function to sync all option rows to hidden textarea
  const syncOptions = () => {
    const rows = optionsTbody.querySelectorAll('tr');
    const options = [];
    rows.forEach(row => {
      const labelInput = row.querySelector('[data-option-field="label"]');
      const valueInput = row.querySelector('[data-option-field="value"]');
      if (labelInput && valueInput) {
        const label = labelInput.value.trim();
        const value = valueInput.value.trim();
        if (label || value) {
          options.push({ label, value });
        }
      }
    });
    hiddenInput.value = JSON.stringify(options);
  };

  // Render existing options
  optionsArray.forEach((opt, index) => {
    const row = renderOptionRow(index, opt.label || '', opt.value || '');
    optionsTbody.appendChild(row);
  });

  // Add "Add Option" button
  const addBtnContainer = document.createElement('div');
  addBtnContainer.style.textAlign = 'right';

  const addBtn = document.createElement('button');
  addBtn.textContent = '+ Add';
  addBtn.style.padding = '4px 10px';
  addBtn.style.cursor = 'pointer';
  addBtn.style.border = '1px solid var(--border-color, #ddd)';
  addBtn.style.borderRadius = '3px';
  addBtn.style.backgroundColor = 'var(--button-bg, #fff)';
  addBtn.style.fontSize = '0.85em';
  addBtn.addEventListener('click', () => {
    const newIndex = optionsTbody.children.length;
    const row = renderOptionRow(newIndex, '', '');
    optionsTbody.appendChild(row);
    // Focus on the label input of the new row
    const labelInput = row.querySelector('[data-option-field="label"]');
    if (labelInput) labelInput.focus();
  });

  addBtnContainer.appendChild(addBtn);
  wrapper.appendChild(addBtnContainer);

  tdVal.appendChild(wrapper);
  tr.appendChild(tdVal);
  tbody.appendChild(tr);
}

// Register this editor with the node editor system for individual properties
if (window.s7Editors) {
  window.s7Editors.registerRenderer(isOptionsPattern, renderOptionsEditor);
}
