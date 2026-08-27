from __future__ import annotations


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mnemosyne Fleet</title>
  <style nonce="__CSP_NONCE__">
    :root { color-scheme:dark; --ink:#edf4f1; --muted:#90a49d; --panel:#111d1a; --line:#27443b; --ok:#63e6a5; --bad:#ff7a82 }
    * { box-sizing:border-box } body { margin:0; background:#07100e; color:var(--ink); font:15px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace }
    main { width:min(1500px,94vw); margin:0 auto; padding:32px 0 64px }
    header,.section-title { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:end; gap:18px }
    header { margin-bottom:24px } h1 { margin:0; font:600 clamp(28px,5vw,54px)/1 system-ui,sans-serif; letter-spacing:-.04em }
    h2 { font:600 17px system-ui,sans-serif; margin:0 0 14px }.eyebrow { color:var(--ok); text-transform:uppercase; letter-spacing:.14em; font-size:12px }
    .auth { display:flex; gap:8px; align-items:center } input,button,select { border:1px solid var(--line); border-radius:6px; background:#0b1714; color:var(--ink); padding:10px 12px; font:inherit }
    button { cursor:pointer } button:focus,input:focus,select:focus { outline:2px solid var(--ok); outline-offset:2px }
    .summary,.nodes { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; margin-bottom:20px }
    .card,.section { border:1px solid var(--line); background:var(--panel); border-radius:10px; padding:16px }
    .metric { font:600 32px/1 system-ui,sans-serif; margin-top:8px }.label,.muted { color:var(--muted) }.ok { color:var(--ok) }.bad { color:var(--bad) }
    .section { margin-top:14px; overflow:auto }.section-title { margin-bottom:10px }.section-title h2 { margin:0 }
    table { width:100%; border-collapse:collapse; min-width:700px } th,td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line) } th { color:var(--muted); font-weight:500 }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 7px; font-size:12px }
    .stack { display:grid; gap:5px }.candidate { padding:5px 0;border-bottom:1px dotted var(--line) }.candidate:last-child { border-bottom:0 }
    .digest { display:block; max-width:560px; overflow-wrap:anywhere; white-space:normal }.status-line { margin-top:8px }
    #error { min-height:24px;color:var(--bad) }
    @media(max-width:600px){ main{width:92vw;padding-top:20px}.auth{width:100%}.auth input{min-width:0;flex:1} }
  </style>
</head>
<body><main>
  <header><div><div class="eyebrow">Nyx control plane</div><h1>Mnemosyne Fleet</h1></div>
    <form class="auth" id="auth"><label class="muted" for="key">Admin key</label><input id="key" type="password" autocomplete="current-password"><button>Connect</button></form>
  </header>
  <div id="error" role="status" aria-live="polite"></div>
  <section class="summary" id="summary" aria-label="Fleet summary"></section>
  <h2>Inference nodes</h2><section class="nodes" id="nodes"></section>
  <section class="section"><h2>Strict model deployment matrix</h2><table><thead><tr><th>Public model / deployment</th><th>Capabilities</th><th>Replicas</th><th>All enrolled candidates</th><th>Fleet queue</th></tr></thead><tbody id="models"></tbody></table></section>
  <section class="section"><h2>Discovered node model inventory</h2><table><thead><tr><th>Node / alias</th><th>Engine / upstream</th><th>Deployment ID</th><th>Capabilities</th><th>Identity / availability</th><th>Capacity</th></tr></thead><tbody id="inventory"></tbody></table></section>
  <section class="section">
    <div class="section-title"><h2>Historical token usage</h2><label class="muted" for="hours">Window <select id="hours"><option value="24">24 hours</option><option value="168">7 days</option></select></label></div>
    <table><thead><tr><th>Node</th><th>Public / node model</th><th>Requests</th><th>Prompt</th><th>Completion</th><th>Total tokens</th><th>Avg latency</th></tr></thead><tbody id="usage"></tbody></table>
  </section>
  <section class="section"><h2>Recent routes (metadata only)</h2><table><thead><tr><th>Started</th><th>Model</th><th>Node</th><th>Endpoint</th><th>Status</th><th>Latency</th></tr></thead><tbody id="routes"></tbody></table></section>
