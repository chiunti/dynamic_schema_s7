const _cfg = document.getElementById('page-config')?.dataset || {};
const apiVariants = _cfg.urlVariants || '';
const currentScope = new URLSearchParams(window.location.search).get('scope') || '';
const apiAttrsByVariant = (vk) => {
  const baseUrl = (_cfg.urlAttributesByVariant || '').replace('__VK__', encodeURIComponent(vk));
  // Add scope param to filter attrs by node type
  if (currentScope) {
    const sep = baseUrl.includes('?') ? '&' : '?';
    return baseUrl + sep + 'scope=' + encodeURIComponent(currentScope);
  }
  return baseUrl;
};

function getCookie(name) {
  const v = `; ${document.cookie}`;
  const p = v.split(`; ${name}=`);
  if (p.length === 2) return p.pop().split(';').shift();
}

async function req(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const csrf = getCookie('csrftoken');
  if (csrf) headers['X-CSRFToken'] = csrf;
  const resp = await fetch(url, { ...options, headers });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

let selectedVariantKey = null;

async function loadVariants() {
  const data = await req(apiVariants);
  const list = document.getElementById('variant_list');
  list.innerHTML = '';
  (data.variants || []).forEach(v => {
    const li = document.createElement('li');
    li.className = 's7-comp-item';
    const ntName = v['node_type__name'] || '';
    // Show node_type in parentheses to distinguish (e.g., "spacer (field)" vs "spacer (widget)")
    li.textContent = ntName ? `${v.variant_key} (${ntName})` : v.variant_key;
    li.title = `node_type: ${ntName}`;
    li.dataset.variantKey = v.variant_key;
    li.addEventListener('click', () => selectVariant(v.variant_key, li));
    list.appendChild(li);
  });
}

async function selectVariant(variantKey, liEl) {
  selectedVariantKey = variantKey;
  document.querySelectorAll('#variant_list .s7-comp-item').forEach(el => el.classList.remove('active'));
  if (liEl) liEl.classList.add('active');

  document.getElementById('selected_variant_label').textContent = variantKey;
  const container = document.getElementById('attrs_container');
  container.innerHTML = '<p class="s7-muted">Loading...</p>';

  const data = await req(apiAttrsByVariant(variantKey));
  const commonAttrs = data.common_attrs || [];
  const catalogAttrs = data.catalog_attrs || [];

  if (commonAttrs.length === 0 && catalogAttrs.length === 0) {
    container.innerHTML = '<p class="s7-muted">No attribute definitions found for this variant\'s node type(s).</p>';
    return;
  }

  container.innerHTML = '';

  // Add general "Add Property" button at the top
  const btnAdd = document.createElement('button');
  btnAdd.className = 'button';
  btnAdd.textContent = '+ Add Property';
  btnAdd.style.marginBottom = '16px';
  btnAdd.addEventListener('click', () => showAddPropertyModal(variantKey));
  container.appendChild(btnAdd);

  // Render common attributes (no checkbox, always present)
  if (commonAttrs.length > 0) {
    const commonSection = document.createElement('div');
    commonSection.className = 's7-props-section';

    const commonHeading = document.createElement('div');
    commonHeading.className = 's7-section-heading';
    commonHeading.textContent = 'Common Properties (always present)';
    commonSection.appendChild(commonHeading);

    const commonTable = document.createElement('table');
    commonTable.className = 's7-table';
    commonTable.innerHTML = `
      <thead>
        <tr>
          <th>json_key</th>
          <th>name</th>
          <th>required</th>
          <th>actions</th>
        </tr>
      </thead>
    `;
    const commonTbody = document.createElement('tbody');
    commonAttrs.forEach(a => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><code>${a.json_key}</code></td>
        <td>${a.name || ''}</td>
        <td>${a.is_required ? '✓' : ''}</td>
        <td>
          <button class="button" data-id="${a.id}" data-action="make-specific" style="font-size:0.75em;padding:2px 8px;">Make specific</button>
          <button class="button" data-id="${a.id}" data-action="delete" style="font-size:0.75em;padding:2px 8px;margin-left:4px;">Delete</button>
        </td>
      `;
      commonTbody.appendChild(tr);
    });
    commonTable.appendChild(commonTbody);
    commonSection.appendChild(commonTable);
    container.appendChild(commonSection);

    // Add event listeners for common property actions
    commonSection.querySelectorAll('button[data-action]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const id = e.target.dataset.id;
        const action = e.target.dataset.action;
        if (action === 'make-specific') {
          await makePropertySpecific(id);
        } else if (action === 'delete') {
          if (confirm('Are you sure you want to delete this property?')) {
            await deleteProperty(id);
          }
        }
      });
    });
  }

  // Render catalog attributes (with checkbox)
  if (catalogAttrs.length > 0) {
    const variantSection = document.createElement('div');
    variantSection.className = 's7-props-section';

    const variantHeaderRow = document.createElement('div');
    variantHeaderRow.style.display = 'flex';
    variantHeaderRow.style.justifyContent = 'space-between';
    variantHeaderRow.style.alignItems = 'center';
    variantHeaderRow.style.marginBottom = '8px';

    const variantHeading = document.createElement('div');
    variantHeading.className = 's7-section-heading';
    variantHeading.style.marginBottom = '0';
    const variantLabel = variantKey.charAt(0).toUpperCase() + variantKey.slice(1).toLowerCase();
    variantHeading.textContent = `${variantLabel} Properties`;
    variantHeaderRow.appendChild(variantHeading);

    variantSection.appendChild(variantHeaderRow);

    const variantTable = document.createElement('table');
    variantTable.className = 's7-table';
    variantTable.innerHTML = `
      <thead>
        <tr>
          <th style="width:2em"></th>
          <th>json_key</th>
          <th>name</th>
          <th>required</th>
          <th>actions</th>
        </tr>
      </thead>
    `;
    const variantTbody = document.createElement('tbody');
    catalogAttrs.forEach(a => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><input type="checkbox" data-id="${a.id}" ${a.active ? 'checked' : ''}></td>
        <td><code>${a.json_key}</code></td>
        <td>${a.name || ''}</td>
        <td>${a.is_required ? '✓' : ''}</td>
        <td>
          <button class="button" data-id="${a.id}" data-action="make-common" style="font-size:0.75em;padding:2px 8px;">Make common</button>
        </td>
      `;
      variantTbody.appendChild(tr);
    });
    variantTable.appendChild(variantTbody);
    variantSection.appendChild(variantTable);
    container.appendChild(variantSection);

    // Add event listeners for catalog property actions
    variantSection.querySelectorAll('button[data-action="make-common"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const id = e.target.dataset.id;
        if (confirm('Are you sure you want to make this property common? It will be available to all variants.')) {
          await makePropertyCommon(id);
        }
      });
    });
  }

  const actions = document.createElement('div');
  actions.className = 's7-actions';
  actions.style.marginTop = '1em';
  const btnSave = document.createElement('button');
  btnSave.className = 'button';
  btnSave.textContent = 'Save';
  const statusMsg = document.createElement('span');
  statusMsg.style.marginLeft = '1em';
  btnSave.addEventListener('click', async () => {
    const selected_ids = [...container.querySelectorAll('input[type=checkbox]:checked')].map(cb => cb.dataset.id);
    statusMsg.textContent = 'Saving...';
    try {
      await req(apiAttrsByVariant(variantKey), {
        method: 'POST',
        body: JSON.stringify({ selected_ids }),
      });
      statusMsg.textContent = '✓ Saved';
      await selectVariant(variantKey, document.querySelector(`#variant_list .s7-comp-item.active`));
    } catch (e) {
      statusMsg.textContent = `Error: ${e.message}`;
    }
  });
  actions.appendChild(btnSave);
  actions.appendChild(statusMsg);
  container.appendChild(actions);
}

