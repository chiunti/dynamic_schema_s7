"""
Utility functions for the schemas admin module.
"""
from collections import deque

from ..models import Node


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

    # --- Load full subtree with parent/type info ---
    all_node_rows = list(Node.objects.select_related("node_type").filter(
        id__in=_get_subtree_ids(root_id)
    ).values("id", "parent_id", "node_type__name", "sort_order"))

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

        parent_range = node_line_map.get(parent_id)

        # Collect children that still need mapping
        unmapped = [c for c in children if str(c["id"]) not in node_line_map]

        if parent_range and unmapped:
            # Objects directly inside the parent range (one level deep)
            direct_children_ranges = objects_directly_inside(parent_range)

            # Objects not claimed by any already-mapped child
            unclaimed = []
            for r in direct_children_ranges:
                claimed = any(
                    node_line_map.get(str(c["id"])) == [r[0], r[1]]
                    for c in children
                )
                if not claimed:
                    unclaimed.append(r)

            # Match unmapped children to unclaimed ranges by sort_order
            # Group unmapped by node_type for ordered matching
            unmapped_by_type = {}
            for c in unmapped:
                t = c["node_type__name"]
                if t not in unmapped_by_type:
                    unmapped_by_type[t] = []
                unmapped_by_type[t].append(c)

            # Assign unclaimed ranges to unmapped children in sort_order
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


def _get_subtree_ids(root_id):
    """Return all node IDs in the subtree rooted at root_id (BFS)."""
    ids = []
    queue = deque([root_id])
    while queue:
        current = queue.popleft()
        ids.append(current)
        children = Node.objects.filter(parent_id=current).values_list("id", flat=True)
        queue.extend(children)
    return ids