</main><script nonce="__CSP_NONCE__">
const esc=value=>String(value??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const adminKey=()=>sessionStorage.getItem("fleetAdminKey")||"";
const authHeaders=()=>({Authorization:`Bearer ${adminKey()}`});
let streamController;
const delay=milliseconds=>new Promise(resolve=>setTimeout(resolve,milliseconds));
const yesNo=value=>value==null?"—":value?"yes":"no";
const seen=value=>value==null?"never":new Date(value*1000).toLocaleString();
const capacityText=capacity=>capacity?`${esc(capacity.active)} active · ${esc(capacity.queued)} queued · ${esc(capacity.effective_limit)} effective (${esc(capacity.derived_limit)} derived${capacity.configured_max_concurrency==null?"":` · max ${esc(capacity.configured_max_concurrency)}`})`:"—";
function render(data){
  document.querySelector("#error").textContent="";
  const online=data.nodes.filter(node=>node.online).length;
  const capacity=data.nodes.reduce((total,node)=>total+(node.online?node.capacity?.effective_limit||0:0),0);
  document.querySelector("#summary").innerHTML=[
    ["Online nodes",`${online} / ${data.nodes.length}`],["Effective capacity",capacity],["Active routes",data.scheduler.active_total],
    ["Queued requests",Object.values(data.scheduler.queues).reduce((total,queue)=>total+queue.depth,0)]
  ].map(([label,value])=>`<div class="card"><div class="label">${esc(label)}</div><div class="metric">${esc(value)}</div></div>`).join("");
  document.querySelector("#nodes").innerHTML=data.nodes.map(node=>`<article class="card"><div class="eyebrow ${node.online?"ok":"bad"}">${node.online?"online":"offline"}</div><h2>${esc(node.node_id)}</h2>
    <div>${esc(node.platform)} ${esc(node.version)} · ${esc(node.health?.state)}</div>
    <div class="muted">Last seen: ${esc(seen(node.last_seen))}${node.error_code?` · ${esc(node.error_code)}`:""}</div>
    <div class="muted">Accepting: ${esc(yesNo(node.health?.accepting))} · authoritative: ${esc(yesNo(node.health?.authoritative))}${node.health?.diagnostic_code?` · ${esc(node.health.diagnostic_code)}`:""}</div>
    <div class="status-line">Resident: ${esc(node.residency?.engine)} / ${esc(node.residency?.alias)} · epoch ${esc(node.residency?.epoch)}</div>
    <div class="muted">Transition: ${esc(node.residency?.transition_target)}</div>
    <div class="status-line">${capacityText(node.capacity)}</div>
    <div class="muted">Source: ${esc(node.capacity?.source)} · ${esc(node.capacity?.confidence)} · node queue ${esc(node.admission?.queue_depth)} / ${esc(node.admission?.queue_limit)}</div>
    <div class="status-line">Usage enabled: ${esc(yesNo(node.usage_delivery?.enabled))} · writer ${node.usage_delivery==null?'<span class="muted">unknown</span>':node.usage_delivery.writer_ready?'<span class="ok">ready</span>':'<span class="bad">not ready</span>'} · outbox ${esc(node.usage_delivery?.outbox_pending)}</div>
    <div class="muted">Last flush: ${esc(seen(node.usage_delivery?.last_flush_at))}${node.usage_delivery?.last_error_code?` · ${esc(node.usage_delivery.last_error_code)}`:""}</div></article>`).join("");
  document.querySelector("#models").innerHTML=data.models.map(model=>`<tr><td><strong>${esc(model.name)}</strong><code class="digest">${esc(model.deployment_id)}</code></td><td>${model.capabilities.map(capability=>`<span class="pill">${esc(capability)}</span>`).join(" ")}</td>
    <td>${esc(model.eligible_replica_count)} eligible / ${esc(model.online_strict_replica_count)} online strict / ${esc(model.strict_replica_count)} known strict</td>
    <td><div class="stack">${model.nodes.map(node=>`<div class="candidate"><strong>${esc(node.node_id)}</strong>${node.warm?" · warm":""}${node.aliases?.length?` · ${node.aliases.map(esc).join(", ")}`:""}<br><span class="${node.eligible?"ok":"bad"}">${node.eligible?"eligible":esc((node.reason_codes||[]).join(", ")||"ineligible")}</span>${node.snapshot_error_code?` <span class="muted">(${esc(node.snapshot_error_code)})</span>`:""}</div>`).join("")}</div></td>
    <td>${esc(data.scheduler.queues[model.name]?.depth)} / ${esc(data.scheduler.queues[model.name]?.limit)}</td></tr>`).join("");
  const inventory=data.nodes.flatMap(node=>(node.deployments||[]).map(deployment=>({node,deployment})));
  document.querySelector("#inventory").innerHTML=inventory.length?inventory.map(({node,deployment})=>`<tr>
    <td><strong>${esc(node.node_id)}</strong><br>${esc(deployment.alias)}${deployment.warm?' <span class="pill">warm</span>':""}${node.online?"":' <span class="bad">stale</span>'}</td>
    <td>${esc(deployment.engine)}<br><span class="muted">${esc(deployment.upstream_model)} · ${esc(deployment.artifact?.format)}${deployment.artifact?.quantization?` · ${esc(deployment.artifact.quantization)}`:""}</span></td>
    <td><code class="digest">${esc(deployment.deployment_id)}</code></td>
    <td>${deployment.capabilities.map(capability=>`<span class="pill">${esc(capability)}</span>`).join(" ")}</td>
    <td>${esc(deployment.identity_confidence)} · fleet ${esc(yesNo(deployment.fleet_eligible))} · loadable ${esc(yesNo(deployment.loadable))}<br><span class="muted">revision ${esc(deployment.resolved_revision)}</span></td>
    <td>${capacityText(deployment.capacity)}<br><span class="muted">${esc(deployment.capacity?.source)} · ${esc(deployment.capacity?.confidence)}</span></td></tr>`).join(""):`<tr><td colspan="6" class="muted">No authenticated node inventories have been received.</td></tr>`;
  document.querySelector("#routes").innerHTML=(data.routes||[]).map(route=>`<tr><td>${new Date(route.started_at*1000).toLocaleString()}</td><td>${esc(route.public_model)}</td><td>${esc(route.node_id)}</td><td>${esc(route.endpoint)}</td>
    <td>${esc(route.status_code||route.failure_code||"active")}</td><td>${route.response_ms==null?"—":esc(Math.round(route.response_ms)+" ms")}</td></tr>`).join("");
}
async function refreshStatus(){
  if(!adminKey())return;
  const response=await fetch("/fleet/api/status",{headers:authHeaders(),cache:"no-store"});
  if(!response.ok)throw new Error(response.status===401?"Admin key rejected":`Status ${response.status}`);
  render(await response.json());
}
async function refreshUsage(){
  if(!adminKey())return;
  const hours=document.querySelector("#hours").value;
  const response=await fetch(`/fleet/api/usage?hours=${hours}`,{headers:authHeaders(),cache:"no-store"});
  if(!response.ok)throw new Error(response.status===503?"Token ledger unavailable":`Usage ${response.status}`);
  const data=await response.json();
  document.querySelector("#usage").innerHTML=data.configured?data.rows.map(row=>`<tr><td>${esc(row.node_id)}</td><td><strong>${esc((row.public_models||[]).join(", ")||"unmapped")}</strong><br><span class="muted">${esc(row.model)}</span></td><td>${esc(row.request_count)}</td>
    <td>${esc(row.prompt_tokens)}</td><td>${esc(row.completion_tokens)}</td><td>${esc(row.total_tokens)}</td><td>${esc(Math.round(row.avg_response_ms)+" ms")}</td></tr>`).join(""):`<tr><td colspan="7" class="muted">Read-only token ledger is not configured.</td></tr>`;
}
async function consumeFleetStream(){
  if(streamController)streamController.abort();
  streamController=new AbortController();
  const ownController=streamController;
  while(adminKey()&&ownController===streamController){
    try{
      const response=await fetch("/fleet/api/events",{headers:authHeaders(),cache:"no-store",signal:ownController.signal});
      if(!response.ok)throw new Error(response.status===401?"Admin key rejected":`Stream ${response.status}`);
      const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";
      while(true){
        const part=await reader.read();if(part.done)break;buffer+=decoder.decode(part.value,{stream:true});
        let boundary;while((boundary=buffer.indexOf("\\n\\n"))>=0){
          const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
          const dataLine=frame.split("\\n").find(line=>line.startsWith("data: "));
          if(dataLine)render(JSON.parse(dataLine.slice(6)));
        }
      }
    }catch(error){
      if(ownController.signal.aborted)return;
      document.querySelector("#error").textContent=error.message;
    }
    await delay(2000);
  }
}
async function connect(){
  try{await Promise.all([refreshStatus(),refreshUsage()]);consumeFleetStream()}catch(error){document.querySelector("#error").textContent=error.message}
}
document.querySelector("#auth").addEventListener("submit",event=>{event.preventDefault();sessionStorage.setItem("fleetAdminKey",document.querySelector("#key").value);connect()});
document.querySelector("#hours").addEventListener("change",()=>refreshUsage().catch(error=>document.querySelector("#error").textContent=error.message));
connect();
</script></body></html>"""
