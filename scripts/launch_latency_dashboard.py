"""Build an offline startup-latency dashboard from UG's local JSONL records.

Run ``python scripts/launch_latency_dashboard.py --help`` for paths. The generated
HTML contains its data and needs no web server, Python packages, or network access.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _normalize_record(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("schema_version", 1) != 1:
        return None
    duration = raw.get("duration_ms")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
        or raw.get("outcome") not in ("handoff", "failed")
        or not isinstance(raw.get("started_at"), str)
    ):
        return None
    try:
        started = datetime.fromisoformat(raw["started_at"].replace("Z", "+00:00"))
        if started.tzinfo is None:
            return None
        started_at = started.astimezone(UTC).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError):
        return None
    record: dict[str, Any] = {
        "started_at": started_at,
        "duration_ms": duration,
        "outcome": raw["outcome"],
    }
    for field in ("agent", "ug_version", "os", "arch"):
        value = raw.get(field)
        record[field] = value if isinstance(value, str) and value else "unknown"
    for field in ("managed", "auto_configure", "skip_preflight", "refresh", "smart_routing"):
        value = raw.get(field)
        record[field] = value if isinstance(value, bool) else None
    event_id = raw.get("event_id")
    if isinstance(event_id, str) and event_id:
        record["event_id"] = event_id
    return record


def load_records(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    """Read valid records; the last valid row for an event ID takes precedence.

    A missing input produces an empty dashboard so it can be opened before the
    first recorded launch. Incomplete, malformed, and unsupported rows are counted
    and ignored. Missing event IDs are kept as independent observations.
    """
    records: list[dict[str, Any]] = []
    ids: dict[str, int] = {}
    ignored = duplicates = 0
    try:
        source = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], 0, 0
    with source:
        for line in source:
            if not line.strip():
                continue
            try:
                record = _normalize_record(json.loads(line))
            except (ValueError, OverflowError):
                record = None
            if record is None:
                ignored += 1
                continue
            event_id = record.get("event_id")
            if event_id and event_id in ids:
                records[ids[event_id]] = record
                duplicates += 1
            else:
                if event_id:
                    ids[event_id] = len(records)
                records.append(record)
    return sorted(records, key=lambda row: row["started_at"]), ignored, duplicates


def render_dashboard(
    records: list[dict[str, Any]],
    *,
    ignored: int = 0,
    duplicates: int = 0,
    generated_at: str | None = None,
) -> str:
    """Embed only normalized measurement fields, escaping HTML script delimiters."""
    payload = json.dumps(
        {
            "records": records,
            "ignored": ignored,
            "duplicates": duplicates,
            "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return _HTML.replace("__LAUNCH_DATA__", payload)


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UG launch latency</title>
<style>
:root { color-scheme: light; --ink:#172b3a; --muted:#526574; --line:#dbe3e8; --p50:#087e8b; --p90:#ba4b18; }
* { box-sizing:border-box; }
body { margin:0; background:#f3f6f8; color:var(--ink); font:15px/1.5 system-ui,sans-serif; }
main { max-width:1320px; margin:auto; padding:36px 28px; }
h1 { font-size:32px; letter-spacing:-.8px; margin:0 0 8px; }
h2 { font-size:18px; margin:0 0 12px; }
p { margin:8px 0; }
.muted,small { color:var(--muted); }
.tag { color:#08736e; font-size:12px; text-transform:uppercase; font-weight:700; letter-spacing:1px; }
.panel { background:white; border:1px solid var(--line); border-radius:12px; padding:22px; margin-top:20px; }
.filters { display:flex; flex-wrap:wrap; gap:12px; }
label { display:flex; flex-direction:column; gap:5px; font-size:12px; font-weight:600; color:var(--muted); }
input,select,button { font:inherit; color:var(--ink); background:white; border:1px solid #b7c7d2; border-radius:6px; padding:8px 10px; }
input:focus-visible,select:focus-visible,button:focus-visible { outline:3px solid #43a7b6; outline-offset:2px; }
button { cursor:pointer; align-self:end; }
.metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin-top:20px; }
.metric { background:white; border:1px solid var(--line); border-radius:12px; padding:20px; }
.metric strong { display:block; font-size:32px; font-variant-numeric:tabular-nums; margin:6px 0; }
.metric span { color:var(--muted); font-size:13px; }
.p50 { color:var(--p50); } .p90 { color:var(--p90); }
.notice { background:#fff6e8; border-color:#e8c995; }
.split { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.table-wrap { overflow:auto; }
table { border-collapse:collapse; width:100%; font-size:13px; font-variant-numeric:tabular-nums; }
th,td { border-bottom:1px solid var(--line); padding:9px 8px; text-align:right; white-space:nowrap; }
th:first-child,td:first-child { text-align:left; }
th { color:var(--muted); font-weight:600; }
thead { background:#f7f9fa; }
.empty { text-align:center; padding:36px 14px; }
.chart-header { display:flex; align-items:center; justify-content:space-between; gap:20px; }
.legend { display:flex; gap:18px; font-size:13px; margin-bottom:8px; }
svg { width:100%; height:auto; display:block; }
footer { margin-top:24px; font-size:12px; color:var(--muted); }
[hidden] { display:none !important; }
@media(max-width:760px) { main { padding:24px 14px; } .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } .split { grid-template-columns:1fr; gap:0; } h1 { font-size:28px; } .metric strong { font-size:25px; } }
</style>
</head>
<body><main>
<div class="tag">Unity Gateway · local measurements</div>
<h1>UG launch latency</h1>
<p class="muted">Time from UG's Python entrypoint to agent process handoff. All times are UTC.</p>
<p class="muted">Includes CLI imports and setup; excludes Python interpreter boot, agent prompt readiness, and session runtime.</p>
<div class="panel filters" aria-label="Filter launches">
<label>From (UTC)<input id="from" type="date"></label>
<label>Through (UTC)<input id="through" type="date"></label>
<label>Agent<select id="agent"><option value="">All agents</option></select></label>
<label>UG version<select id="ug_version"><option value="">All versions</option></select></label>
<label>Platform<select id="os"><option value="">All platforms</option></select></label>
<label>Managed config<select id="managed"><option value="">All</option><option value="true">Managed</option><option value="false">Unmanaged</option><option value="unknown">Unknown</option></select></label>
<label>First-run setup<select id="auto_configure"><option value="">All</option><option value="false">Already configured</option><option value="true">Auto-configuring</option><option value="unknown">Unknown</option></select></label>
<label>Skip preflight<select id="skip_preflight"><option value="">All</option><option value="false">Standard preflight</option><option value="true">Skipped</option><option value="unknown">Unknown</option></select></label>
<label>Smart routing<select id="smart_routing"><option value="">All</option><option value="true">Enabled</option><option value="false">Disabled</option><option value="unknown">Unknown</option></select></label>
<button id="reset" type="button">Reset filters</button>
</div>
<p id="selection" class="muted" aria-live="polite"></p>
<div class="metrics" aria-label="Latency and launch counts">
<div class="metric"><span>p50 · median handoff</span><strong id="p50" class="p50">—</strong><span>50% of observed handoffs at or below</span></div>
<div class="metric"><span>p90 · slower launches</span><strong id="p90" class="p90">—</strong><span>90th percentile of observed handoff times</span></div>
<div class="metric"><span>Recorded handoffs</span><strong id="count">0</strong><span>Samples used for p50 and p90</span></div>
<div class="metric"><span>Failed before handoff</span><strong id="failed">0</strong><span>Excluded from latency percentiles</span></div>
</div>
<div id="empty" class="panel empty" hidden><h2>No recorded launches yet</h2><p>Enable UG_LAUNCH_LATENCY_PATH, launch an agent with this checkout, then regenerate this dashboard.</p></div>
<div id="sample-note" class="panel notice" hidden></div>
<section class="panel" aria-labelledby="trend-title">
<div class="chart-header"><h2 id="trend-title">Handoff latency over time</h2><label>Bucket<select id="bucket"><option value="day">Daily</option><option value="hour">Hourly</option></select></label></div>
<div class="legend"><span class="p50">● p50</span><span class="p90">● p90</span></div>
<svg id="chart" viewBox="0 0 1000 270" role="img" aria-label="p50 and p90 handoff latency over time"></svg>
<p class="muted">Each point uses the raw handoffs in its bucket. Missing buckets are omitted; hover over a point for its sample count.</p>
<div class="table-wrap"><table><thead><tr><th>Bucket (UTC)</th><th>Handoffs</th><th>p50</th><th>p90</th><th>Failed</th></tr></thead><tbody id="trend-table"></tbody></table></div>
</section>
<div class="split">
<section class="panel" aria-labelledby="agent-title"><h2 id="agent-title">By agent</h2><div class="table-wrap"><table><thead><tr><th>Agent</th><th>Handoffs</th><th>p50</th><th>p90</th><th>Failed</th></tr></thead><tbody id="agent-table"></tbody></table></div></section>
<section class="panel" aria-labelledby="version-title"><h2 id="version-title">By UG version</h2><div class="table-wrap"><table><thead><tr><th>UG version</th><th>Handoffs</th><th>p50</th><th>p90</th><th>Failed</th></tr></thead><tbody id="version-table"></tbody></table></div></section>
</div>
<section class="panel"><h2>How to read this</h2>
<p>p50 is the median. p90 describes the upper end of the observed launch times. Percentiles use linear interpolation at rank (n − 1) × q over all selected handoff durations; percentiles from different buckets or machines are never averaged.</p>
<p>A handoff records UG reaching the native agent's launch boundary. It does not prove that the agent became ready or completed a task. Setup and interactive login may affect a launch; compare similar cohorts with the filters.</p>
<p>This is a local snapshot of opted-in launches, not fleetwide usage. Counts omit failures before agent selection and measurements dropped by best-effort capture. Forwarded agent subcommands also count as handoffs. No telemetry service or dashboard server is configured.</p>
<p>Use First-run setup → Already configured to exclude auto-configuration from regular-launch latency. Smart routing reflects the requested launch mode; a setup fallback can disable it before handoff.</p>
<p id="quality" class="muted"></p>
</section>
<footer id="generated"></footer>
</main>
<script id="launch-data" type="application/json">__LAUNCH_DATA__</script>
<script>
"use strict";
const data = JSON.parse(document.getElementById("launch-data").textContent);
const records = data.records;
const byId = id => document.getElementById(id);
const filterIds = ["from", "through", "agent", "ug_version", "os", "managed", "auto_configure", "skip_preflight", "smart_routing"];
const fmtCount = n => n.toLocaleString("en-US");
const fmtDuration = value => value === null ? "—" : value < 1000 ? `${value.toFixed(0)} ms` : `${(value / 1000).toFixed(2)} s`;
function percentile(sorted, q) {
  if (!sorted.length) return null;
  const rank = (sorted.length - 1) * q;
  const lower = Math.floor(rank);
  return sorted[lower] + (sorted[Math.ceil(rank)] - sorted[lower]) * (rank - lower);
}
function summarize(rows) {
  const values = rows.filter(row => row.outcome === "handoff").map(row => row.duration_ms).sort((a,b) => a-b);
  return {count: values.length, failed: rows.length - values.length, p50: percentile(values,.5), p90: percentile(values,.9)};
}
function grouped(rows, key) {
  const groups = new Map();
  for (const row of rows) {
    const value = key(row);
    if (!groups.has(value)) groups.set(value, []);
    groups.get(value).push(row);
  }
  return [...groups].map(([label,values]) => ({label,...summarize(values)}));
}
function setTable(id, groups) {
  const body = byId(id);
  body.replaceChildren();
  for (const group of groups) {
    const tr = document.createElement("tr");
    for (const value of [group.label, fmtCount(group.count), fmtDuration(group.p50), fmtDuration(group.p90), fmtCount(group.failed)]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    body.append(tr);
  }
  if (!groups.length) {
    const td = document.createElement("td"); td.colSpan = 5; td.textContent = "No observations in this selection.";
    const tr = document.createElement("tr"); tr.append(td); body.append(tr);
  }
}
function svgNode(tag, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key,value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}
function drawChart(groups) {
  const svg = byId("chart"); svg.replaceChildren();
  const points = groups.filter(group => group.count);
  if (!points.length) {
    svg.append(svgNode("text", {x:500,y:130,"text-anchor":"middle",fill:"#526574"}, "No handoff latency samples in this selection."));
    return;
  }
  const left=78, right=975, top=15, bottom=220;
  const max = Math.max(...points.map(point => point.p90), 1) * 1.12;
  const start = Date.parse(points[0].label.replace(" ","T") + (points[0].label.length === 10 ? "T00:00:00Z" : ":00:00Z"));
  const end = Date.parse(points[points.length-1].label.replace(" ","T") + (points[points.length-1].label.length === 10 ? "T00:00:00Z" : ":00:00Z"));
  const x = point => {
    const ts = Date.parse(point.label.replace(" ","T") + (point.label.length === 10 ? "T00:00:00Z" : ":00:00Z"));
    return start === end ? (left+right)/2 : left+(ts-start)/(end-start)*(right-left);
  };
  const y = value => bottom-value/max*(bottom-top);
  for (let i=0;i<=4;i++) {
    const value=max*i/4;
    svg.append(svgNode("line",{x1:left,y1:y(value),x2:right,y2:y(value),stroke:"#dbe3e8"}));
    svg.append(svgNode("text",{x:left-10,y:y(value)+4,"text-anchor":"end",fill:"#526574","font-size":12},fmtDuration(value)));
  }
  for (const [field,color] of [["p50","#087e8b"],["p90","#ba4b18"]]) {
    if (points.length>1) svg.append(svgNode("polyline",{points:points.map(p=>`${x(p)},${y(p[field])}`).join(" "),fill:"none",stroke:color,"stroke-width":2}));
    for (const point of points) {
      const circle=svgNode("circle",{cx:x(point),cy:y(point[field]),r:4,fill:color});
      circle.append(svgNode("title",{},`${point.label} UTC · ${field}: ${fmtDuration(point[field])} · n=${point.count}`)); svg.append(circle);
    }
  }
  const labels = points.length>1 ? [points[0],points[points.length-1]] : [points[0]];
  labels.forEach((point,index)=>svg.append(svgNode("text",{x:x(point),y:250,"text-anchor":labels.length===1?"middle":index?"end":"start",fill:"#526574","font-size":12},point.label+" UTC")));
}
function update() {
  const from=byId("from").value, through=byId("through").value;
  const selected=records.filter(row=> {
    const day=row.started_at.slice(0,10);
    if ((from && day<from) || (through && day>through)) return false;
    return filterIds.slice(2).every(key=> {
      const wanted=byId(key).value;
      return !wanted || wanted === String(row[key] === null ? "unknown" : row[key]);
    });
  });
  const stats=summarize(selected);
  byId("p50").textContent=fmtDuration(stats.p50); byId("p90").textContent=fmtDuration(stats.p90);
  byId("count").textContent=fmtCount(stats.count); byId("failed").textContent=fmtCount(stats.failed);
  byId("selection").textContent=`${fmtCount(selected.length)} of ${fmtCount(records.length)} recorded launch observations selected.`;
  byId("empty").hidden=records.length>0;
  byId("sample-note").hidden=!stats.count || stats.count>=20;
  byId("sample-note").textContent=`Only ${fmtCount(stats.count)} handoff sample${stats.count===1?"":"s"} in this selection. Treat p90 as preliminary until more launches are recorded.`;
  const hourly=byId("bucket").value==="hour";
  const trend=grouped(selected,row=>row.started_at.slice(0,hourly?13:10).replace("T"," ")).sort((a,b)=>a.label.localeCompare(b.label));
  drawChart(trend); setTable("trend-table",trend);
  setTable("agent-table",grouped(selected,row=>row.agent).sort((a,b)=>b.count-a.count||a.label.localeCompare(b.label)));
  setTable("version-table",grouped(selected,row=>row.ug_version).sort((a,b)=>b.count-a.count||a.label.localeCompare(b.label)));
}
for (const key of ["agent","ug_version","os"]) {
  for (const value of [...new Set(records.map(row=>row[key]))].sort()) {
    const option=document.createElement("option"); option.value=value; option.textContent=value; byId(key).append(option);
  }
}
for (const key of [...filterIds,"bucket"]) byId(key).addEventListener("change",update);
byId("reset").addEventListener("click",()=> { for (const key of filterIds) byId(key).value=""; update(); });
byId("quality").textContent=`Input quality: ${fmtCount(data.ignored)} invalid or unsupported rows ignored; ${fmtCount(data.duplicates)} duplicate event IDs replaced by their latest valid row. Missing dimensions appear as unknown.`;
byId("generated").textContent=`Generated ${data.generated_at} · Static offline snapshot; regenerate after recording more launches.`;
update();
</script>
</body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("~/.ucode/launch-latency.jsonl"),
        help="Local UG launch JSONL file (default: ~/.ucode/launch-latency.jsonl).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("launch-latency.html"),
        help="Offline HTML dashboard to write (default: launch-latency.html).",
    )
    args = parser.parse_args()
    source = args.input.expanduser()
    destination = args.output.expanduser()
    if source.resolve() == destination.resolve():
        parser.error("--output must differ from --input so launch records are preserved")
    try:
        records, ignored, duplicates = load_records(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            render_dashboard(records, ignored=ignored, duplicates=duplicates), encoding="utf-8"
        )
    except OSError as exc:
        parser.exit(1, f"Could not build launch dashboard: {exc}\n")
    print(
        f"Wrote {destination} with {len(records)} launch observations "
        f"({ignored} invalid rows ignored, {duplicates} duplicate IDs replaced)."
    )


if __name__ == "__main__":
    main()
