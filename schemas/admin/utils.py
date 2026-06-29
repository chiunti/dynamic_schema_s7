"""
Utility functions for the schemas admin module.
"""
from collections import deque

from ..repositories.schema_repository import SchemaRepository


def build_node_line_map(json_text, root_id):
    """
    Build a map of { node_id: [startLine, endLine] } for every node in the subtree.

    Strategy:
    - Pass 1: build inventory of all { } object ranges in the JSON text.
    - Pass 2: for nodes whose UUID appears in the JSON (natural_uuid AttributeDef),
      find their enclosing object by searching for the UUID string.
    - Pass 3: for nodes whose UUID does NOT appear (embedded without id, e.g. nodes
      with collection_key IS NULL in NodeTypeComposition), locate them inside their
      parent's already-mapped range by finding objects not attributed to any named key
      that belongs to sibling collections.

    Args:
        json_text: JSON string to analyze
        root_id: UUID of the root node

    Returns:
        Dict mapping node_id (str) to [start_line, end_line] (0-indexed)
    """
    lines = json_text.split("\n")

    # --- Build object range inventory ---
    object_ranges = []  # [startLine, endLine]
    stack = []
    for li, line in enumerate(lines):
        for ch in line:
            if ch == "{":
                stack.append(li)
            elif ch == "}" and stack:
                object_ranges.append([stack.pop(), li])

    def find_enclosing_object(line_index, within=None):
        """Smallest object range containing line_index, optionally within a parent range."""
        best = None
        for r in object_ranges:
            if within and (r[0] < within[0] or r[1] > within[1]):
                continue
            if r[0] <= line_index <= r[1]:
                if best is None or (r[1] - r[0]) < (best[1] - best[0]):
                    best = r
        return best

    def find_object_starting_at(line_index, within=None):
        """Find an object that starts at line_index, optionally within a parent range."""
        for r in object_ranges:
            if within and (r[0] < within[0] or r[1] > within[1]):
                continue
            if r[0] == line_index:
                return r
        return None

    def objects_directly_inside(parent_range):
        """Objects whose immediate enclosing object is parent_range."""
        result = []
        for r in object_ranges:
            if r == parent_range:
                continue
            if r[0] < parent_range[0] or r[1] > parent_range[1]:
                continue
            # Check that no other object sits between r and parent_range
            has_intermediate = any(
                other != parent_range and other != r
                and other[0] <= r[0] and other[1] >= r[1]
                and other[0] >= parent_range[0] and other[1] <= parent_range[1]
                for other in object_ranges
            )
            if not has_intermediate:
                result.append(r)
        return sorted(result, key=lambda x: x[0])

    # --- Load full subtree with parent/type/key info ---
    repo = SchemaRepository()
    all_node_rows = repo.get_subtree_nodes_with_type(root_id)

    # Identify empty nodes (no attributes and no children) - these won't appear in JSON
    # due to s7_strip_empty_values, so they should not be highlighted
    node_ids = [row["id"] for row in all_node_rows]
    nodes_with_attrs = repo.get_nodes_with_attrs(node_ids)
    # Check for children by looking for nodes whose parent is in our node_ids
    nodes_with_children = repo.get_nodes_with_children(node_ids)
    empty_node_ids = set(node_ids) - nodes_with_attrs - nodes_with_children

    # Fix: derive missing keys for structural nodes (no collection_key composition)
    # For nodes like sdui_props, sdui_layout, etc., derive key from node_type name
    # This ensures the JSON highlighting works correctly even if key is not set in DB
    for row in all_node_rows:
        if not row.get('key'):
            node_type_name = row['node_type__name']
            # Derive key from node_type name: sdui_props -> props, sdui_layout -> layout
            if node_type_name.startswith('sdui_'):
                derived_key = node_type_name.replace('sdui_', '')
                row['key'] = derived_key

    # Build parent→children map (sorted by sort_order)
    children_by_parent = {}
    for row in all_node_rows:
        pid = str(row["parent_id"]) if row["parent_id"] else None
        if pid not in children_by_parent:
            children_by_parent[pid] = []
        children_by_parent[pid].append(row)
    for pid in children_by_parent:
        children_by_parent[pid].sort(key=lambda r: r["sort_order"] or 0)

    node_line_map = {}

    # --- Pass 2: nodes with UUID in JSON ---
    for row in all_node_rows:
        # Skip empty nodes - they won't appear in JSON due to s7_strip_empty_values
        if row["id"] in empty_node_ids:
            continue

        uuid_str = str(row["id"])
        for li, line in enumerate(lines):
            if uuid_str in line:
                r = find_enclosing_object(li)
                if r:
                    node_line_map[uuid_str] = [r[0], r[1]]
                break

    # --- Pass 3: nodes without UUID in JSON (BFS from root, using parent's mapped range) ---
    queue = deque([str(root_id)])
    visited = set()
    while queue:
        parent_id = queue.popleft()
        if parent_id in visited:
            continue
        visited.add(parent_id)

        children = children_by_parent.get(parent_id, [])
        if not children:
            continue

        # Filter out empty nodes from children - they won't appear in JSON
        children = [c for c in children if c["id"] not in empty_node_ids]
        if not children:
            continue

        parent_range = node_line_map.get(parent_id)

        # Collect children that still need mapping
        unmapped = [c for c in children if str(c["id"]) not in node_line_map]

        # For nodes in arrays, the parent_range might be just the individual object
        # Try to use grandparent range if parent is in an array
        search_range = parent_range
        if parent_range and unmapped:
            # Check if parent is inside an array (starts with { and ends with }, but is small)
            parent_lines = lines[parent_range[0]:parent_range[1] + 1]
            parent_text = "\n".join(parent_lines)
            # If parent is a small object with "id" and "type", it's likely in an array
            if '"id"' in parent_text and '"type"' in parent_text and len(parent_lines) < 10:
                # Try to get grandparent range
                # The parent_id is the grandparent of the unmapped children
                # Get the parent's parent_id from the children_by_parent map
                grandparent_id = parent_id  # parent_id is the ID of the parent node
                # Look up the parent's parent_id from all_node_rows
                parent_row = None
                for row in all_node_rows:
                    if str(row["id"]) == parent_id:
                        parent_row = row
                        break
                if parent_row and parent_row["parent_id"]:
                    grandparent_id = str(parent_row["parent_id"])
                    if grandparent_id and grandparent_id in node_line_map:
                        grandparent_range = node_line_map[grandparent_id]
                        search_range = grandparent_range

        if search_range and unmapped:
            # Pass 3a: locate keyed sub-nodes by searching for their node.key as a JSON key
            # within the parent's line range (e.g. "layout": { or "action": {).
            still_unmapped = []
            
            # Group unmapped nodes by key to handle multiple nodes with same key
            unmapped_by_key = {}
            for c in unmapped:
                node_key = c.get("key") or ""
                if node_key:
                    if node_key not in unmapped_by_key:
                        unmapped_by_key[node_key] = []
                    unmapped_by_key[node_key].append(c)
            
            # For each key, find all matches and assign by sort_order
            for node_key, nodes_with_key in unmapped_by_key.items():
                # Sort nodes by sort_order to ensure correct assignment
                nodes_with_key.sort(key=lambda x: x["sort_order"] or 0)
                
                # Check if this is sdui_props for special handling
                is_sdui_props = any(n['node_type__name'] == 'sdui_props' for n in nodes_with_key)
                
                search_pattern = f'"{node_key}":'
                matches = []
                for li in range(search_range[0], search_range[1] + 1):
                    # More specific pattern: ensure it's followed by { or array
                    if search_pattern in lines[li]:
                        # Check if this is a key-value pair (not part of a string)
                        line = lines[li]
                        # Find the position of the pattern
                        pattern_pos = line.find(search_pattern)
                        if pattern_pos != -1:
                            # Check if it's followed by { or [ or " (string value)
                            after_pattern = line[pattern_pos + len(search_pattern):].strip()
                            if after_pattern.startswith('{'):
                                # For objects, find the object that starts at the line with the {
                                # First, find the line with the {
                                brace_line = li
                                if '{' not in line:
                                    # The { might be on the next line
                                    for next_li in range(li + 1, min(li + 3, len(lines))):
                                        if '{' in lines[next_li]:
                                            brace_line = next_li
                                            break
                                r = find_object_starting_at(brace_line, within=search_range)
                                if r and r != search_range:
                                    matches.append((li, r))
                            elif after_pattern.startswith('['):
                                # For arrays, find the array that starts at the line with the [
                                brace_line = li
                                if '[' not in line:
                                    # The [ might be on the next line
                                    for next_li in range(li + 1, min(li + 3, len(lines))):
                                        if '[' in lines[next_li]:
                                            brace_line = next_li
                                            break
                                r = find_object_starting_at(brace_line, within=search_range)
                                if r and r != search_range:
                                    matches.append((li, r))
                            elif after_pattern.startswith('"'):
                                # For string values, the value is on the same line
                                # Just use the line itself as the range
                                matches.append((li, [li, li]))
                
                # Sort matches by line number
                matches.sort(key=lambda x: x[0])
                
                # For sdui_props, use a different strategy: find direct children only
                # sdui_props should be direct children of the parent, not nested
                if is_sdui_props and len(matches) > 1:
                    # If we're using grandparent range, filter to matches within parent range
                    if search_range != parent_range:
                        # Filter matches to only those within the parent's range
                        parent_matches = []
                        for li, r in matches:
                            # Check if the match is within the parent's range
                            if r[0] >= parent_range[0] and r[1] <= parent_range[1]:
                                parent_matches.append((li, r))
                        if parent_matches:
                            matches = parent_matches
                    else:
                        # Get direct children ranges of parent
                        direct_children = objects_directly_inside(parent_range)
                        # Filter matches to only those that are direct children
                        direct_matches = []
                        for li, r in matches:
                            if any(r == dc for dc in direct_children):
                                direct_matches.append((li, r))
                        if direct_matches:
                            matches = direct_matches
                
                # Assign matches to nodes by sort_order
                for idx, node in enumerate(nodes_with_key):
                    if idx < len(matches):
                        node_line_map[str(node["id"])] = [matches[idx][1][0], matches[idx][1][1]]
                    else:
                        # No match available, will be handled by Pass 3b
                        still_unmapped.append(node)
            
            # Collect nodes that still need mapping (no key or no match)
            for c in unmapped:
                if str(c["id"]) not in node_line_map:
                    still_unmapped.append(c)

            # Pass 3b: fallback — match remaining unmapped to unclaimed ranges by sort_order
            if still_unmapped:
                direct_children_ranges = objects_directly_inside(parent_range)
                unclaimed = []
                for r in direct_children_ranges:
                    claimed = any(
                        node_line_map.get(str(c["id"])) == [r[0], r[1]]
                        for c in children
                    )
                    if not claimed:
                        unclaimed.append(r)

                unmapped_by_type = {}
                for c in still_unmapped:
                    t = c["node_type__name"]
                    if t not in unmapped_by_type:
                        unmapped_by_type[t] = []
                    unmapped_by_type[t].append(c)

                for r in sorted(unclaimed, key=lambda x: x[0]):
                    for t, candidates in unmapped_by_type.items():
                        if candidates:
                            node_line_map[str(candidates[0]["id"])] = [r[0], r[1]]
                            candidates.pop(0)
                            break

        # Continue BFS
        for c in children:
            queue.append(str(c["id"]))

    return node_line_map
