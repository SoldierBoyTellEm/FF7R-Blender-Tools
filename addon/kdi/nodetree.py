"""Read-only Blender node-graph view of a KineDriver rig.

KDI is genuinely node based: ``Operators`` are typed nodes and ``ConnectionBody``
entries are edges carrying named ports, authored originally as a Maya graph. This
module mirrors that structure into a custom Blender node tree so the rig can be
inspected in the Node Editor.

It is a **viewer**: editing the tree changes nothing. The driver layer is still
generated from the audit by ``kdi/drivers.py``; nothing here writes back.

Two things shape the design:

- **Connection operators are edges, not nodes.** They are skipped as nodes and
  consumed to create links, which is both truer to the graph and removes roughly
  half the node count.
- **These graphs are large** -- a median of 681 operators and up to 4,455, which
  Blender's node editor does not enjoy. The default entry point therefore builds
  the subgraph driving one target bone (everything that transitively feeds it),
  with a hard node cap as a backstop.

Cooked KDI carries no node coordinates, so positions are computed here by longest
path layering.
"""

from __future__ import annotations

from typing import Any

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty

from ..reporting import FF7R_LoggedOperator

TREE_TYPE = "FF7R_KDI_NodeTree"
SOCKET_TYPE = "FF7R_KDI_Socket"
NODE_TYPE = "FF7R_KDI_Node"

# Node body colours by operator family, so a graph reads at a glance.
FAMILY_COLORS = {
    "SOURCE": (0.22, 0.36, 0.24),
    "EFFECTOR": (0.38, 0.30, 0.16),
    "TARGET": (0.20, 0.26, 0.40),
    "OTHER": (0.28, 0.28, 0.28),
}

LAYER_SPACING_X = 340.0
LAYER_SPACING_Y = 190.0
DEFAULT_MAX_NODES = 400


def operator_family(operator_type: str) -> str:
    if operator_type.startswith("Source"):
        return "SOURCE"
    if operator_type.startswith("Effector"):
        return "EFFECTOR"
    if operator_type.startswith("Target"):
        return "TARGET"
    return "OTHER"


