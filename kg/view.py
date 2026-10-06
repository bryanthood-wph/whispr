"""`python -m kg.view`: a graph neighbourhood as one JSON answer with a self-contained SVG.

The back end of a Claude Code pane that shows the graph around one entity: the pane runs
this as a subprocess and renders the `svg` it prints (or shows `text` in a terminal).

- **Read-only.** The database is opened with kg.db.connect_readonly, like the MCP
  server (kg/mcp_server.py); the reads are Store.search, Store.entities_named and
  Traverser.neighbors, so the walk, its filters and its bounds are exactly MCP's.
- **One JSON object on stdout** (UTF-8), always: the answer and exit 0, or
  `{"error": ...}` and a non-zero exit naming what went wrong (USAGE, NO_DATABASE,
  NOT_FOUND, AMBIGUOUS, ERROR below), so the pane can tell "nothing built yet" from a
  typo. Tracebacks go to stderr.
- **The drawing.** Radial rings by depth around the centre; each entity is placed in
  a wedge of its parent's (the entity that reached it on its shortest route), sized by
  how many leaves sit under it, so neighbours of a node sit near it. Only the walk's
  shortest-route edges are drawn (a tree). Same input, byte-identical SVG: every
  order comes from the walk's own order, no randomness.
- **Safe to inline.** Names and quotes are XML-escaped; no script, no event
  attributes, no external reference (colours and the font are checked on start, so
  config text cannot add one). Hover tooltips are `<title>` elements, the highlight
  is CSS `:hover`.
- **Bounded.** At most kg.view.max_nodes neighbours, and the SVG is never longer than
  kg.view.svg_max_chars, counted as the host counts it (UTF-16 code units, so an emoji
  is two): the farthest, least recently linked neighbours (the end of the walk's order)
  are dropped until it fits, and the answer says `truncated`. A page always moves on:
  a neighbour too big to draw even alone is skipped, never offered again as the next page.

Every size, colour and cut length is config (kg.view in config/defaults.yaml).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Optional, Sequence

from kg import db
from kg.store import Store
from kg.traverse import Traverser, cut
from pipeline.config import load_config

log = logging.getLogger("kg.view")

# Exit codes: the CLI contract the pane is written against.
OK = 0
ERROR = 1           # the query was refused (an unknown relation or type, a bad time, the walk's time limit) or config
USAGE = 2           # the command line is wrong
NO_DATABASE = 3     # no database file yet: the pipeline has not run
NOT_FOUND = 4       # no entity has that id or exact name
AMBIGUOUS = 5       # several entities have that exact name; the answer lists them

# What config text may hold where it lands in the SVG: hex colours, and a font list of
# plain names, so no config value can open a url(), an import or an attribute.
_HEX_COLOR = re.compile(r"#(?:[0-9A-Fa-f]{3}){1,2}")
_FONT_FAMILY = re.compile(r"[A-Za-z0-9 ,\-]+")
# Characters XML 1.0 cannot hold, even escaped.
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
_XML_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"))
SVG_NS = "http://www.w3.org/2000/svg"
# Why an answer was cut, as `truncated_by` names it.
CUT_MAX_NODES, CUT_SVG = "max_nodes", "svg_max_chars"


class ViewError(Exception):
    """An answer that is an error: its exit code and the JSON to print."""

    def __init__(self, code: int, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.answer = {"error": message, **extra}


def host_length(text: str) -> int:
    """`text`'s length as the host pane counts it: UTF-16 code units (a JavaScript string)."""
    return len(text.encode("utf-16-le")) // 2


def xml_text(text: str) -> str:
    """`text` safe as XML character data or a quoted attribute value."""
    text = _NOT_XML.sub("", text)
    for raw, entity in _XML_ESCAPES:
        text = text.replace(raw, entity)
    return text


def _one_line(text: Optional[str]) -> str:
    return " ".join((text or "").split())