function showAddPropertyModal(variantKey) {
  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;';

  const content = document.createElement('div');
  content.style.cssText = 'background:white;padding:20px;border-radius:8px;min-width:400px;max-width:600px;';

  const title = document.createElement('h3');
  title.textContent = 'Add New Property';
  title.style.marginTop = '0';
  content.appendChild(title);

  const formContainer = document.createElement('div');
  formContainer.style.display = 'flex';
  formContainer.style.flexDirection = 'column';
  formContainer.style.gap = '12px';

  const jsonKeyRow = document.createElement('div');
  const jsonKeyInput = document.createElement('input');
  jsonKeyInput.type = 'text';
  jsonKeyInput.style.width = '100%';
  jsonKeyInput.style.padding = '4px';
  const jsonKeyLabel = document.createElement('label');
  jsonKeyLabel.textContent = 'json_key: ';
  jsonKeyLabel.appendChild(jsonKeyInput);
  jsonKeyRow.appendChild(jsonKeyLabel);
  formContainer.appendChild(jsonKeyRow);

  const nameRow = document.createElement('div');
  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.style.width = '100%';
  nameInput.style.padding = '4px';
  const nameLabel = document.createElement('label');
  nameLabel.textContent = 'name: ';
  nameLabel.appendChild(nameInput);
  nameRow.appendChild(nameLabel);
  formContainer.appendChild(nameRow);

  const requiredRow = document.createElement('div');
  const requiredCheckbox = document.createElement('input');
  requiredCheckbox.type = 'checkbox';
  const requiredLabel = document.createElement('label');
  requiredLabel.appendChild(requiredCheckbox);
  requiredLabel.appendChild(document.createTextNode(' Required'));
  requiredRow.appendChild(requiredLabel);
  formContainer.appendChild(requiredRow);

  const commonRow = document.createElement('div');
  const commonCheckbox = document.createElement('input');
  commonCheckbox.type = 'checkbox';
  const commonLabel = document.createElement('label');
  commonLabel.appendChild(commonCheckbox);
  commonLabel.appendChild(document.createTextNode(' Common property (always present)'));
  commonRow.appendChild(commonLabel);
  formContainer.appendChild(commonRow);

  content.appendChild(formContainer);

  const buttons = document.createElement('div');
  buttons.style.display = 'flex';
  buttons.style.justifyContent = 'flex-end';
  buttons.style.gap = '8px';
  buttons.style.marginTop = '16px';

  const btnCancel = document.createElement('button');
  btnCancel.className = 'button';
  btnCancel.textContent = 'Cancel';
  btnCancel.addEventListener('click', () => document.body.removeChild(modal));
  buttons.appendChild(btnCancel);

  const btnCreate = document.createElement('button');
  btnCreate.className = 'button';
  btnCreate.textContent = 'Create';
  btnCreate.addEventListener('click', async () => {
    const jsonKey = jsonKeyInput.value.trim();
    const name = nameInput.value.trim();
    const isRequired = requiredCheckbox.checked;
    const isCommon = commonCheckbox.checked;

    if (!jsonKey || !name) {
      alert('json_key and name are required');
      return;
    }

    try {
      await req('/admin/schemas/api/create-attr-def/', {
        method: 'POST',
        body: JSON.stringify({ variant_key: variantKey, json_key: jsonKey, name: name, is_required: isRequired, is_common: isCommon, add_to_catalog: !isCommon }),
      });
      document.body.removeChild(modal);
      await selectVariant(variantKey, document.querySelector(`#variant_list .s7-comp-item.active`));
    } catch (e) {
      alert(`Error: ${e.message}`);
    }
  });
  buttons.appendChild(btnCreate);

  content.appendChild(buttons);
  modal.appendChild(content);
  document.body.appendChild(modal);
}

async function makePropertyCommon(id) {
  try {
    await req('/admin/schemas/api/make-attr-common/', {
      method: 'POST',
      body: JSON.stringify({ id: id }),
    });
    await selectVariant(selectedVariantKey, document.querySelector(`#variant_list .s7-comp-item.active`));
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

async function makePropertySpecific(id) {
  try {
    await req('/admin/schemas/api/make-attr-specific/', {
      method: 'POST',
      body: JSON.stringify({ id: id }),
    });
    await selectVariant(selectedVariantKey, document.querySelector(`#variant_list .s7-comp-item.active`));
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

async function deleteProperty(id) {
  try {
    await req('/admin/schemas/api/delete-attr-def/', {
      method: 'POST',
      body: JSON.stringify({ id: id }),
    });
    await selectVariant(selectedVariantKey, document.querySelector(`#variant_list .s7-comp-item.active`));
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

loadVariants().catch(e => console.error('Failed to load variants:', e));
