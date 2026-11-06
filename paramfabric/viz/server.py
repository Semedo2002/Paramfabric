"""FastAPI server for an interactive parameter dependency graph"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import load_fabric

_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width">
  <title>Paramfabric graph</title>
  <script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
  <style>
    body{margin:0;background:#101827;color:#e5edf7;font:15px system-ui,sans-serif}
    header{padding:16px 24px;background:#172338}h1{font-size:20px;margin:0}
    svg{width:100vw;height:calc(100vh - 58px)}
    .link{stroke:#61738d;stroke-opacity:.65}.node circle{stroke:#c7d6e8;stroke-width:1.5}
    .node text{fill:#f2f6fc;font-size:12px;pointer-events:none}
  </style>
</head>
<body><header><h1>Paramfabric, parameter lineage</h1></header><svg></svg>
<script>
fetch('/api/graph').then(r=>r.json()).then(data=>{
  const svg=d3.select('svg'), width=innerWidth, height=innerHeight-58;
  const color=d3.scaleOrdinal(d3.schemeTableau10), links=data.links;
  const simulation=d3.forceSimulation(data.nodes)
    .force('link',d3.forceLink(links).id(d=>d.id).distance(115))
    .force('charge',d3.forceManyBody().strength(-360))
    .force('center',d3.forceCenter(width/2,height/2));
  const link=svg.append('g').selectAll('line').data(links).join('line').attr('class','link');
  const node=svg.append('g').selectAll('g').data(data.nodes).join('g').attr('class','node')
    .call(d3.drag().on('start',(e,d)=>{if(!e.active)simulation.alphaTarget(.3).restart();d.fx=d.x;d.fy=d.y})
      .on('drag',(e,d)=>{d.fx=e.x;d.fy=e.y})
      .on('end',(e,d)=>{if(!e.active)simulation.alphaTarget(0);d.fx=null;d.fy=null}));
  node.append('circle').attr('r',10).attr('fill',d=>color(d.tags[0]||'untagged'));
  node.append('title').text(d=>`${d.id}: ${d.value} ${d.unit}\\n${d.description}`);
  node.append('text').attr('x',13).attr('y',4).text(d=>d.id);
  simulation.on('tick',()=>{link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y)
    .attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
    node.attr('transform',d=>`translate(${d.x},${d.y})`)});
});
</script></body></html>"""


def create_app(fabric_path: str | Path = "params.yaml") -> FastAPI:
    """Create a read-only local API and force-directed graph viewer"""
    path = Path(fabric_path)
    app = FastAPI(title="Paramfabric", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _HTML

    @app.get("/api/graph")
    def graph_data() -> dict[str, list[dict[str, object]]]:
        graph=ParameterGraph(load_fabric(path))
        values=graph.evaluate()
        nodes = [
            {
                "id": parameter.name,
                "value": parameter.value if parameter.type in {"str", "bool"} else float(values[parameter.name].magnitude),
                "unit": parameter.unit,
                "description": parameter.description,
                "tags": parameter.tags,
            }
            for parameter in graph.fabric.parameters
        ]
        links = [
            {"source": source, "target": target}
            for source, target in graph.graph.edges
        ]
        return {"nodes": nodes, "links": links}

    return app
