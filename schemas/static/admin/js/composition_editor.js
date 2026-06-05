const baseUrl = window.location.pathname.replace(/\/editor\/?$/, '');
  const currentScope = (typeof window.currentScope !== 'undefined') ? window.currentScope : '';
  const scopeParam = currentScope ? `?scope=${encodeURIComponent(currentScope)}` : '';
  const apiGraph = baseUrl + '/editor/api/graph/' + scopeParam;
  const apiComp = (id) => baseUrl + `/editor/api/composition/${id}/`;
  const apiCreate = baseUrl + '/editor/api/create/';
  const apiDelete = (id) => baseUrl + `/editor/api/delete/${id}/`;
  const treeMode = (typeof window.treeMode !== 'undefined') ? window.treeMode : 'compositions';

  // Alert messages
  const MSG_ERROR_LOAD_COMPOSITION = 'Error: Could not load composition';

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

  let nodeTypes = [];
  let comps = [];
  let selectedCompId = null;

  function byId(list, id) {
    return list.find(x => String(x.id) === String(id));
  }

  function buildTree() {
    // Root = is_root OR does not appear as any child_type in compositions.
    const hasParent = new Set(comps.map(c => String(c.child_type_id)));
    const roots = nodeTypes
      .filter(nt => nt.is_root || !hasParent.has(String(nt.id)))
      .map(nt => String(nt.id));
    const rootSet = new Set(roots);
    if (rootSet.size === 0 && nodeTypes.length) rootSet.add(String(nodeTypes[0].id));
    return [...rootSet];
  }

  function renderTree() {
    const container = document.getElementById('tree');
    container.innerHTML = '';

    const childrenByParent = new Map();
    comps.forEach(c => {
      const p = String(c.parent_type_id);
      childrenByParent.set(p, childrenByParent.get(p) || []);
      childrenByParent.get(p).push(c);
    });
    childrenByParent.forEach(arr => arr.sort((a,b) => String(a.child_type_id).localeCompare(String(b.child_type_id))));

    const roots = buildTree();

    // Build children list for a node type, returns a <ul> or null
    function buildChildrenUl(ntId, visitedSet) {
      const edges = childrenByParent.get(String(ntId)) || [];
      if (edges.length === 0) return null;

      const ul = document.createElement('ul');
      ul.classList.add('s7-tree-children');

      edges.forEach((edge, idx) => {
        const childNt = byId(nodeTypes, edge.child_type_id);
        if (!childNt) return;

        const isLast = idx === edges.length - 1;
        const li = document.createElement('li');
        if (isLast) li.classList.add('s7-last');

        const row = document.createElement('div');
        row.classList.add('s7-tree-row');
        row.dataset.compId = edge.id;
        if (String(edge.id) === String(selectedCompId)) row.classList.add('s7-selected');

        const nameSpan = document.createElement('span');
        nameSpan.textContent = childNt.name;
        row.appendChild(nameSpan);

        if (edge.collection_key) {
          const keySpan = document.createElement('span');
          keySpan.textContent = `(${edge.collection_key})`;
          keySpan.style.color = '#888';
          keySpan.style.fontSize = '0.85em';
          row.appendChild(keySpan);
        }

        row.onclick = () => selectComposition(String(edge.id));
        li.appendChild(row);

        // Recurse only if not already visited (cycle guard)
        if (!visitedSet.has(String(childNt.id))) {
          visitedSet.add(String(childNt.id));
          const grandChildren = buildChildrenUl(String(childNt.id), visitedSet);
          if (grandChildren) li.appendChild(grandChildren);
          visitedSet.delete(String(childNt.id));
        }

        ul.appendChild(li);
      });

      return ul;
    }

    roots.forEach(rid => {
      const nt = byId(nodeTypes, rid);
      if (!nt) return;

      // Root label (non-clickable)
      const rootDiv = document.createElement('div');
      rootDiv.style.padding = '6px 8px';
      rootDiv.style.fontWeight = 'bold';
      rootDiv.style.fontSize = '1.05em';
      rootDiv.textContent = nt.name;
      container.appendChild(rootDiv);

      const visitedSet = new Set([String(rid)]);
      const childrenUl = buildChildrenUl(rid, visitedSet);
      if (childrenUl) container.appendChild(childrenUl);
    });
  }

  function fillSelect(selectId) {
    const sel = document.getElementById(selectId);
    sel.innerHTML = '';
    nodeTypes.forEach(nt => {
      const opt = document.createElement('option');
      opt.value = nt.id;
      opt.textContent = `${nt.label} (${nt.name})`;
      sel.appendChild(opt);
    });
  }

  async function loadGraph() {
    const data = await requestJson(apiGraph, { method: 'GET' });
    nodeTypes = data.node_types || [];
    comps = data.compositions || [];
    fillSelect('parent_type');
    fillSelect('child_type');
    renderTree();
    clearProps();
  }

  function clearProps() {
    selectedCompId = null;
    document.getElementById('sel_title').textContent = '(select a relationship)';
    document.getElementById('collection_key').value = '';
    document.getElementById('min_children').value = '';
    document.getElementById('max_children').value = '';
    document.getElementById('collection_key').disabled = true;
    document.getElementById('min_children').disabled = true;
    document.getElementById('max_children').disabled = true;
    document.getElementById('btn_save').disabled = true;
    document.getElementById('btn_delete').disabled = true;
  }

  async function selectComposition(compId) {
    selectedCompId = String(compId);
    const data = await requestJson(apiComp(compId), { method: 'GET' });
    
    if (!data.parent_type || !data.child_type) {
      console.error('Invalid composition data:', data);
      alert(MSG_ERROR_LOAD_COMPOSITION);
      clearProps();
      return;
    }
    
    document.getElementById('sel_title').textContent = `${data.parent_type.name} -> ${data.child_type.name}`;

    document.getElementById('collection_key').value = data.collection_key || '';
    document.getElementById('min_children').value = data.min_children ?? '';
    document.getElementById('max_children').value = data.max_children ?? '';

    document.getElementById('collection_key').disabled = false;
    document.getElementById('min_children').disabled = false;
    document.getElementById('max_children').disabled = false;
    document.getElementById('btn_save').disabled = false;
    document.getElementById('btn_delete').disabled = false;

    renderTree();
  }

  async function saveComposition() {
    if (!selectedCompId) return;
    const payload = {
      collection_key: document.getElementById('collection_key').value,
      min_children: document.getElementById('min_children').value,
      max_children: document.getElementById('max_children').value,
    };
    await requestJson(apiComp(selectedCompId), { method: 'PATCH', body: JSON.stringify(payload) });
    await loadGraph();
    
    // Verify the composition still exists before selecting it
    const comp = comps.find(c => String(c.id) === String(selectedCompId));
    if (comp) {
      await selectComposition(selectedCompId);
    } else {
      clearProps();
    }
  }

  async function deleteComposition() {
    if (!selectedCompId) return;
    if (!confirm('Delete relationship?')) return;
    await requestJson(apiDelete(selectedCompId), { method: 'DELETE' });
    await loadGraph();
  }

  async function createComposition() {
    const parent_type_id = document.getElementById('parent_type').value;
    const child_type_id = document.getElementById('child_type').value;
    const res = await requestJson(apiCreate, { method: 'POST', body: JSON.stringify({ parent_type_id, child_type_id }) });
    await loadGraph();
    if (res && res.id) {
      await selectComposition(res.id);
    }
  }

  document.getElementById('btn_refresh').onclick = async () => {
    try {
      await loadGraph();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_save').onclick = async () => {
    try {
      await saveComposition();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_delete').onclick = async () => {
    try {
      await deleteComposition();
    } catch (e) {
      alert(e.message);
    }
  };

  document.getElementById('btn_create').onclick = async () => {
    try {
      await createComposition();
    } catch (e) {
      alert(e.message);
    }
  };

  loadGraph();