def _number(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def node_summary(operator_type: str, body: dict[str, Any] | None) -> list[str]:
    """A few human-meaningful lines for a node, chosen per operator family."""
    if not isinstance(body, dict):
        return []
    lines: list[str] = []
    family = operator_family(operator_type)

    if family == "SOURCE":
        bones = body.get("SourceBoneNameArray") or []
        if bones:
            lines.append(f"from: {', '.join(str(b) for b in bones[:2])}"
                         + (f" +{len(bones) - 2}" if len(bones) > 2 else ""))
        base = body.get("BaseSpaceInfo") or {}
        base_type = str(base.get("BaseSpaceType", "")).rsplit("_", 1)[-1]
        if base_type and base_type != "PARENT":
            lines.append(f"space: {base_type} {base.get('BoneName') or ''}".strip())
        if (body.get("MirrorParams") or {}).get("EnableMirroring"):
            lines.append("mirrored")
        if body.get("ReverseOrder"):
            lines.append("reverse order")
    elif family == "TARGET":
        bone = body.get("TargetObjectBoneName")
        if bone:
            lines.append(f"to: {bone}")
    elif operator_type == "EffectorEZParamLink":
        x0 = body.get("PX0", 0.0)
        span = float(body.get("VX1_0", 0.0) or 0.0) + float(body.get("VX2_1", 0.0) or 0.0)
        lines.append(f"x: {_number(x0)} .. {_number(float(x0 or 0.0) + span)}")
        lines.append(f"y: {_number(body.get('PY0'))} .. {_number(body.get('PY2'))}")
        if body.get("ByCoef"):
            lines.append("ByCoef (scales input)")
    elif operator_type == "EffectorEZParamLinkLinear":
        lines.append(f"y = {_number(body.get('Scale', 1.0))}x + {_number(body.get('Offset', 0.0))}")
        if body.get("EnableMin") or body.get("EnableMax"):
            lines.append("clamped")
    return lines


class FF7R_KDI_Socket(bpy.types.NodeSocket):
    """One named KineDriver port (a ParameterType channel)."""

    bl_idname = SOCKET_TYPE
    bl_label = "KDI Port"

    coefficient: bpy.props.FloatProperty(name="Coef", default=1.0)

    def draw(self, _context, layout, _node, text):
        # The wire's unit-conversion Coef belongs to the edge; surfacing it on the
        # input side is where it is actually applied, and a non-unit value is
        # exactly the sort of thing worth spotting at a glance.
        if not self.is_output and self.coefficient not in (1.0, 0.0):
            layout.label(text=f"{text}  x{self.coefficient:g}")
        else:
            layout.label(text=text)

    def draw_color(self, _context, _node):
        return (0.70, 0.60, 0.35, 1.0)

    @classmethod
    def draw_color_simple(cls):
        return (0.70, 0.60, 0.35, 1.0)


class FF7R_KDI_Node(bpy.types.Node):
    """One KineDriver operator.

    Deliberately one class for every operator type rather than 24 subclasses: the
    type is data, sockets are built from the asset's own connections, and an
    operator type this add-on has never seen still renders correctly.
    """

    bl_idname = NODE_TYPE
    bl_label = "KDI Operator"
    bl_width_default = 230.0

    operator_type: StringProperty(name="Operator", default="")
    operator_index: IntProperty(name="Index", default=-1)
    summary_text: StringProperty(name="Summary", default="")
    asset_label: StringProperty(name="Label", default="")

    @classmethod
    def poll(cls, node_tree):
        return node_tree.bl_idname == TREE_TYPE

    def draw_label(self):
        return f"{self.operator_type} #{self.operator_index}"

    def draw_buttons(self, _context, layout):
        if self.asset_label:
            layout.label(text=self.asset_label, icon="DOT")
        for line in self.summary_text.split("\n"):
            if line:
                layout.label(text=line)


class FF7R_KDI_NodeTree(bpy.types.NodeTree):
    """Read-only view of a KineDriver operator graph."""

    bl_idname = TREE_TYPE
    bl_label = "FF7R KineDriver"
    bl_icon = "DRIVER"


def _port_key(port: dict[str, Any]) -> str:
    name = port.get("parameter") or "?"
    multi = port.get("multi_index") or 0
    return f"{name}[{multi}]" if multi else str(name)


def subgraph_for_bone(
        nodes: list[dict[str, Any]],
        connections: list[dict[str, Any]],
        target_bone: str,
) -> set[int]:
    """Operator indices that transitively feed the given target bone."""
    by_index = {int(n["operator_index"]): n for n in nodes}
    incoming: dict[int, list[int]] = {}
    for connection in connections:
        source = connection.get("source") or {}
        target = connection.get("target") or {}
        if source.get("operator_index") is None or target.get("operator_index") is None:
            continue
        incoming.setdefault(int(target["operator_index"]), []).append(int(source["operator_index"]))

    seeds = [
        index for index, node in by_index.items()
        if (node.get("body") or {}).get("TargetObjectBoneName") == target_bone
    ]
    seen: set[int] = set()
    stack = list(seeds)
    while stack:
        index = stack.pop()
        if index in seen:
            continue
        seen.add(index)
        stack.extend(incoming.get(index, ()))
    return seen


def _assign_layers(indices: set[int], edges: list[tuple[int, int]]) -> dict[int, int]:
    """Longest-path layering, tolerant of the cycles the audit can report."""
    successors: dict[int, list[int]] = {}
    indegree = {index: 0 for index in indices}
    for source, target in edges:
        successors.setdefault(source, []).append(target)
        indegree[target] = indegree.get(target, 0) + 1

    layer = {index: 0 for index in indices}
    ready = [index for index in indices if indegree.get(index, 0) == 0]
    visited = 0
    while ready:
        index = ready.pop()
        visited += 1
        for successor in successors.get(index, ()):
            layer[successor] = max(layer[successor], layer[index] + 1)
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
    if visited != len(indices):
        # A cycle (or a cycle-blocked region) left nodes unranked; fall back to
        # operator order for those so the view still draws rather than failing.
        for index in sorted(indices):
            if indegree.get(index, 0) > 0:
                layer[index] = max(layer.values(), default=0) + 1
    return layer


def build_node_tree(
        tree: Any,
        graph: dict[str, Any],
        target_bone: str = "",
        max_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, Any]:
    """Populate `tree` from an audit graph. Returns a small stats dict."""
    tree.nodes.clear()
    all_nodes = graph["nodes"]
    connections = graph.get("live_connections") or []

    # Connections are edges; every other operator is a node.
    candidates = {
        int(node["operator_index"]): node
        for node in all_nodes
        if node.get("operator_type") != "Connection"
    }
    if target_bone:
        keep = subgraph_for_bone(all_nodes, connections, target_bone)
        candidates = {i: n for i, n in candidates.items() if i in keep}

    truncated = False
    if len(candidates) > max_nodes:
        truncated = True
        candidates = dict(sorted(candidates.items())[:max_nodes])

    edges: list[tuple[int, int]] = []
    used_connections = []
    for connection in connections:
        source = connection.get("source") or {}
        target = connection.get("target") or {}
        source_index, target_index = source.get("operator_index"), target.get("operator_index")
        if source_index is None or target_index is None:
            continue
        if int(source_index) in candidates and int(target_index) in candidates:
            edges.append((int(source_index), int(target_index)))
            used_connections.append(connection)

    layers = _assign_layers(set(candidates), edges)
    per_layer: dict[int, int] = {}
    created: dict[int, Any] = {}
    for index in sorted(candidates):
        source_node = candidates[index]
        operator_type = source_node.get("operator_type") or "Unknown"
        node = tree.nodes.new(NODE_TYPE)
        node.operator_type = operator_type
        node.operator_index = index
        label = source_node.get("label")
        node.asset_label = "" if label in (None, "None") else str(label)
        node.summary_text = "\n".join(node_summary(operator_type, source_node.get("body")))
        node.use_custom_color = True
        node.color = FAMILY_COLORS[operator_family(operator_type)]
        layer = layers.get(index, 0)
        row = per_layer.get(layer, 0)
        per_layer[layer] = row + 1
        node.location = (layer * LAYER_SPACING_X, -row * LAYER_SPACING_Y)
        created[index] = node

    # Sockets are created on demand from the connections that actually exist, so
    # a node only shows the ports its rig really uses.
    def socket(node: Any, collection: Any, key: str) -> Any:
        existing = collection.get(key)
        if existing is not None:
            return existing
        return collection.new(SOCKET_TYPE, key)

    link_count = 0
    for connection in used_connections:
        source, target = connection["source"], connection["target"]
        from_node = created[int(source["operator_index"])]
        to_node = created[int(target["operator_index"])]
        from_socket = socket(from_node, from_node.outputs, _port_key(source))
        to_socket = socket(to_node, to_node.inputs, _port_key(target))
        try:
            to_socket.coefficient = float(connection.get("coefficient", 1.0) or 1.0)
        except (TypeError, ValueError):
            pass
        tree.links.new(from_socket, to_socket)
        link_count += 1

    return {
        "node_count": len(created),
        "link_count": link_count,
        "truncated": truncated,
        "total_operators": len(all_nodes),
    }


def _selected_bone_name(context: Any) -> str:
    armature = context.active_object
    if not armature or armature.type != "ARMATURE":
        return ""
    bone = getattr(armature.data, "bones", None)
    active = getattr(bone, "active", None) if bone else None
    return active.name if active else ""


class FF7R_KDI_OT_visualize(FF7R_LoggedOperator):
    """Show a KineDriver rig as a node graph in the Node Editor"""

    bl_idname = "kdi.visualize_graph"
    bl_label = "KDI: Visualize Driver Graph"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(
        name="KDI JSON",
        description="KineDriver JSON to visualize. Leave blank to use the one the "
                    "active armature's driver layer was built from",
        subtype="FILE_PATH",
        default="",
    )
    target_bone: StringProperty(
        name="Only bones driving",
        description="Show just the operators that transitively drive this target "
                    "bone. Leave blank to show the whole graph, which is large",
        default="",
    )
    max_nodes: IntProperty(
        name="Node limit",
        description="Stop after this many operators. Blender's node editor becomes "
                    "unusable well before a full rig's operator count",
        default=DEFAULT_MAX_NODES,
        min=10,
        max=5000,
    )

    def invoke(self, context, _event):
        # Debugging almost always starts from "this bone looks wrong", so seed the
        # filter with whatever bone is selected.
        if not self.target_bone:
            self.target_bone = _selected_bone_name(context)
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "filepath")
        layout.prop(self, "target_bone")
        layout.prop(self, "max_nodes")
        layout.label(text="Open a Node Editor and pick the 'FF7R KineDriver' tree type.",
                     icon="INFO")

    def execute(self, context):
        from pathlib import Path
        from . import audit as kdi_audit
        from .drivers import SOURCE_PROPERTY

        source = (self.filepath or "").strip()
        if not source:
            armature = context.active_object
            if armature and armature.type == "ARMATURE":
                source = armature.get(SOURCE_PROPERTY, "")
            if not source:
                self.report({"ERROR"},
                            "No KDI given. Choose a file, or select an armature that "
                            "already has an imported KDI layer.")
                return {"CANCELLED"}

        path = Path(bpy.path.abspath(source))
        if not path.is_file():
            self.report({"ERROR"}, f"KDI JSON not found: {path}")
            return {"CANCELLED"}

        try:
            asset, raw = kdi_audit.read_kdi(path)
            graph = kdi_audit.build_graph(asset, path, raw)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not read the KDI: {exc}")
            return {"CANCELLED"}

        name = f"KDI {asset.get('Name') or path.stem}"
        if self.target_bone:
            name += f" [{self.target_bone}]"
        tree = bpy.data.node_groups.get(name)
        if tree is None or tree.bl_idname != TREE_TYPE:
            tree = bpy.data.node_groups.new(name, TREE_TYPE)

        try:
            stats = build_node_tree(tree, graph, self.target_bone.strip(), self.max_nodes)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not build the graph view: {exc}")
            return {"CANCELLED"}

        if not stats["node_count"]:
            self.report({"WARNING"},
                        f"Nothing to show for '{self.target_bone}' -- no operator in this "
                        f"KDI drives that bone.")
            return {"CANCELLED"}

        # Point any open Node Editor at the tree, so it is visible without hunting.
        for area in context.screen.areas:
            if area.type == "NODE_EDITOR":
                for space in area.spaces:
                    if space.type == "NODE_EDITOR":
                        space.tree_type = TREE_TYPE
                        space.node_tree = tree

        message = (f"'{name}': {stats['node_count']} operators, {stats['link_count']} "
                   f"connections (of {stats['total_operators']} operators in the asset)")
        if stats["truncated"]:
            message += f"; truncated at the {self.max_nodes} node limit"
        self.report({"INFO"}, message)
        return {"FINISHED"}


CLASSES = (
    FF7R_KDI_Socket,
    FF7R_KDI_Node,
    FF7R_KDI_NodeTree,
    FF7R_KDI_OT_visualize,
)
