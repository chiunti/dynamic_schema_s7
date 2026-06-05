  const baseUrl = window.location.pathname.replace(/\/editor\/?$/, '');
  const apiTree = baseUrl + '/editor/api/tree/';
  const apiNode = (id) => baseUrl + `/editor/api/node/${id}/`;
  const apiProps = (id) => baseUrl + `/editor/api/node/${id}/properties/`;
  const apiAllowedChildren = (id) => baseUrl + `/editor/api/node/${id}/allowed-children/`;
  const apiCreate = baseUrl + '/editor/api/create/';
  const apiDelete = (id) => baseUrl + `/editor/api/delete/${id}/`;
  const apiMove = baseUrl + '/editor/api/move/';
  const apiReorder = baseUrl + '/editor/api/reorder/';
  const apiNodeJson = baseUrl + '/editor/api/node-json/';
  const apiNodeTypeVariants = (nodeType) => `${baseUrl}/editor/api/node-type-variants/?node_type=${nodeType}`;

  // Alert messages
  const MSG_PROVIDE_NODE_ID_OR_KEY_VERSION = 'Provide node_id or key+version';

  function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(';').shift();
  }

  async function requestJson(url, options = {}) {
    const headers = options.headers || {};
    headers['Content-Type'] = headers['Content-Type'] || 'application/json';
    const csrf = getCookie('csrftoken');
    if (csrf) headers['X-CSRFToken'] = csrf;
    options.headers = headers;
    const resp = await fetch(url, options);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const msg = data && data.error ? data.error : `HTTP ${resp.status}`;
      throw new Error(msg);
    }
    return data;
  }

  let currentRootId = null;
  let currentSelectedNodeId = null;
  let currentTree = [];
  let jsonNodeLineMap = new Map(); // Maps node_id to { startLine, endLine }

  function getQueryParams() {
    const node_id = document.getElementById('node_id').value.trim();
    const key = document.getElementById('key').value.trim();
    const version = document.getElementById('version').value.trim();
    const params = new URLSearchParams();
    if (node_id) params.set('node_id', node_id);
    if (!node_id && key && version) {
      params.set('key', key);
      params.set('version', version);
    }
    return params;
  }

  function buildTree(nodes) {
    const byId = new Map();
    nodes.forEach(n => byId.set(String(n.id), { ...n, children: [] }));
    const roots = [];
    nodes.forEach(n => {
      const id = String(n.id);
      const parentId = n.parent_id ? String(n.parent_id) : null;
      const item = byId.get(id);
      if (!parentId || !byId.has(parentId)) {
        roots.push(item);
      } else {
        byId.get(parentId).children.push(item);
      }
    });
    const sortRec = (arr) => {
      arr.sort((a,b) => (a.position ?? 0) - (b.position ?? 0));
      arr.forEach(x => sortRec(x.children));
    };
    sortRec(roots);
    return roots;
  }

  function renderTree(container, roots) {
    container.innerHTML = '';

    function buildUl(nodes, parentEl) {
      if (!nodes || !nodes.length) return;
      const ul = document.createElement('ul');
      ul.classList.add('s7-tree-children');

      nodes.forEach((node, idx) => {
        const isLast = idx === nodes.length - 1;
        const li = document.createElement('li');
        if (isLast) li.classList.add('s7-last');

        const row = document.createElement('div');
        row.classList.add('s7-tree-row');
        row.dataset.nodeId = node.id;
        row.dataset.parentId = node.parent_id || '';
        row.textContent = `${node.name} (${node.node_type})`;

        row.draggable = true;
        row.ondragstart = (ev) => {
          ev.dataTransfer.setData('text/plain', String(node.id));
          ev.dataTransfer.setData('parent-id', String(node.parent_id || ''));
          row.classList.add('dragging');
        };
        row.ondragend = (ev) => {
          row.classList.remove('dragging');
          document.querySelectorAll('.drop-indicator').forEach(el => el.remove());
        };
        row.ondragover = (ev) => {
          ev.preventDefault();
          const rect = row.getBoundingClientRect();
          const isAbove = ev.clientY < rect.top + rect.height / 2;
          document.querySelectorAll('.drop-indicator').forEach(el => el.remove());
          const indicator = document.createElement('div');
          indicator.className = 'drop-indicator';
          indicator.style.position = 'fixed';
          indicator.style.left = rect.left + 'px';
          indicator.style.width = rect.width + 'px';
          indicator.style.height = '3px';
          indicator.style.backgroundColor = '#1890ff';
          indicator.style.zIndex = '1000';
          indicator.style.pointerEvents = 'none';
          indicator.style.top = (isAbove ? rect.top : rect.bottom) + 'px';
          document.body.appendChild(indicator);
        };
        row.ondragleave = (ev) => {
          document.querySelectorAll('.drop-indicator').forEach(el => el.remove());
        };
        row.ondrop = async (ev) => {
          ev.preventDefault();
          document.querySelectorAll('.drop-indicator').forEach(el => el.remove());
          const draggedId = ev.dataTransfer.getData('text/plain');
          const draggedParentId = ev.dataTransfer.getData('parent-id');
          const targetId = String(node.id);
          const targetParentId = String(node.parent_id || '');
          if (!draggedId || draggedId === targetId) return;
          try {
            if (draggedParentId === targetParentId) {
              const siblings = currentTree.filter(n => String(n.parent_id) === draggedParentId).sort((a, b) => a.position - b.position);
              const draggedIndex = siblings.findIndex(n => n.id === draggedId);
              const targetIndex = siblings.findIndex(n => n.id === targetId);
              if (draggedIndex !== -1 && targetIndex !== -1) {
                const direction = targetIndex > draggedIndex ? 'down' : 'up';
                for (let i = 0; i < Math.abs(targetIndex - draggedIndex); i++) {
                  await requestJson(apiReorder, { method: 'POST', body: JSON.stringify({ node_id: draggedId, direction }) });
                }
              }
            } else {
              await requestJson(apiMove, { method: 'POST', body: JSON.stringify({ node_id: draggedId, new_parent_id: targetId }) });
            }
            const savedNodeId = draggedId;
            await loadTree();
            await selectNode(savedNodeId);
          } catch (e) {
            alert(e.message);
          }
        };
        row.onclick = async () => { selectNode(String(node.id)); };

        li.appendChild(row);
        buildUl(node.children, li);
        ul.appendChild(li);
      });

      parentEl.appendChild(ul);
    }

    buildUl(roots, container);
  }

  function highlightSelected() {
    document.querySelectorAll('[data-node-id]').forEach(el => {
      if (String(el.dataset.nodeId) === String(currentSelectedNodeId)) {
        el.classList.add('s7-selected');
      } else {
        el.classList.remove('s7-selected');
      }
    });
  }

  async function updateNodeJson() {
    if (!currentRootId) return;

    const jsonParams = new URLSearchParams();
    jsonParams.set('node_id', currentRootId);

    try {
      const jsonData = await requestJson(apiNodeJson + '?' + jsonParams.toString(), { method: 'GET' });
      const jsonString = jsonData.json || '{}';

      // Build node-to-line mapping from server-provided map
      jsonNodeLineMap = buildNodeLineMap(jsonData.node_line_map || {});

      // Render JSON with line spans
      renderJsonWithLines(jsonString);

      // Re-apply highlight for the currently selected node
      if (currentSelectedNodeId) highlightJsonNode(currentSelectedNodeId);

      // Display warnings if any
      const warningsContainer = document.getElementById('json_warnings');
      warningsContainer.innerHTML = '';
      if (jsonData.warnings && jsonData.warnings.length > 0) {
        warningsContainer.style.display = 'block';
        warningsContainer.classList.add('s7-json-warnings--blocked');

        // Critical banner - publishing is blocked
        const criticalBanner = document.createElement('div');
        criticalBanner.className = 's7-warning-critical';
        criticalBanner.innerHTML = `
          <strong>🚫 Cannot Publish Schema</strong>
          <span>Complete all required properties before publishing.</span>
        `;
        warningsContainer.appendChild(criticalBanner);

        const warningTitle = document.createElement('div');
        warningTitle.className = 's7-warning-title';
        const totalProps = jsonData.warnings.reduce((sum, w) => sum + w.missing.length, 0);
        warningTitle.textContent = `⚠️ ${jsonData.warnings.length} node(s) with ${totalProps} missing required properties:`;
        warningsContainer.appendChild(warningTitle);

        jsonData.warnings.forEach(w => {
          const warningItem = document.createElement('div');
          warningItem.className = 's7-warning-item';
          warningItem.innerHTML = `<strong>${w.node_name}</strong> (${w.node_type}): missing <code>${w.missing.join('</code>, <code>')}</code>`;
          warningsContainer.appendChild(warningItem);
        });
      } else {
        warningsContainer.style.display = 'none';
        warningsContainer.classList.remove('s7-json-warnings--blocked');
      }
    } catch (e) {
      document.getElementById('json_output').textContent = 'Error loading JSON: ' + e.message;
    }
  }

  function buildNodeLineMap(serverNodeLineMap) {
    // Server provides { node_id: [startLine, endLine] } for every node it could locate in the JSON.
    // Convert to a Map for O(1) lookup.
    const lineMap = new Map();
    if (serverNodeLineMap) {
      for (const [id, range] of Object.entries(serverNodeLineMap)) {
        lineMap.set(id, { startLine: range[0], endLine: range[1] });
      }
    }
    return lineMap;
  }

  function renderJsonWithLines(jsonString) {
    const output = document.getElementById('json_output');
    const lines = jsonString.split('\n');

    output.innerHTML = '';

    lines.forEach((line, index) => {
      const lineDiv = document.createElement('div');
      lineDiv.className = 's7-json-line';
      lineDiv.dataset.lineIndex = index;
      lineDiv.textContent = line;
      output.appendChild(lineDiv);
    });
  }

  function highlightJsonNode(nodeId) {
    document.querySelectorAll('.s7-json-line-highlight').forEach(el => {
      el.classList.remove('s7-json-line-highlight');
    });

    if (!nodeId || !jsonNodeLineMap.has(nodeId)) return;

    const { startLine, endLine } = jsonNodeLineMap.get(nodeId);
    const output = document.getElementById('json_output');

    for (let i = startLine; i <= endLine; i++) {
      const lineEl = output.querySelector(`[data-line-index="${i}"]`);
      if (lineEl) lineEl.classList.add('s7-json-line-highlight');
    }
  }

  async function loadTree(selectAfterLoad = null) {
    const params = getQueryParams();
    if (![...params.keys()].length) {
      alert(MSG_PROVIDE_NODE_ID_OR_KEY_VERSION);
      return;
    }

    const data = await requestJson(apiTree + '?' + params.toString(), { method: 'GET' });
    currentRootId = String(data.root_id);
    currentTree = data.nodes || [];

    const roots = buildTree(currentTree);
    renderTree(document.getElementById('tree'), roots);

    // Show form JSON using s7 function
    await updateNodeJson();

    currentSelectedNodeId = null;
    clearRightPanel();

    // Wait for DOM to update before selecting
    await new Promise(resolve => setTimeout(resolve, 0));

    // Only select root node by default if no specific node was provided
    if (selectAfterLoad === null && currentRootId) {
      await selectNode(currentRootId);
    }
  }

  function clearRightPanel() {
    document.getElementById('sel_title').textContent = '(select a node)';
    document.getElementById('node_name').value = '';
    document.getElementById('node_name').dataset.originalName = '';
    document.getElementById('props').innerHTML = '';
    document.getElementById('props').dataset.originalProps = '';
    document.getElementById('btn_save_key').disabled = true;
    document.getElementById('btn_save_props').disabled = true;
    document.getElementById('btn_delete').disabled = true;
    document.getElementById('btn_up').disabled = true;
    document.getElementById('btn_down').disabled = true;
    document.getElementById('create_node_type').disabled = true;
    document.getElementById('create_key').disabled = true;
    document.getElementById('btn_create').disabled = true;
  }

  async function setCreateAllowed(allowed, parentNodeType = null) {
    const sel = document.getElementById('create_node_type');

    sel.innerHTML = '';
    allowed.forEach(a => {
      const opt = document.createElement('option');
      opt.value = a.node_type;
      opt.textContent = `${a.label} (${a.node_type})`;
      sel.appendChild(opt);
    });
    const has = allowed.length > 0;
    sel.disabled = !has;
    document.getElementById('create_key').disabled = !has;
    document.getElementById('btn_create').disabled = !has;

    const updateVariantRow = async () => {
      const nodeType = sel.value;
      const variantRow = document.getElementById('variant_row');
      const variantSel = document.getElementById('variant_select');

      // Skip API call if nodeType is empty
      if (!nodeType) {
        variantRow.classList.add('hidden');
        variantSel.innerHTML = '';
        document.getElementById('btn_create').disabled = allowed.length === 0;
        return;
      }

      // Data-driven: try to load variants from backend
      try {
        const variantsData = await requestJson(apiNodeTypeVariants(nodeType), { method: 'GET' });
        const options = variantsData.options || [];
        const hasVariants = options.length > 0;

        variantRow.classList.toggle('hidden', !hasVariants);
        variantSel.innerHTML = '';

        if (hasVariants) {
          const emptyOpt = document.createElement('option');
          emptyOpt.value = '';
          emptyOpt.textContent = '— select type —';
          variantSel.appendChild(emptyOpt);
          options.forEach(opt => {
            const o = document.createElement('option');
            o.value = opt.value;
            o.textContent = opt.label;
            variantSel.appendChild(o);
          });
          document.getElementById('btn_create').disabled = true; // Require variant selection
        } else {
          document.getElementById('btn_create').disabled = allowed.length === 0;
        }
      } catch (e) {
        console.warn('Failed to load variants for node type:', nodeType, e);
        variantRow.classList.add('hidden');
        variantSel.innerHTML = '';
        document.getElementById('btn_create').disabled = allowed.length === 0;
      }
    };

    sel.removeEventListener('change', sel._variantRowHandler);
    sel._variantRowHandler = updateVariantRow;
    sel.addEventListener('change', sel._variantRowHandler);

    // Enable create button when variant is selected
    const variantSel = document.getElementById('variant_select');
    variantSel.removeEventListener('change', variantSel._variantEnableHandler);
    variantSel._variantEnableHandler = () => {
      const variantRow = document.getElementById('variant_row');
      const hasVariants = !variantRow.classList.contains('hidden');
      if (hasVariants) {
        document.getElementById('btn_create').disabled = !variantSel.value;
      }
    };
    variantSel.addEventListener('change', variantSel._variantEnableHandler);

    await updateVariantRow();
  }

  async function selectNode(nodeId) {
    try {
      currentSelectedNodeId = String(nodeId);
      highlightSelected();
      highlightJsonNode(currentSelectedNodeId);

      const node = await requestJson(apiNode(nodeId), { method: 'GET' });
      document.getElementById('sel_title').textContent = `${node.name} (${node.node_type})`;
      document.getElementById('node_name').value = node.name;
      // Save original name to detect changes
      document.getElementById('node_name').dataset.originalName = node.name;
      document.getElementById('btn_save_key').disabled = true;
      document.getElementById('btn_save_props').disabled = false;
      document.getElementById('btn_delete').disabled = false;
      document.getElementById('btn_up').disabled = false;
      document.getElementById('btn_down').disabled = false;

      const allowed = await requestJson(apiAllowedChildren(nodeId), { method: 'GET' });
      await setCreateAllowed(allowed.allowed || [], node.node_type);

      const propsData = await requestJson(apiProps(nodeId), { method: 'GET' });

      // Save original property values
      const propsContainer = document.getElementById('props');
      propsContainer.dataset.originalProps = JSON.stringify(propsData.properties || []);
      propsContainer.dataset.nodeType = node.node_type;
      document.getElementById('btn_save_props').disabled = true;

      // Render properties - completely generic based on AttributeDefs defined by user
      renderProps(propsData.properties || []);
    } catch (e) {
      console.error('Error selecting node:', e);
      alert('Error loading node properties: ' + e.message);
    }
  }

  function checkPropsChanges() {
    const propsContainer = document.getElementById('props');
    const originalProps = JSON.parse(propsContainer.dataset.originalProps || '[]');
    const currentProps = [];

    document.querySelectorAll('#props [data-json-key]').forEach(el => {
      const jsonKey = el.dataset.jsonKey;
      let value;
      if (el.dataset.checkbox === '1' && el.dataset.boolState) {
        const boolState = el.dataset.boolState;
        if (boolState === 'true') {
          value = true;
        } else if (boolState === 'false') {
          value = false;
        } else {
          value = null;
        }
      } else if (el.dataset.checkbox === '1') {
        value = el.checked;
      } else if (el.dataset.json === '1') {
        const jsonStr = el.value.trim();
        value = jsonStr ? JSON.parse(jsonStr) : null;
      } else if (el.type === 'number') {
        value = el.value ? parseFloat(el.value) : null;
      } else {
        value = el.value || null;
      }
      currentProps.push({ json_key: jsonKey, value });
    });

    // Compare with original values
    let hasChanges = false;
    const originalMap = new Map(originalProps.map(p => [p.json_key, p.value.value_string ?? p.value.value_number ?? p.value.value_bool ?? p.value.value_json]));
    const currentMap = new Map(currentProps.map(p => [p.json_key, p.value]));

    for (const [key, currentValue] of currentMap) {
      const originalValue = originalMap.get(key);
      // Use JSON.stringify for objects/arrays, String for primitives
      const toStr = v => (v !== null && typeof v === 'object') ? JSON.stringify(v) : String(v);
      const currentStr = toStr(currentValue);
      const originalStr = toStr(originalValue);
      if (currentStr !== originalStr) {
        hasChanges = true;
        break;
      }
    }

    document.getElementById('btn_save_props').disabled = !hasChanges;
  }

  function buildPropsTable(props) {
    const table = document.createElement('table');
    table.className = 's7-table';
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>Property</th><th>Type</th><th>Value</th></tr>';
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    props.forEach(p => {
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

      let input;
      const current = p.value || {};

      if (p.data_type === 'domain_list' && p.domain) {
        // Multi-select with checkboxes for domain items
        const wrapper = document.createElement('div');
        wrapper.style.display = 'flex';
        wrapper.style.flexDirection = 'column';
        wrapper.style.gap = '4px';
        wrapper.style.maxHeight = '200px';
        wrapper.style.overflowY = 'auto';
        wrapper.style.padding = '4px';
        wrapper.style.border = '1px solid var(--border-color,#ddd)';
        wrapper.style.borderRadius = '4px';

        const currentValues = current.value_json || [];
        const valuesArray = Array.isArray(currentValues) ? currentValues : [];

        (p.domain_items || []).forEach(di => {
          const labelRow = document.createElement('label');
          labelRow.style.display = 'flex';
          labelRow.style.alignItems = 'center';
          labelRow.style.gap = '6px';
          labelRow.style.fontSize = '0.85em';

          const checkbox = document.createElement('input');
          checkbox.type = 'checkbox';
          checkbox.value = di.value;
          checkbox.checked = valuesArray.includes(di.value);
          checkbox.dataset.domainList = '1';
          checkbox.addEventListener('change', checkPropsChanges);

          const labelText = document.createElement('span');
          labelText.textContent = di.label;

          labelRow.appendChild(checkbox);
          labelRow.appendChild(labelText);
          wrapper.appendChild(labelRow);
        });

        // Hidden textarea to store the JSON array
        input = document.createElement('textarea');
        input.style.display = 'none';
        input.dataset.json = '1';
        input.dataset.domainListWidget = '1';
        input.value = valuesArray.length > 0 ? JSON.stringify(valuesArray) : '';

        // Sync checkboxes to hidden textarea
        const syncDomainList = () => {
          const checked = wrapper.querySelectorAll('input[type="checkbox"]:checked');
          const values = Array.from(checked).map(cb => cb.value);
          input.value = values.length > 0 ? JSON.stringify(values) : '';
          checkPropsChanges();
        };

        wrapper.querySelectorAll('input[type="checkbox"]').forEach(cb => {
          cb.addEventListener('change', syncDomainList);
        });

        tdVal.appendChild(wrapper);
        tdVal.appendChild(input);
        input.dataset.jsonKey = p.json_key;
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
        return;
      } else if (p.data_type === 'int_tuple') {
        // Fixed-length array of integers
        // Length is determined by domain_items count if available
        const wrapper = document.createElement('div');
        wrapper.style.display = 'flex';
        wrapper.style.flexDirection = 'column';
        wrapper.style.gap = '4px';

        const currentValues = current.value_json || [];
        const valuesArray = Array.isArray(currentValues) ? currentValues : [];

        // Use labels from domain_items if available, otherwise show warning
        let labels;
        let fixedLength = null;
        if (p.domain && p.domain_items && p.domain_items.length > 0) {
          // Use natural order from domain_items
          labels = p.domain_items.map(di => di.label);
          fixedLength = p.domain_items.length;
        } else {
          // No domain: render warning and skip widget
          const warn = document.createElement('div');
          warn.className = 's7-field-warning';
          warn.textContent = `⚠ int_tuple requires a domain to define its fields. Assign a domain to attribute "${p.json_key}" to enable editing.`;
          tdVal.appendChild(warn);
          tr.appendChild(tdVal);
          tbody.appendChild(tr);
          return;
        }

        // If fixed length is specified by domain_items, ensure we have that many fields
        const displayLength = fixedLength || valuesArray.length;
        const displayValues = fixedLength
          ? valuesArray.length >= fixedLength ? valuesArray.slice(0, fixedLength) : Array(fixedLength).fill(0)
          : valuesArray;

        const inputs = [];

        labels.forEach((label, index) => {
          const row = document.createElement('div');
          row.style.display = 'flex';
          row.style.alignItems = 'center';
          row.style.gap = '6px';

          const labelSpan = document.createElement('span');
          labelSpan.textContent = label;
          labelSpan.style.fontSize = '0.85em';
          labelSpan.style.minWidth = '30px';

          const numInput = document.createElement('input');
          numInput.type = 'number';
          numInput.value = displayValues[index] ?? 0;
          numInput.style.flex = '1';
          numInput.dataset.intTuple = '1';
          numInput.dataset.index = index;
          numInput.addEventListener('change', checkPropsChanges);

          row.appendChild(labelSpan);
          row.appendChild(numInput);
          wrapper.appendChild(row);
          inputs.push(numInput);
        });

        // Hidden textarea to store the JSON array
        input = document.createElement('textarea');
        input.style.display = 'none';
        input.dataset.json = '1';
        input.dataset.intTupleWidget = '1';
        input.value = JSON.stringify(displayValues);

        // Sync number inputs to hidden textarea
        const syncIntTuple = () => {
          const values = inputs.map(inp => parseFloat(inp.value) || 0);
          input.value = JSON.stringify(values);
          checkPropsChanges();
        };

        inputs.forEach(inp => {
          inp.addEventListener('change', syncIntTuple);
        });

        tdVal.appendChild(wrapper);
        tdVal.appendChild(input);
        input.dataset.jsonKey = p.json_key;
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
        return;
      } else if (p.data_type === 'dict') {
        // Dictionary with key-value pairs
        // If domain_items are available, show fixed fields with those keys
        // Otherwise show generic key-value editor
        const hasDomainItems = p.domain && p.domain_items && p.domain_items.length > 0;

        if (hasDomainItems) {
          // Fixed fields based on domain_items
          const wrapper = document.createElement('div');
          wrapper.style.display = 'flex';
          wrapper.style.flexDirection = 'column';
          wrapper.style.gap = '4px';

          const currentDict = current.value_json || {};
          const dictObj = typeof currentDict === 'object' && currentDict !== null ? currentDict : {};

          const labels = p.domain_items.map(di => di.label);
          const inputs = [];

          labels.forEach(label => {
            const row = document.createElement('div');
            row.style.display = 'flex';
            row.style.alignItems = 'center';
            row.style.gap = '6px';

            const labelSpan = document.createElement('span');
            labelSpan.textContent = label;
            labelSpan.style.fontSize = '0.85em';
            labelSpan.style.minWidth = '50px';

            const valueInput = document.createElement('input');
            valueInput.type = 'text';
            valueInput.value = dictObj[label] ?? '';
            valueInput.style.flex = '1';
            valueInput.dataset.dictFixedKey = '1';
            valueInput.addEventListener('change', checkPropsChanges);

            row.appendChild(labelSpan);
            row.appendChild(valueInput);
            wrapper.appendChild(row);
            inputs.push(valueInput);
          });

          // Hidden textarea to store the JSON object
          input = document.createElement('textarea');
          input.style.display = 'none';
          input.dataset.json = '1';
          input.dataset.dictFixedWidget = '1';

          const syncDictFixed = () => {
            const obj = {};
            labels.forEach((label, index) => {
              const val = inputs[index].value.trim();
              if (val) obj[label] = val;
            });
            input.value = JSON.stringify(obj);
            checkPropsChanges();
          };

          inputs.forEach(inp => {
            inp.addEventListener('change', syncDictFixed);
          });

          syncDictFixed();

          tdVal.appendChild(wrapper);
          tdVal.appendChild(input);
          input.dataset.jsonKey = p.json_key;
          tr.appendChild(tdVal);
          tbody.appendChild(tr);
          return;
        }

        // Generic dict editor for other cases
        const wrapper = document.createElement('div');
        wrapper.style.display = 'flex';
        wrapper.style.flexDirection = 'column';
        wrapper.style.gap = '4px';

        const currentDict = current.value_json || {};
        const dictObj = typeof currentDict === 'object' && currentDict !== null ? currentDict : {};

        const rows = [];

        const renderDictRows = () => {
          // Clear existing rows
          rows.forEach(row => wrapper.removeChild(row));
          rows.length = 0;

          Object.entries(dictObj).forEach(([key, value]) => {
            const row = document.createElement('div');
            row.style.display = 'flex';
            row.style.alignItems = 'center';
            row.style.gap = '4px';

            const keyInput = document.createElement('input');
            keyInput.type = 'text';
            keyInput.value = key;
            keyInput.style.flex = '1';
            keyInput.style.minWidth = '80px';
            keyInput.dataset.dictKey = '1';
            keyInput.addEventListener('change', () => {
              const newKey = keyInput.value.trim();
              if (newKey && newKey !== key) {
                dictObj[newKey] = dictObj[key];
                delete dictObj[key];
                renderDictRows();
                syncDict();
              }
            });

            const valueInput = document.createElement('input');
            valueInput.type = 'text';
            valueInput.value = value !== null && value !== undefined ? String(value) : '';
            valueInput.style.flex = '1';
            valueInput.dataset.dictValue = '1';
            valueInput.addEventListener('change', () => {
              const raw = valueInput.value.trim();
              // Strip surrounding quotes → always treat as literal string
              if ((raw.startsWith('"') && raw.endsWith('"')) || (raw.startsWith("'") && raw.endsWith("'"))) {
                dictObj[key] = raw.slice(1, -1);
              } else if (raw === 'true') {
                dictObj[key] = true;
              } else if (raw === 'false') {
                dictObj[key] = false;
              } else if (raw !== '' && !isNaN(Number(raw))) {
                dictObj[key] = Number(raw);
              } else {
                dictObj[key] = raw;
              }
              syncDict();
            });

            const deleteBtn = document.createElement('button');
            deleteBtn.textContent = '×';
            deleteBtn.style.padding = '2px 8px';
            deleteBtn.style.cursor = 'pointer';
            deleteBtn.addEventListener('click', () => {
              delete dictObj[key];
              renderDictRows();
              syncDict();
            });

            row.appendChild(keyInput);
            row.appendChild(valueInput);
            row.appendChild(deleteBtn);
            wrapper.appendChild(row);
            rows.push(row);
          });
        };

        // Hidden textarea to store the JSON object
        input = document.createElement('textarea');
        input.style.display = 'none';
        input.dataset.json = '1';
        input.dataset.dictWidget = '1';
        input.value = JSON.stringify(dictObj);

        const syncDict = () => {
          input.value = JSON.stringify(dictObj);
          checkPropsChanges();
        };

        // Add button
        const addBtn = document.createElement('button');
        addBtn.textContent = '+ Add';
        addBtn.style.padding = '4px 8px';
        addBtn.style.cursor = 'pointer';
        addBtn.addEventListener('click', () => {
          const newKey = `key_${Object.keys(dictObj).length}`;
          dictObj[newKey] = '';
          renderDictRows();
          syncDict();
        });

        renderDictRows();
        wrapper.appendChild(addBtn);

        tdVal.appendChild(wrapper);
        tdVal.appendChild(input);
        input.dataset.jsonKey = p.json_key;
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
        return;
      } else if (p.domain) {
        input = document.createElement('select');
        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = '';
        input.appendChild(empty);
        (p.domain_items || []).forEach(di => {
          const opt = document.createElement('option');
          opt.value = di.value;
          opt.textContent = di.label;
          input.appendChild(opt);
        });
        input.value = current.value_string ?? '';
      } else if (p.data_type === 'bool') {
        input = document.createElement('input');
        input.type = 'checkbox';
        if (current.value_bool === true) {
          input.checked = true;
          input.indeterminate = false;
          input.dataset.boolState = 'true';
        } else if (current.value_bool === false) {
          input.checked = false;
          input.indeterminate = false;
          input.dataset.boolState = 'false';
        } else {
          input.checked = false;
          input.indeterminate = true;
          input.dataset.boolState = 'null';
        }
        input.dataset.checkbox = '1';
        input.addEventListener('click', (e) => {
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
          checkPropsChanges();
        });
      } else if (p.data_type === 'color') {
        // Color picker + hex text input side by side
        const wrapper = document.createElement('div');
        wrapper.style.display = 'flex';
        wrapper.style.alignItems = 'center';
        wrapper.style.gap = '6px';

        const colorPicker = document.createElement('input');
        colorPicker.type = 'color';
        colorPicker.value = current.value_string || '#000000';
        colorPicker.style.width = '40px';
        colorPicker.style.height = '28px';
        colorPicker.style.padding = '0';
        colorPicker.style.border = 'none';
        colorPicker.style.cursor = 'pointer';

        const hexInput = document.createElement('input');
        hexInput.type = 'text';
        hexInput.value = current.value_string || '';
        hexInput.placeholder = '#rrggbb';
        hexInput.style.flex = '1';
        hexInput.maxLength = 7;

        colorPicker.addEventListener('input', () => {
          hexInput.value = colorPicker.value;
          checkPropsChanges();
        });
        hexInput.addEventListener('input', () => {
          if (/^#[0-9a-fA-F]{6}$/.test(hexInput.value)) {
            colorPicker.value = hexInput.value;
          }
          checkPropsChanges();
        });

        input = hexInput;
        wrapper.appendChild(colorPicker);
        wrapper.appendChild(hexInput);
        tdVal.appendChild(wrapper);
        input.dataset.jsonKey = p.json_key;
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
        return;
      } else if (p.data_type === 'list_string' || p.data_type === 'list_int') {
        // Editable list: one item per row with add/remove buttons
        const isInt = p.data_type === 'list_int';
        const currentList = Array.isArray(current.value_json) ? current.value_json : [];
        const items = currentList.map(v => String(v));

        const wrapper = document.createElement('div');
        wrapper.style.display = 'flex';
        wrapper.style.flexDirection = 'column';
        wrapper.style.gap = '4px';

        input = document.createElement('textarea');
        input.style.display = 'none';
        input.dataset.json = '1';

        const syncList = () => {
          const rowInputs = wrapper.querySelectorAll('input[data-list-item]');
          const vals = Array.from(rowInputs).map(r => isInt ? (parseInt(r.value, 10) || 0) : r.value).filter(v => !isInt || !isNaN(v));
          input.value = vals.length ? JSON.stringify(vals) : '';
          checkPropsChanges();
        };

        const addRow = (val = '') => {
          const row = document.createElement('div');
          row.style.display = 'flex';
          row.style.gap = '4px';

          const itemInput = document.createElement('input');
          itemInput.type = isInt ? 'number' : 'text';
          itemInput.value = val;
          itemInput.style.flex = '1';
          itemInput.dataset.listItem = '1';
          itemInput.addEventListener('input', syncList);

          const rmBtn = document.createElement('button');
          rmBtn.textContent = '✕';
          rmBtn.style.cssText = 'padding:2px 6px;cursor:pointer;font-size:0.8em;';
          rmBtn.addEventListener('click', () => {
            wrapper.removeChild(row);
            syncList();
          });

          row.appendChild(itemInput);
          row.appendChild(rmBtn);
          wrapper.insertBefore(row, addBtn);
        };

        const addBtn = document.createElement('button');
        addBtn.textContent = '+ Add item';
        addBtn.style.cssText = 'padding:3px 8px;cursor:pointer;font-size:0.85em;align-self:flex-start;';
        addBtn.addEventListener('click', () => { addRow(); syncList(); });
        wrapper.appendChild(addBtn);

        items.forEach(v => addRow(v));
        syncList();

        tdVal.appendChild(wrapper);
        tdVal.appendChild(input);
        input.dataset.jsonKey = p.json_key;
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
        return;
      } else if (p.data_type === 'date') {
        input = document.createElement('input');
        input.type = 'date';
        input.value = current.value_string ?? '';
      } else if (p.data_type === 'int') {
        input = document.createElement('input');
        input.type = 'number';
        input.step = '1';
        input.dataset.numericType = 'int';
        input.value = current.value_number ?? '';
      } else if (p.data_type === 'float' || p.data_type === 'number') {
        input = document.createElement('input');
        input.type = 'number';
        input.step = 'any';
        input.dataset.numericType = 'float';
        input.value = current.value_number ?? '';
      } else if ((p.json_key === 'padding' || p.json_key === 'margin') && p.data_type === 'json') {
        // 4-value editor: [top, right, bottom, left]
        const raw = current.value_json;
        const vals = Array.isArray(raw) ? raw : [null, null, null, null];
        const wrapper = document.createElement('table');
        const sides = ['top', 'right', 'bottom', 'left'];
        sides.forEach((side, i) => {
          const lbl = document.createElement('label');
          lbl.style.cssText = 'font-size:0.8em; color:var(--body-quiet-color,#666); display:flex; flex-direction:column; gap:2px;';
          lbl.textContent = side;
          const n = document.createElement('input');
          n.type = 'number';
          n.step = 'any';
          n.value = vals[i] != null ? vals[i] : '';
          n.style.width = '100%';
          n.dataset.edgeSide = side;
          n.addEventListener('input', checkPropsChanges);
          n.addEventListener('change', checkPropsChanges);
          lbl.appendChild(n);
          wrapper.appendChild(lbl);
        });
        // Hidden textarea that holds the JSON array — read by checkPropsChanges / save
        input = document.createElement('textarea');
        input.style.display = 'none';
        input.dataset.json = '1';
        input.dataset.edgeWidget = '1';
        input.value = raw != null ? JSON.stringify(raw) : '';
        const syncEdge = () => {
          const arr = sides.map(s => {
            const n = wrapper.querySelector(`[data-edge-side="${s}"]`);
            return n.value !== '' ? parseFloat(n.value) : null;
          });
          const allNull = arr.every(v => v === null);
          input.value = allNull ? '' : JSON.stringify(arr);
          checkPropsChanges();
        };
        // Table layout: predictable alignment for top/left+right/bottom
        wrapper.style.cssText = 'border-collapse:collapse; width:260px;';
        wrapper.innerHTML = '';
        const LBL = 'font-size:0.78em; color:var(--body-quiet-color,#666); padding:2px 4px 2px 0; white-space:nowrap; vertical-align:middle; background:transparent;';
        const INP = 'text-align:center; width:100%; box-sizing:border-box;';
        const TR  = 'background:transparent !important;';
        const makeEdgeRow = (side, idx, colspan) => {
          const tr = document.createElement('tr');
          tr.style.cssText = TR;
          const tdl = document.createElement('td'); tdl.style.cssText = LBL; tdl.textContent = side;
          const tdi = document.createElement('td'); tdi.colSpan = colspan;
          const n = document.createElement('input'); n.type='number'; n.step='any';
          n.value = vals[idx] != null ? vals[idx] : '';
          n.style.cssText = INP; n.dataset.edgeSide = side;
          n.addEventListener('input', syncEdge); n.addEventListener('change', syncEdge);
          tdi.appendChild(n); tr.appendChild(tdl); tr.appendChild(tdi);
          return tr;
        };
        // top row
        wrapper.appendChild(makeEdgeRow('top', 0, 3));
        // middle row: left | right
        const midTr = document.createElement('tr');
        midTr.style.cssText = TR;
        const tdll = document.createElement('td'); tdll.style.cssText = LBL; tdll.textContent = 'left';
        const tdli = document.createElement('td');
        const nLeft = document.createElement('input'); nLeft.type='number'; nLeft.step='any';
        nLeft.value = vals[3] != null ? vals[3] : ''; nLeft.style.cssText = INP; nLeft.dataset.edgeSide = 'left';
        nLeft.addEventListener('input', syncEdge); nLeft.addEventListener('change', syncEdge);
        tdli.appendChild(nLeft);
        const tdrl = document.createElement('td'); tdrl.style.cssText = LBL; tdrl.textContent = 'right';
        const tdri = document.createElement('td');
        const nRight = document.createElement('input'); nRight.type='number'; nRight.step='any';
        nRight.value = vals[1] != null ? vals[1] : ''; nRight.style.cssText = INP; nRight.dataset.edgeSide = 'right';
        nRight.addEventListener('input', syncEdge); nRight.addEventListener('change', syncEdge);
        tdri.appendChild(nRight);
        midTr.appendChild(tdll); midTr.appendChild(tdli); midTr.appendChild(tdrl); midTr.appendChild(tdri);
        wrapper.appendChild(midTr);
        // bottom row
        wrapper.appendChild(makeEdgeRow('bottom', 2, 3));
        tdVal.appendChild(wrapper);
        tdVal.appendChild(input);
        input.dataset.jsonKey = p.json_key;
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
        return;
      } else if (p.json_key === 'position' && p.data_type === 'json') {
        // Key-value editor: optional top/right/bottom/left numeric fields
        const raw = current.value_json;
        const posMap = (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw : {};
        const posSides = ['top', 'right', 'bottom', 'left'];
        const posWrapper = document.createElement('table');
        const posHidden = document.createElement('textarea');
        posHidden.style.display = 'none';
        posHidden.dataset.json = '1';
        posHidden.dataset.posWidget = '1';
        posHidden.value = raw != null ? JSON.stringify(raw) : '';
        const syncPos = () => {
          const obj = {};
          posSides.forEach(s => {
            const n = posWrapper.querySelector(`[data-pos-value="${s}"]`);
            if (n && n.value !== '') obj[s] = parseFloat(n.value);
          });
          posHidden.value = Object.keys(obj).length ? JSON.stringify(obj) : '';
          checkPropsChanges();
        };
        // Table layout for position widget
        posWrapper.style.cssText = 'border-collapse:collapse; width:260px;';
        const PLBL = 'font-size:0.78em; color:var(--body-quiet-color,#666); padding:2px 4px 2px 0; white-space:nowrap; vertical-align:middle; background:transparent;';
        const PINP = 'text-align:center; width:100%; box-sizing:border-box;';
        const PTR  = 'background:transparent !important;';
        const makePosRow = (side, colspan) => {
          const tr = document.createElement('tr');
          tr.style.cssText = PTR;
          const tdl = document.createElement('td'); tdl.style.cssText = PLBL; tdl.textContent = side;
          const tdi = document.createElement('td'); tdi.colSpan = colspan;
          const n = document.createElement('input'); n.type='number'; n.step='any';
          n.value = posMap[side] != null ? posMap[side] : '';
          n.style.cssText = PINP; n.dataset.posValue = side;
          n.addEventListener('input', syncPos); n.addEventListener('change', syncPos);
          tdi.appendChild(n); tr.appendChild(tdl); tr.appendChild(tdi);
          return tr;
        };
        posWrapper.appendChild(makePosRow('top', 3));
        const posMidTr = document.createElement('tr');
        posMidTr.style.cssText = PTR;
        const ptdll = document.createElement('td'); ptdll.style.cssText = PLBL; ptdll.textContent = 'left';
        const ptdli = document.createElement('td');
        const pnLeft = document.createElement('input'); pnLeft.type='number'; pnLeft.step='any';
        pnLeft.value = posMap['left'] != null ? posMap['left'] : ''; pnLeft.style.cssText = PINP; pnLeft.dataset.posValue = 'left';
        pnLeft.addEventListener('input', syncPos); pnLeft.addEventListener('change', syncPos); ptdli.appendChild(pnLeft);
        const ptdrl = document.createElement('td'); ptdrl.style.cssText = PLBL; ptdrl.textContent = 'right';
        const ptdri = document.createElement('td');
        const pnRight = document.createElement('input'); pnRight.type='number'; pnRight.step='any';
        pnRight.value = posMap['right'] != null ? posMap['right'] : ''; pnRight.style.cssText = PINP; pnRight.dataset.posValue = 'right';
        pnRight.addEventListener('input', syncPos); pnRight.addEventListener('change', syncPos); ptdri.appendChild(pnRight);
        posMidTr.appendChild(ptdll); posMidTr.appendChild(ptdli); posMidTr.appendChild(ptdrl); posMidTr.appendChild(ptdri);
        posWrapper.appendChild(posMidTr);
        posWrapper.appendChild(makePosRow('bottom', 3));
        input = posHidden;
        tdVal.appendChild(posWrapper);
        tdVal.appendChild(posHidden);
        input.dataset.jsonKey = p.json_key;
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
        return;
      } else if (p.data_type === 'json' || p.data_type === 'list_string') {
        input = document.createElement('textarea');
        input.value = current.value_json != null ? JSON.stringify(current.value_json) : '';
        input.dataset.json = '1';
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.value = current.value_string ?? '';
      }

      input.dataset.jsonKey = p.json_key;
      // Add listener to detect changes in properties
      input.addEventListener('input', checkPropsChanges);
      input.addEventListener('change', checkPropsChanges);
      tdVal.appendChild(input);
      tr.appendChild(tdVal);

      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    return table;
  }

  function renderProps(props) {
    const container = document.getElementById('props');
    container.innerHTML = '';

    // Variant grouping: universal (NULL) vs type-specific
    const hasVariantGroups = props.some(p => p.variant_key !== null);
    const variantLabels = { null: 'Common Properties' };

    const hasGroups = hasVariantGroups;
    if (!hasGroups) {
      container.appendChild(buildPropsTable(props));
      return;
    }

    // Group by variant (universal vs type-specific)
    const variantGroups = {};
    props.forEach(p => {
      const v = p.variant_key === null ? 'universal' : 'variant';
      (variantGroups[v] = variantGroups[v] || []).push(p);
    });

    // Render universal props first, then type-specific
    ['universal', 'variant'].forEach(v => {
      if (!variantGroups[v] || variantGroups[v].length === 0) return;

      const variantSection = document.createElement('div');
      variantSection.style.marginBottom = '16px';

      if (v === 'universal' && hasVariantGroups) {
        const variantHeading = document.createElement('div');
        variantHeading.textContent = variantLabels.null;
        variantHeading.style.cssText = 'font-weight:bold; font-size:0.9em; text-transform:uppercase; letter-spacing:0.05em; color:var(--body-quiet-color,#666); padding:8px 0 4px 0; border-bottom:1px solid var(--hairline-color,#ddd); margin-bottom:8px;';
        variantSection.appendChild(variantHeading);
      } else if (v === 'variant') {
        // Get the variant_key from the first property in this group
        const firstProp = variantGroups[v][0];
        if (firstProp && firstProp.variant_key) {
          const variantHeading = document.createElement('div');
          // Capitalize first letter, lowercase rest
          const variantLabel = firstProp.variant_key.charAt(0).toUpperCase() + firstProp.variant_key.slice(1).toLowerCase();
          variantHeading.textContent = `${variantLabel} Properties`;
          variantHeading.style.cssText = 'font-weight:bold; font-size:0.9em; text-transform:uppercase; letter-spacing:0.05em; color:var(--body-quiet-color,#666); padding:8px 0 4px 0; border-bottom:1px solid var(--hairline-color,#ddd); margin-bottom:8px;';
          variantSection.appendChild(variantHeading);
        }
      }

      variantSection.appendChild(buildPropsTable(variantGroups[v]));
      container.appendChild(variantSection);
    });
  }

  async function saveKey() {
    if (!currentSelectedNodeId) return;
    const name = document.getElementById('node_name').value;
    await requestJson(apiNode(currentSelectedNodeId), { method: 'PATCH', body: JSON.stringify({ name }) });
    // Update name in tree without reloading entire tree
    const nodeRow = document.querySelector(`[data-node-id="${currentSelectedNodeId}"]`);
    if (nodeRow) {
      // Get current node_type from node text (format: "name (node_type)")
      const currentText = nodeRow.textContent;
      const nodeType = currentText.match(/\(([^)]+)\)$/)?.[1] || 'unknown';
      nodeRow.textContent = `${name} (${nodeType})`;
      document.getElementById('sel_title').textContent = `${name} (${nodeType})`;
    }
    // Update original name and disable button
    document.getElementById('node_name').dataset.originalName = name;
    document.getElementById('btn_save_key').disabled = true;
    // Update form JSON
    await updateNodeJson();
  }

  async function saveProps() {
    if (!currentSelectedNodeId) return;

    const propsContainer = document.getElementById('props');
    const nodeType = propsContainer.dataset.nodeType;

    let updates = {};

    // Generic handling for all node types
    const elements = document.querySelectorAll('#props [data-json-key]');
    for (const el of elements) {
        const jsonKey = el.dataset.jsonKey;

        // For checkboxes, always use boolState
        if (el.dataset.checkbox === '1') {
          const boolState = el.dataset.boolState || (el.checked ? 'true' : 'false');
          if (boolState === 'true') {
            updates[jsonKey] = true;
          } else if (boolState === 'false') {
            updates[jsonKey] = false;
          } else {
            updates[jsonKey] = null;
          }
          continue;
        }

        let v = el.value;
        if (el.dataset.json === '1') {
          v = v.trim();
          if (v === '') {
            updates[jsonKey] = null;
          } else {
            try {
              updates[jsonKey] = JSON.parse(v);
            } catch (e) {
              throw new Error(`Invalid JSON in ${jsonKey}`);
            }
          }
          continue;
        }
        if (el.dataset.numericType) {
          if (v === '' || v === null) {
            updates[jsonKey] = null;
          } else {
            updates[jsonKey] = el.dataset.numericType === 'int' ? parseInt(v, 10) : parseFloat(v);
          }
          continue;
        }
        updates[jsonKey] = v;
    }

    await requestJson(apiProps(currentSelectedNodeId), { method: 'PATCH', body: JSON.stringify({ properties: updates }) });
    // Update original values after saving
    const propsData = await requestJson(apiProps(currentSelectedNodeId), { method: 'GET' });
    document.getElementById('props').dataset.originalProps = JSON.stringify(propsData.properties || []);
    document.getElementById('btn_save_props').disabled = true;
    await selectNode(currentSelectedNodeId);
    // Update form JSON
    await updateNodeJson();
  }

  async function createChild() {
    if (!currentSelectedNodeId) return;
    const node_type = document.getElementById('create_node_type').value;
    const nameInput = document.getElementById('create_key').value;
    const variantKey = document.getElementById('variant_select').value || null;
    const payload = { parent_id: currentSelectedNodeId, node_type };
    if (nameInput && nameInput.trim()) {
      payload.name = nameInput.trim();
    }
    if (variantKey) {
      payload.variant_key = variantKey;
    }
    const res = await requestJson(apiCreate, { method: 'POST', body: JSON.stringify(payload) });
    document.getElementById('create_key').value = '';
    document.getElementById('variant_select').value = '';
    if (res && res.node_id) {
      await loadTree();
      await new Promise(resolve => setTimeout(resolve, 100));
      await selectNode(String(res.node_id));
    } else {
      await loadTree();
    }
  }

  async function deleteSubtree() {
    if (!currentSelectedNodeId) return;
    if (!confirm('Are you sure you want to delete the entire subtree?')) return;

    // Check if root node is being deleted
    const isRootNode = String(currentSelectedNodeId) === String(currentRootId);

    await requestJson(apiDelete(currentSelectedNodeId), { method: 'DELETE' });

    if (isRootNode) {
      // Redirect to schemas list
      window.location.href = '/admin/schemas/schema/';
    } else {
      await loadTree();
    }
  }

  document.getElementById('load_tree').onclick = async () => {
    try {
      await loadTree();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_refresh').onclick = async () => {
    try {
      await loadTree();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_save_key').onclick = async () => {
    try {
      await saveKey();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_save_props').onclick = async () => {
    try {
      await saveProps();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_create').onclick = async () => {
    try {
      await createChild();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_delete').onclick = async () => {
    try {
      await deleteSubtree();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_up').onclick = async () => {
    if (!currentSelectedNodeId) return;
    try {
      await requestJson(apiReorder, { method: 'POST', body: JSON.stringify({ node_id: currentSelectedNodeId, direction: 'up' }) });
      const savedNodeId = currentSelectedNodeId;
      await loadTree();
      await selectNode(savedNodeId);
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_down').onclick = async () => {
    if (!currentSelectedNodeId) return;
    try {
      await requestJson(apiReorder, { method: 'POST', body: JSON.stringify({ node_id: currentSelectedNodeId, direction: 'down' }) });
      const savedNodeId = currentSelectedNodeId;
      await loadTree();
      await selectNode(savedNodeId);
    } catch (e) {
      alert(e.message);
    }
  };

  // Enable/disable save name button when input changes
  document.getElementById('node_name').addEventListener('input', (e) => {
    const originalName = e.target.dataset.originalName || '';
    const currentName = e.target.value;
    document.getElementById('btn_save_key').disabled = currentName === originalName;
  });

  // Resizable panels
  const resizer = document.getElementById('resizer');
  const leftPanel = document.querySelector('.s7-panel-left');
  const rightPanel = document.querySelector('.s7-panel-right');
  let isResizing = false;

  resizer.addEventListener('mousedown', (e) => {
    isResizing = true;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', (e) => {
    if (!isResizing) return;
    const containerRect = document.querySelector('.s7-editor').getBoundingClientRect();
    const newLeftWidth = e.clientX - containerRect.left;
    const containerWidth = containerRect.width;

    if (newLeftWidth > 200 && newLeftWidth < containerWidth - 300) {
      leftPanel.style.flex = '0 0 ' + newLeftWidth + 'px';
      rightPanel.style.flex = '1';
    }
  });

  document.addEventListener('mouseup', () => {
    if (isResizing) {
      isResizing = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
  });

  (async () => {
    const qs = new URLSearchParams(window.location.search || '');
    const nodeId = (qs.get('node_id') || '').trim();
    const key = (qs.get('key') || '').trim();
    const version = (qs.get('version') || '').trim();

    if (nodeId) {
      document.getElementById('node_id').value = nodeId;
    } else {
      if (key) document.getElementById('key').value = key;
      if (version) document.getElementById('version').value = version;
    }

    if (nodeId || (key && version)) {
      try {
        await loadTree();
      } catch (e) {
        alert(e.message);
      }
    }
  })();