class View:
    def __init__(self, store: Store, cfg: dict):
        self.store = store
        self.cfg = cfg
        self.traverser = Traverser(store)
        self.v = cfg["kg"]["view"]
        self._check_config()

    def _check_config(self) -> None:
        v = self.v
        missing = sorted(self.store.entity_types - set(v["palette"]))
        if missing:
            raise ViewError(ERROR, f"kg.view.palette has no colour for entity type(s) {missing} "
                                   f"(every type in {self.cfg['ontology']} needs one)")
        colors = {**{f"colors.{k}": c for k, c in v["colors"].items()},
                  **{f"palette.{k}": c for k, c in v["palette"].items()}}
        bad = sorted(k for k, c in colors.items() if not _HEX_COLOR.fullmatch(c))
        if bad:
            raise ViewError(ERROR, f"kg.view.{', kg.view.'.join(bad)}: a colour must be #rgb or #rrggbb")
        if not _FONT_FAMILY.fullmatch(v["font_family"]):
            raise ViewError(ERROR, "kg.view.font_family may hold only letters, digits, spaces, commas and hyphens")
        if v["margin_px"] * 2 >= v["canvas_px"]:
            raise ViewError(ERROR, "kg.view.margin_px must be less than half of kg.view.canvas_px")

    # ---- reads ----------------------------------------------------------------------

    def search(self, text: str) -> dict:
        """Entity cards for the pane's picker: Store.search, entities only."""
        cards = [{"id": c["id"], "name": c["name"], "type": c["type"]}
                 for c in self.store.search(text) if c["kind"] == "entity"]
        return {"query": text, "results": cards}

    def resolve(self, ref: str) -> str:
        """The entity `ref` is: an id (a merged-away one included; the walk follows it to
        its survivor), else the one live entity it exactly names."""
        if self.store.entity(ref) is not None:
            return ref
        named = self.store.entities_named(ref)
        if not named:
            raise ViewError(NOT_FOUND, f"no entity with id or exact name {ref!r}; try search")
        if len(named) > 1:
            raise ViewError(AMBIGUOUS, f"{len(named)} entities are named {ref!r}; pass an id",
                            candidates=[self._card(entity_id) for entity_id in named])
        return named[0]

    def _card(self, entity_id: str) -> dict:
        ent = self.store.entity(entity_id)
        return {"id": ent["id"], "name": ent["canonical_name"], "type": ent["type"]}

    def neighborhood(self, ref: str, *, hops: Optional[int] = None, types: Optional[Sequence[str]] = None,
                     relations: Optional[Sequence[str]] = None, since: Optional[str] = None,
                     until: Optional[str] = None, as_of: Optional[str] = None, offset: int = 0,
                     through_owner: bool = False) -> dict:
        """The neighbourhood answer (module docstring): Traverser.neighbors, paged up to
        kg.view.max_nodes, drawn, and cut until the SVG fits kg.view.svg_max_chars."""
        v = self.v
        entity_id = self.resolve(ref)
        hops = v["default_hops"] if hops is None else hops
        results: list[dict] = []
        at = offset
        while True:
            answer = self.traverser.neighbors(entity_id, hops=hops, relations=relations, since=since, until=until,
                                              as_of=as_of, types=types, offset=at, through_owner=through_owner)
            results += answer["results"]
            if not answer["truncated"] or not answer["results"] or len(results) >= v["max_nodes"]:
                break
            at += len(answer["results"])
        truncated_by = [CUT_MAX_NODES] if answer["truncated"] or len(results) > v["max_nodes"] else []
        results = results[:v["max_nodes"]]
        center = answer["start"]
        dates: dict[str, Optional[str]] = {}
        # The most neighbours whose drawing fits: the walk lists the farthest, least
        # recently linked last, so those go first.
        for keep in range(len(results), -1, -1):
            drawing = self._draw(center, results[:keep], answer["hops"], answer["reached"], offset,
                                 bool(truncated_by) or keep < len(results), dates)
            if host_length(drawing["svg"]) <= v["svg_max_chars"]:
                break
        else:
            raise ViewError(ERROR, f"kg.view.svg_max_chars ({v['svg_max_chars']}) is too small to draw even "
                                   f"{center['name']!r} alone")
        if keep < len(results):
            truncated_by.append(CUT_SVG)
        step = keep or min(len(results), 1)                             # skip one too big to draw alone
        return {"center": center, "hops": answer["hops"], "hops_requested": answer["hops_requested"],
                "reached": answer["reached"], "offset": offset,
                "next_offset": offset + step if truncated_by else None,
                "truncated": bool(truncated_by), "truncated_by": truncated_by, **drawing}

    # ---- drawing --------------------------------------------------------------------

    def _draw(self, center: dict, results: list[dict], hops: int, reached: int, offset: int, cut_short: bool,
              dates: dict[str, Optional[str]]) -> dict:
        tree = self._tree(center, results)
        self._layout(tree)
        alt = self._alt(center, tree, hops, reached, offset, cut_short)
        nodes = [{**{k: n[k] for k in ("id", "name", "type", "depth")}, "x": n["x"], "y": n["y"],
                  "route_only": n["route_only"]} for n in tree["nodes"]]
        edges = [self._edge_json(n["edge"], dates) for n in tree["nodes"][1:]]
        return {"nodes": nodes, "edges": edges, "svg": self._svg(tree, edges, alt), "alt": alt,
                "text": self._text(tree, edges, cut_short, offset)}

    def _tree(self, center: dict, results: list[dict]) -> dict:
        """The drawn entities as a tree, centre first then by depth in the walk's order;
        each with its parent and the edge from it (the last hop of its chain). An entity
        on a chain but not itself a result (its type filtered out, or on an earlier page)
        is drawn as `route_only`, so no edge hangs loose."""
        root = {"id": center["id"], "name": center["name"], "type": center["type"], "depth": 0,
                "parent": None, "edge": None, "route_only": False}
        by_id = {root["id"]: root}
        for result in results:
            at = root
            for edge in result["chain"]:
                nxt, name = ((edge["dst_id"], edge["dst_name"]) if edge["src_id"] == at["id"]
                             else (edge["src_id"], edge["src_name"]))
                if nxt not in by_id:
                    by_id[nxt] = {"id": nxt, "name": name, "type": None, "depth": at["depth"] + 1,
                                  "parent": at["id"], "edge": edge, "route_only": True}
                at = by_id[nxt]
            at["type"], at["route_only"] = result["entity"]["type"], False
        for node in by_id.values():
            if node["type"] is None:
                node["type"] = self.store.entity(node["id"])["type"]
        nodes = sorted(by_id.values(), key=lambda n: n["depth"])        # stable: the walk's order within a depth
        children: dict[str, list[dict]] = {n["id"]: [] for n in nodes}
        for node in nodes[1:]:
            children[node["parent"]].append(node)
        return {"nodes": nodes, "by_id": by_id, "children": children}

    def _layout(self, tree: dict) -> None:
        """x, y for every node: depth rings around the centre, each node in a wedge of
        its parent's sized by the leaves under it, at the wedge's middle angle."""
        v = self.v
        nodes, children = tree["nodes"], tree["children"]
        leaves: dict[str, int] = {}
        for node in reversed(nodes):                                    # children before parents
            leaves[node["id"]] = sum(leaves[c["id"]] for c in children[node["id"]]) or 1
        half = v["canvas_px"] / 2
        deepest = nodes[-1]["depth"]
        ring = min(v["ring_px"], (half - v["margin_px"]) / deepest) if deepest else 0
        start = math.radians(v["start_deg"])
        wedges = {nodes[0]["id"]: (start, start + 2 * math.pi)}
        for node in nodes:                                              # parents before children
            low, high = wedges[node["id"]]
            span = high - low                                           # the parent's whole wedge, shared out
            for child in children[node["id"]]:
                width = span * leaves[child["id"]] / leaves[node["id"]]
                wedges[child["id"]] = (low, low + width)
                low += width
        places = v["coord_decimals"]
        for node in nodes:
            low, high = wedges[node["id"]]
            angle = (low + high) / 2
            radius = node["depth"] * ring
            node["angle"] = angle
            node["x"] = round(half + radius * math.cos(angle), places)
            node["y"] = round(half + radius * math.sin(angle), places)

    def _alt(self, center: dict, tree: dict, hops: int, reached: int, offset: int, cut_short: bool) -> str:
        shown = [n for n in tree["nodes"][1:] if not n["route_only"]]
        via = len(tree["nodes"]) - 1 - len(shown)
        head = f"{center['name']} ({center['type']})"
        within = f"within {hops} hop{'' if hops == 1 else 's'}"
        if not shown and not via:
            if cut_short or offset:
                return f"{head} has {reached} neighbor{'' if reached == 1 else 's'} {within}; none on this page."
            return f"{head} has no neighbors {within}."
        counts: dict[str, int] = {}
        for node in shown:
            counts[node["type"]] = counts.get(node["type"], 0) + 1
        by_type = ", ".join(f"{t} {counts[t]}" for t in sorted(counts))
        text = f"{head} and {len(shown)} neighbor{'' if len(shown) == 1 else 's'} {within}"
        text += f" ({by_type})" if by_type else ""
        if via:
            text += f", plus {via} {'entity' if via == 1 else 'entities'} on the routes to them"
        if cut_short or offset:
            text += f"; showing {offset + 1}-{offset + len(shown)} of {reached}"
        return text + "."

    def _date(self, episode_id: str, dates: dict[str, Optional[str]]) -> Optional[str]:
        if episode_id not in dates:
            ep = self.store.episode(episode_id)
            start = ep["meeting_start"] if ep else None
            dates[episode_id] = datetime.fromisoformat(start).astimezone().date().isoformat() if start else None
        return dates[episode_id]

    def _edge_json(self, card: dict, dates: dict[str, Optional[str]]) -> dict:
        return {"id": card["id"], "src": card["src_id"], "dst": card["dst_id"], "relation": card["relation"],
                "quote": cut(_one_line(card["quote"]), self.v["quote_chars"]), "episode": card["episode_id"],
                "episode_date": self._date(card["episode_id"], dates), "valid_from": card["valid_from"],
                "provenance": card["provenance"], "state": card["state"]}

    def _num(self, value: float) -> str:
        return f"{value:.{self.v['coord_decimals']}f}"

    def _svg(self, tree: dict, edges: list[dict], alt: str) -> str:
        v, colors, stroke = self.v, self.v["colors"], self.v["stroke_px"]
        size = self._num(v["canvas_px"])
        n = self._num
        style = (f".e .h{{stroke:transparent;stroke-width:{stroke['hit']}px}}"
                 f".e .l{{stroke:{colors['edge']};stroke-width:{stroke['edge']}px}}"
                 f".e:hover .l{{stroke:{colors['highlight']};stroke-width:{stroke['hover']}px}}"
                 f".n circle{{stroke:{colors['node_stroke']};stroke-width:{stroke['node']}px}}"
                 f".n.r circle{{fill-opacity:{v['route_only_opacity']}}}"
                 f".n:hover circle{{stroke:{colors['highlight']};stroke-width:{stroke['hover']}px}}"
                 f"text{{fill:{colors['label']};stroke:{colors['label_halo']};stroke-width:{stroke['halo']}px;"
                 "stroke-linejoin:round;paint-order:stroke}")
        parts = [f'<svg xmlns="{SVG_NS}" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img"'
                 f' font-family="{v["font_family"]}" font-size="{n(v["font_px"])}">',
                 f"<title>{xml_text(alt)}</title>", f"<style>{style}</style>", "<g>"]
        by_id = tree["by_id"]
        for node, edge in zip(tree["nodes"][1:], edges):
            parent = by_id[node["parent"]]
            ends = f'x1="{n(parent["x"])}" y1="{n(parent["y"])}" x2="{n(node["x"])}" y2="{n(node["y"])}"'
            src, dst = by_id[edge["src"]]["name"], by_id[edge["dst"]]["name"]
            tip = f"{src} {edge['relation']} {dst}"
            tip += f"\n“{edge['quote']}”" if edge["quote"] else ""
            tip += f"\n{edge['episode_date'] or edge['valid_from'] or edge['episode']} · {edge['provenance']}"
            parts.append(f'<g class="e"><title>{xml_text(tip)}</title><line class="h" {ends}/>'
                         f'<line class="l" {ends}/></g>')
        parts.append("</g><g>")
        children = tree["children"]
        for node in reversed(tree["nodes"]):                            # the centre last, on top
            parts.append(self._svg_node(node, bool(children[node["id"]])))
        parts.append("</g></svg>")
        return "".join(parts)

    def _svg_node(self, node: dict, inner: bool) -> str:
        """One node: its circle, its label and its tooltip. Leaves are labelled along
        their ray, outward (flipped on the left half so the text reads left to right);
        the centre and inner nodes above themselves, level, so the label does not run
        over their children."""
        v, n = self.v, self._num
        depth = node["depth"]
        radius = v["center_radius_px"] if depth == 0 else v["node_radius_px"]
        x, y = node["x"], node["y"]
        label = xml_text(cut(_one_line(node["name"]), v["label_chars"]) or "")
        if depth == 0 or inner:
            text = (f'<text x="{n(x)}" y="{n(y - radius - v["label_gap_px"])}" text-anchor="middle">'
                    f"{label}</text>")
        else:
            angle = node["angle"]
            reach = radius + v["label_gap_px"]
            tx, ty = x + reach * math.cos(angle), y + reach * math.sin(angle)
            degrees = math.degrees(angle)
            right = math.cos(angle) >= 0
            turn = degrees if right else degrees + 180
            text = (f'<text x="{n(tx)}" y="{n(ty)}" dominant-baseline="central" text-anchor="{"start" if right else "end"}"'
                    f' transform="rotate({n(turn)} {n(tx)} {n(ty)})">{label}</text>')
        hops = f"{depth} hop{'' if depth == 1 else 's'} away" if depth else "centre"
        tip = f"{node['name']} ({node['type']}), {hops}"
        if node["route_only"]:
            tip += "; on the route only (filtered out or on another page)"
        css = "n r" if node["route_only"] else "n"
        return (f'<g class="{css}"><title>{xml_text(tip)}</title>'
                f'<circle cx="{n(x)}" cy="{n(y)}" r="{n(radius)}" fill="{v["palette"][node["type"]]}"/>{text}</g>')

    def _text(self, tree: dict, edges: list[dict], cut_short: bool, offset: int) -> str:
        """The terminal fallback: an indented tree, one line per entity with the edge
        that reached it (-relation-> when stored from parent to child, <-relation-
        the other way), its quote and date."""
        indent = " " * self.v["text_indent"]
        edge_of = {node["id"]: edge for node, edge in zip(tree["nodes"][1:], edges)}
        root = tree["nodes"][0]
        lines = [f"{root['name']} ({root['type']})"]

        def walk(node: dict) -> None:
            for child in tree["children"][node["id"]]:
                edge = edge_of[child["id"]]
                arrow = f"-{edge['relation']}->" if edge["dst"] == child["id"] else f"<-{edge['relation']}-"
                line = f"{indent * child['depth']}{arrow} {child['name']} ({child['type']})"
                line += " [route]" if child["route_only"] else ""
                line += f'  "{edge["quote"]}"' if edge["quote"] else ""
                when = edge["episode_date"] or edge["valid_from"]
                line += f"  {when}" if when else ""
                lines.append(line)
                walk(child)

        walk(root)
        if cut_short:
            shown = sum(1 for node in tree["nodes"][1:] if not node["route_only"])
            lines.append(f"(more not shown: ask again with --offset {offset + shown})")
        return "\n".join(lines)


