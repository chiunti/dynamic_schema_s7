/**
 * Conditional Editor - Specialized editor for conditional logic structures
 *
 * This module provides a custom editor for properties that follow the conditional pattern:
 * - Object with "logic" (and/or) and "conditions" array
 * - Each condition has "left", "op", "right"
 * - Left/right can be {"field": "key"} or {"value": any}
 * - Example: {
 *     "logic": "and",
 *     "conditions": [
 *       {"left": {"field": "age"}, "op": ">=", "right": {"value": 18}}
 *     ]
 *   }
 *
 * Pattern is detected by data_type === 'conditional'.
 * This extension is registered for individual properties, not for all properties.
 */


// Detect if a single property is a conditional field.
// Prefer editor_extension; fall back to data_type name during transition.
function isConditionalPattern(props) {
  if (props.length !== 1) return false;
  const prop = props[0];
  if (prop.editor_extension) {
    return prop.editor_extension === 'conditional_editor';
  }
  return prop.data_type === 'conditional';
}

function renderConditionalEditor(props, tbody) {
  props.forEach(p => {
    renderConditionalRow(p, tbody);
  });
}

function renderConditionalRow(p, tbody) {
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
  // Conditional values (structures or boolean literals) are stored in value_json
  const currentValue = current.value_json;
  const currentConditional = (typeof currentValue === 'object' && currentValue !== null) ? currentValue : null;
  const currentBool = (typeof currentValue === 'boolean') ? currentValue : null;
  const hasBoolValue = currentBool === true || currentBool === false;

  let mode = hasBoolValue ? 'boolean' : 'conditional';

  // Create inline conditional editor
  const wrapper = document.createElement('div');
  wrapper.style.display = 'flex';
  wrapper.style.flexDirection = 'column';
  wrapper.style.gap = '8px';

  // Mode selector
  const modeRow = document.createElement('div');
  modeRow.style.display = 'flex';
  modeRow.style.alignItems = 'center';
  modeRow.style.gap = '8px';

  const modeLabel = document.createElement('label');
  modeLabel.textContent = 'Value type:';
  modeLabel.style.fontWeight = 'bold';
  modeLabel.style.fontSize = '0.85em';
  modeRow.appendChild(modeLabel);

  const modeSelect = document.createElement('select');
  modeSelect.style.padding = '4px';
  modeSelect.style.borderRadius = '3px';
  modeSelect.style.border = '1px solid var(--border-color, #ddd)';
  modeSelect.style.fontSize = '0.85em';

  [
    { value: 'boolean', label: 'Boolean' },
    { value: 'conditional', label: 'Conditional structure' }
  ].forEach(opt => {
    const option = document.createElement('option');
    option.value = opt.value;
    option.textContent = opt.label;
    if (opt.value === mode) option.selected = true;
    modeSelect.appendChild(option);
  });

  modeRow.appendChild(modeSelect);
  wrapper.appendChild(modeRow);

  // Boolean value container
  const booleanContainer = document.createElement('div');
  booleanContainer.style.display = 'flex';
  booleanContainer.style.alignItems = 'center';
  booleanContainer.style.gap = '8px';

  const booleanCheckbox = document.createElement('input');
  booleanCheckbox.type = 'checkbox';
  booleanCheckbox.dataset.checkbox = '1';
  if (currentBool === true) {
    booleanCheckbox.checked = true;
    booleanCheckbox.indeterminate = false;
    booleanCheckbox.dataset.boolState = 'true';
  } else if (currentBool === false) {
    booleanCheckbox.checked = false;
    booleanCheckbox.indeterminate = false;
    booleanCheckbox.dataset.boolState = 'false';
  } else {
    booleanCheckbox.checked = false;
    booleanCheckbox.indeterminate = true;
    booleanCheckbox.dataset.boolState = 'null';
  }

  booleanCheckbox.addEventListener('click', (e) => {
    const currentState = e.target.dataset.boolState;
    if (currentState === 'null') {
      e.target.checked = true;
      e.target.indeterminate = false;
      e.target.dataset.boolState = 'true';
    } else if (currentState === 'true') {
      e.target.checked = false;
      e.target.indeterminate = false;
      e.target.dataset.boolState = 'false';
    } else {
      e.target.checked = false;
      e.target.indeterminate = true;
      e.target.dataset.boolState = 'null';
    }
    syncBoolean();
    window.s7NodeEditor?.checkPropsChanges();
  });

  const booleanLabel = document.createElement('label');
  booleanLabel.style.fontSize = '0.85em';
  booleanLabel.style.display = 'flex';
  booleanLabel.style.alignItems = 'center';
  booleanLabel.style.gap = '6px';
  const booleanLabelText = document.createElement('span');
  booleanLabelText.textContent = 'Boolean value';
  booleanLabel.appendChild(booleanCheckbox);
  booleanLabel.appendChild(booleanLabelText);
  booleanContainer.appendChild(booleanLabel);

  const booleanHint = document.createElement('span');
  booleanHint.style.fontSize = '0.8em';
  booleanHint.style.color = 'var(--body-quiet-color, #666)';
  booleanHint.textContent = 'Click to cycle: null → true → false → null';
  booleanContainer.appendChild(booleanHint);

  wrapper.appendChild(booleanContainer);

  // Conditional structure container
  const conditionalContainer = document.createElement('div');
  conditionalContainer.style.display = 'flex';
  conditionalContainer.style.flexDirection = 'column';
  conditionalContainer.style.gap = '8px';

  // Logic operator selector
  const logicRow = document.createElement('div');
  logicRow.style.display = 'flex';
  logicRow.style.alignItems = 'center';
  logicRow.style.gap = '8px';

  const logicLabel = document.createElement('label');
  logicLabel.textContent = 'Logic:';
  logicLabel.style.fontWeight = 'bold';
  logicLabel.style.fontSize = '0.85em';
  logicRow.appendChild(logicLabel);

  const logicSelect = document.createElement('select');
  logicSelect.style.padding = '4px';
  logicSelect.style.borderRadius = '3px';
  logicSelect.style.border = '1px solid var(--border-color, #ddd)';
  logicSelect.style.fontSize = '0.85em';

  const logic = currentConditional?.logic || 'and';
  const conditions = Array.isArray(currentConditional?.conditions) ? currentConditional.conditions : [];

  ['and', 'or'].forEach(op => {
    const option = document.createElement('option');
    option.value = op;
    option.textContent = op.toUpperCase();
    if (op === logic) option.selected = true;
    logicSelect.appendChild(option);
  });

  logicSelect.addEventListener('change', () => {
    syncConditional();
    window.s7NodeEditor?.checkPropsChanges();
  });

  logicRow.appendChild(logicSelect);
  conditionalContainer.appendChild(logicRow);

  // Create container for conditions (vertical layout)
  const conditionsContainer = document.createElement('div');
  conditionsContainer.style.display = 'flex';
  conditionsContainer.style.flexDirection = 'column';
  conditionsContainer.style.gap = '8px';
  conditionsContainer.style.marginBottom = '8px';
  conditionsContainer.style.fontSize = '0.85em';
  conditionalContainer.appendChild(conditionsContainer);

  // Hidden textarea to store the JSON value (boolean or conditional structure)
  const hiddenInput = document.createElement('textarea');
  hiddenInput.style.display = 'none';
  hiddenInput.dataset.json = '1';
  hiddenInput.dataset.jsonKey = p.json_key;
  wrapper.appendChild(hiddenInput);

  // Allowed operators
  const ALLOWED_OPERATORS = ['==', '!=', '>', '>=', '<', '<=', 'in', 'not_in'];

  // Helper to display a value operand in a text input.
  // Strings are shown without quotes; booleans/numbers/null/objects use JSON.stringify.
  const formatValueOperand = (value) => {
    if (value === undefined) return '';
    if (typeof value === 'string') return value;
    return JSON.stringify(value);
  };

  // Function to render a single condition row
  const renderConditionRow = (index, condition = {}) => {
    const conditionDiv = document.createElement('div');
    conditionDiv.style.display = 'flex';
    conditionDiv.style.flexDirection = 'column';
    conditionDiv.style.gap = '8px';
    conditionDiv.style.padding = '8px';
    conditionDiv.style.border = '1px solid var(--border-color, #ddd)';
    conditionDiv.style.borderRadius = '4px';
    conditionDiv.style.backgroundColor = 'var(--body-bg, #fff)';

    const left = condition.left || {};
    const leftKind = left.field ? 'field' : (left.value !== undefined ? 'value' : 'field');
    const leftValue = leftKind === 'field'
      ? (left.field || '')
      : formatValueOperand(left.value);

    const op = condition.op || '==';

    const right = condition.right || {};
    const rightKind = right.field ? 'field' : (right.value !== undefined ? 'value' : 'value');
    const rightValue = rightKind === 'field'
      ? (right.field || '')
      : formatValueOperand(right.value);

    // Left operand row
    const leftRow = document.createElement('div');
    leftRow.style.display = 'flex';
    leftRow.style.flexDirection = 'column';
    leftRow.style.gap = '4px';

    const leftLabel = document.createElement('label');
    leftLabel.textContent = 'Left';
    leftLabel.style.fontWeight = 'bold';
    leftLabel.style.fontSize = '0.85em';
    leftRow.appendChild(leftLabel);

    const leftInputRow = document.createElement('div');
    leftInputRow.style.display = 'flex';
    leftInputRow.style.gap = '4px';

    const leftKindSelect = document.createElement('select');
    leftKindSelect.style.padding = '4px';
    leftKindSelect.style.borderRadius = '3px';
    leftKindSelect.style.border = '1px solid var(--border-color, #ddd)';
    leftKindSelect.style.fontSize = '0.85em';
    leftKindSelect.style.width = '80px';

    ['field', 'value'].forEach(kind => {
      const option = document.createElement('option');
      option.value = kind;
      option.textContent = kind.charAt(0).toUpperCase() + kind.slice(1);
      if (kind === leftKind) option.selected = true;
      leftKindSelect.appendChild(option);
    });

    leftKindSelect.addEventListener('change', () => {
      const leftInput = conditionDiv.querySelector('.left-operand-input');
      if (leftInput) {
        leftInput.placeholder = leftKindSelect.value === 'field' ? 'Field key' : 'Value';
        syncConditional();
        window.s7NodeEditor?.checkPropsChanges();
      }
    });

    leftInputRow.appendChild(leftKindSelect);

    const leftInput = document.createElement('input');
    leftInput.type = 'text';
    leftInput.value = leftValue;
    leftInput.placeholder = leftKind === 'field' ? 'Field key' : 'Value';
    leftInput.style.flex = '1';
    leftInput.style.padding = '4px';
    leftInput.style.boxSizing = 'border-box';
    leftInput.style.fontSize = '0.85em';
    leftInput.className = 'left-operand-input';
    leftInput.addEventListener('input', () => {
      syncConditional();
      window.s7NodeEditor?.checkPropsChanges();
    });

    leftInputRow.appendChild(leftInput);
    leftRow.appendChild(leftInputRow);
    conditionDiv.appendChild(leftRow);

    // Operator row
    const opRow = document.createElement('div');
    opRow.style.display = 'flex';
    opRow.style.flexDirection = 'column';
    opRow.style.gap = '4px';

    const opLabel = document.createElement('label');
    opLabel.textContent = 'Operator';
    opLabel.style.fontWeight = 'bold';
    opLabel.style.fontSize = '0.85em';
    opRow.appendChild(opLabel);

    const opSelect = document.createElement('select');
    opSelect.style.flex = '1';
    opSelect.style.padding = '4px';
    opSelect.style.borderRadius = '3px';
    opSelect.style.border = '1px solid var(--border-color, #ddd)';
    opSelect.style.fontSize = '0.85em';

    ALLOWED_OPERATORS.forEach(operator => {
      const option = document.createElement('option');
      option.value = operator;
      option.textContent = operator;
      if (operator === op) option.selected = true;
      opSelect.appendChild(option);
    });

    opSelect.addEventListener('change', () => {
      syncConditional();
      window.s7NodeEditor?.checkPropsChanges();
    });

    opRow.appendChild(opSelect);
    conditionDiv.appendChild(opRow);

    // Right operand row
    const rightRow = document.createElement('div');
    rightRow.style.display = 'flex';
    rightRow.style.flexDirection = 'column';
    rightRow.style.gap = '4px';

    const rightLabel = document.createElement('label');
    rightLabel.textContent = 'Right';
    rightLabel.style.fontWeight = 'bold';
    rightLabel.style.fontSize = '0.85em';
    rightRow.appendChild(rightLabel);

    const rightInputRow = document.createElement('div');
    rightInputRow.style.display = 'flex';
    rightInputRow.style.gap = '4px';

    const rightKindSelect = document.createElement('select');
    rightKindSelect.style.padding = '4px';
    rightKindSelect.style.borderRadius = '3px';
    rightKindSelect.style.border = '1px solid var(--border-color, #ddd)';
    rightKindSelect.style.fontSize = '0.85em';
    rightKindSelect.style.width = '80px';

    ['field', 'value'].forEach(kind => {
      const option = document.createElement('option');
      option.value = kind;
      option.textContent = kind.charAt(0).toUpperCase() + kind.slice(1);
      if (kind === rightKind) option.selected = true;
      rightKindSelect.appendChild(option);
    });

    rightKindSelect.addEventListener('change', () => {
      const rightInput = conditionDiv.querySelector('.right-operand-input');
      if (rightInput) {
        rightInput.placeholder = rightKindSelect.value === 'field' ? 'Field key' : 'Value';
        syncConditional();
        window.s7NodeEditor?.checkPropsChanges();
      }
    });

    rightInputRow.appendChild(rightKindSelect);

    const rightInput = document.createElement('input');
    rightInput.type = 'text';
    rightInput.value = rightValue;
    rightInput.placeholder = rightKind === 'field' ? 'Field key' : 'Value';
    rightInput.style.flex = '1';
    rightInput.style.padding = '4px';
    rightInput.style.boxSizing = 'border-box';
    rightInput.style.fontSize = '0.85em';
    rightInput.className = 'right-operand-input';
    rightInput.addEventListener('input', () => {
      syncConditional();
      window.s7NodeEditor?.checkPropsChanges();
    });

    rightInputRow.appendChild(rightInput);
    rightRow.appendChild(rightInputRow);
    conditionDiv.appendChild(rightRow);

    // Delete button row
    const deleteRow = document.createElement('div');
    deleteRow.style.display = 'flex';
    deleteRow.style.justifyContent = 'flex-end';

    const deleteBtn = document.createElement('button');
    deleteBtn.textContent = '× Remove';
    deleteBtn.title = 'Remove condition';
    deleteBtn.style.padding = '4px 10px';
    deleteBtn.style.cursor = 'pointer';
    deleteBtn.style.border = '1px solid var(--border-color, #ddd)';
    deleteBtn.style.borderRadius = '3px';
    deleteBtn.style.backgroundColor = 'var(--button-bg, #fff)';
    deleteBtn.style.fontSize = '0.85em';
    deleteBtn.addEventListener('click', () => {
      conditionDiv.remove();
      syncConditional();
      window.s7NodeEditor?.checkPropsChanges();
    });

    deleteRow.appendChild(deleteBtn);
    conditionDiv.appendChild(deleteRow);

    return conditionDiv;
  };

  // Function to sync all condition rows to hidden textarea
  const syncConditional = () => {
    const logic = logicSelect.value;
    const conditionDivs = conditionsContainer.children;
    const conditions = [];
    
    for (let i = 0; i < conditionDivs.length; i++) {
      const conditionDiv = conditionDivs[i];
      
      const leftInput = conditionDiv.querySelector('.left-operand-input');
      const leftKindSelect = leftInput ? leftInput.previousElementSibling : null;
      const opSelect = conditionDiv.querySelectorAll('select')[1];
      const rightInput = conditionDiv.querySelector('.right-operand-input');
      const rightKindSelect = rightInput ? rightInput.previousElementSibling : null;
      
      if (leftKindSelect && leftInput && opSelect && rightKindSelect && rightInput) {
        const leftKind = leftKindSelect.value;
        const leftValue = leftInput.value.trim();
        const op = opSelect.value;
        const rightKind = rightKindSelect.value;
        const rightValue = rightInput.value.trim();
        
        if (leftValue && rightValue) {
          const condition = {
            left: {},
            op: op,
            right: {}
          };
          
          if (leftKind === 'field') {
            condition.left.field = leftValue;
          } else {
            // Try to parse as JSON for values, otherwise keep as string
            try {
              condition.left.value = JSON.parse(leftValue);
            } catch {
              condition.left.value = leftValue;
            }
          }
          
          if (rightKind === 'field') {
            condition.right.field = rightValue;
          } else {
            try {
              condition.right.value = JSON.parse(rightValue);
            } catch {
              condition.right.value = rightValue;
            }
          }
          
          conditions.push(condition);
        }
      }
    }
    
    // If no conditions, set to null instead of empty object
    if (conditions.length === 0) {
      hiddenInput.value = '';
    } else {
      const conditional = {
        logic: logic,
        conditions: conditions
      };
      hiddenInput.value = JSON.stringify(conditional);
    }
  };

  // Render existing conditions
  conditions.forEach((condition, index) => {
    const conditionDiv = renderConditionRow(index, condition);
    conditionsContainer.appendChild(conditionDiv);
  });

  // Sync initial values to hidden textarea after all rows are rendered
  syncConditional();

  // Add "Add Condition" button
  const addBtnContainer = document.createElement('div');
  addBtnContainer.style.textAlign = 'right';

  const addBtn = document.createElement('button');
  addBtn.textContent = '+ Add Condition';
  addBtn.style.padding = '4px 10px';
  addBtn.style.cursor = 'pointer';
  addBtn.style.border = '1px solid var(--border-color, #ddd)';
  addBtn.style.borderRadius = '3px';
  addBtn.style.backgroundColor = 'var(--button-bg, #fff)';
  addBtn.style.fontSize = '0.85em';
  addBtn.addEventListener('click', () => {
    const newIndex = conditionsContainer.children.length;
    const conditionDiv = renderConditionRow(newIndex, {});
    conditionsContainer.appendChild(conditionDiv);
    // Focus on the left input of the new condition
    const leftInput = conditionDiv.querySelector('.left-operand-input');
    if (leftInput) leftInput.focus();
  });

  addBtnContainer.appendChild(addBtn);
  conditionalContainer.appendChild(addBtnContainer);

  wrapper.appendChild(conditionalContainer);

  // Function to sync boolean value to hidden input
  const syncBoolean = () => {
    const boolState = booleanCheckbox.dataset.boolState;
    if (boolState === 'true') {
      hiddenInput.value = 'true';
    } else if (boolState === 'false') {
      hiddenInput.value = 'false';
    } else {
      hiddenInput.value = '';
    }
  };

  // Toggle visibility based on selected mode
  const updateVisibility = () => {
    const isBoolean = modeSelect.value === 'boolean';
    booleanContainer.style.display = isBoolean ? 'flex' : 'none';
    conditionalContainer.style.display = isBoolean ? 'none' : 'flex';
  };

  modeSelect.addEventListener('change', () => {
    updateVisibility();
    if (modeSelect.value === 'boolean') {
      syncBoolean();
    } else {
      syncConditional();
    }
    window.s7NodeEditor?.checkPropsChanges();
  });

  updateVisibility();
  if (mode === 'boolean') {
    syncBoolean();
  }

  tdVal.appendChild(wrapper);
  tr.appendChild(tdVal);
  tbody.appendChild(tr);
}

// Register this editor with the node editor system for individual properties
if (window.s7Editors) {
  window.s7Editors.registerRenderer(isConditionalPattern, renderConditionalEditor);
}