# ---- command line -------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse that raises a USAGE ViewError (printed as JSON) instead of exiting, for
    an error and for -h alike (the help text is the error)."""

    def error(self, message: str):
        raise ViewError(USAGE, f"{self.prog}: {message}")

    def print_help(self, file=None):
        raise ViewError(USAGE, self.format_help())


def _at_least(low: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
        if value < low:
            raise argparse.ArgumentTypeError(f"must be at least {low}, not {value}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument("--config", type=Path, default=None,
                        help="the per-user overlay (default %%APPDATA%%\\whispr\\config.yaml)")
    parser = _Parser(prog="python -m kg.view", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    search = commands.add_parser("search", parents=[common], help="entity cards matching any word")
    search.add_argument("text", nargs="+")
    near = commands.add_parser("neighborhood", parents=[common], help="the graph around one entity, drawn")
    near.add_argument("entity", help="an entity id, or an entity's exact name")
    near.add_argument("--hops", type=_at_least(1), default=None, help="default kg.view.default_hops")
    near.add_argument("--types", nargs="+", action="extend", default=None, metavar="T",
                      help="keep only entities of these types (the walk still passes through any)")
    near.add_argument("--relations", nargs="+", action="extend", default=None, metavar="R",
                      help="walk only edges of these relations")
    near.add_argument("--since", default=None, help="only edges valid at or after this ISO-8601 time")
    near.add_argument("--until", default=None, help="only edges valid at or before this ISO-8601 time")
    near.add_argument("--as-of", dest="as_of", default=None, help="the graph as it stood at this ISO-8601 time")
    near.add_argument("--offset", type=_at_least(0), default=0, help="skip this many neighbours (next page)")
    near.add_argument("--through-owner", dest="through_owner", action="store_true",
                      help="also walk through the owner's own person node")
    return parser


def _open(cfg: dict):
    try:
        return db.connect_readonly(cfg)
    except db.MigrationError as exc:
        if not db.database_path(cfg).is_file():
            raise ViewError(NO_DATABASE, str(exc)) from exc
        raise ViewError(ERROR, str(exc)) from exc


def run(args: argparse.Namespace) -> dict:
    cfg = load_config(overlay_path=args.config)
    conn = _open(cfg)
    try:
        view = View(Store(conn, cfg), cfg)
        if args.command == "search":
            return view.search(" ".join(args.text))
        return view.neighborhood(args.entity, hops=args.hops, types=args.types, relations=args.relations,
                                 since=args.since, until=args.until, as_of=args.as_of, offset=args.offset,
                                 through_owner=args.through_owner)
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None, stdout: Optional[BinaryIO] = None) -> int:
    """Run one command; print its one JSON object; return the exit code."""
    out = stdout if stdout is not None else sys.stdout.buffer
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        answer, code = run(build_parser().parse_args(argv)), OK
    except ViewError as exc:
        answer, code = exc.answer, exc.code
    except ValueError as exc:                  # StoreError, ConfigError, or a time that is not ISO-8601
        answer, code = {"error": str(exc)}, ERROR
    except Exception as exc:                   # anything else: still one JSON object, and the traceback on stderr
        log.exception("internal error")
        answer, code = {"error": f"internal error: {type(exc).__name__}: {exc}"}, ERROR
    out.write(json.dumps(answer, ensure_ascii=False).encode("utf-8", "backslashreplace") + b"\n")
    out.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
