"""Browser-independent checks for dashboard state, filtering, and failures."""
import json
import shutil
import subprocess

import pytest
from mnemosyne_fleet.dashboard import DASHBOARD_HTML


def run_js(source):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for dashboard behavior checks')
    result = subprocess.run([node, '-'], input=source, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def function(name):
    script = DASHBOARD_HTML.split('<script nonce="__CSP_NONCE__">')[1].split('</script>')[0]
    start = script.index('function ' + name + '(')
    end = script.find('\nfunction ', start + 1)
    # Catalog is followed immediately by render(); connect by event wiring.
    if name == 'connect':
        start = script.index('async function connect(')
        end = script.index('\ndocument.querySelector("#universalCatalog")', start)
    return script[start:end]


def test_catalog_filter_search_and_untrusted_names():
    script = DASHBOARD_HTML.split('<script nonce="__CSP_NONCE__">')[1].split('</script>')[0]
    esc = next(line for line in script.splitlines() if line.startswith('const esc='))
    run_js('''
const assert=require('node:assert/strict');
const target={innerHTML:'',contains(){return false}};
const search={value:''}, counter={textContent:''};
const document={activeElement:null,querySelector:id=>({'#universalCatalog':target,'#modelSearch':search,'#modelCount':counter}[id])};
const glyph=()=>'',emptyState=(title,description)=>title+' '+description;
function updateRegion(target,html){target.innerHTML=html}
const state={latest:{models:[{name:'vision',eligible_replica_count:1,nodes:[{node_id:'athena',eligible:true,warm:true}]},{name:'<img src=x onerror=alert(1)>',nodes:[]}]},modelCatalog:{candidates:[],mappings:[{public_model:'vision',origin_alias:'local',removable:true,capabilities:['responses']},{public_model:'<img src=x onerror=alert(1)>',removable:false}]}};
''' + esc + '\n' + function('renderUniversalCatalog') + '''
renderUniversalCatalog();
assert.match(target.innerHTML,/1 replica ready/);
assert.match(target.innerHTML,/&lt;img/);
assert.ok(!target.innerHTML.includes('<img'));
state.modelFilter='ready';renderUniversalCatalog();
assert.ok(!target.innerHTML.includes('&lt;img'));
assert.equal(counter.textContent,'1 of 2 aliases');
search.value='athena';renderUniversalCatalog();
assert.match(target.innerHTML,/vision/);
state.modelFilter='unavailable';renderUniversalCatalog();
assert.match(target.innerHTML,/No matching models/);
search.value='';renderUniversalCatalog();
assert.match(target.innerHTML,/Unavailable/);
assert.ok(!target.innerHTML.includes('replica ready'));
''')


def test_ledger_failure_does_not_block_status_stream():
    run_js('''
const assert=require('node:assert/strict');
let streamed=false, usageError=null, connected=null;
let streamController=null;
const adminKey=()=> 'test';
const setConnection=(kind,label)=>{connected=kind};
const refreshStatus=async()=>({});
const refreshUsage=async()=>{throw new Error('Token ledger unavailable')};
const usageFailure=error=>{usageError=error.message};
const stream=()=>{streamed=true};
const document={querySelector:()=>({textContent:''})};
''' + function('connect') + '''
(async()=>{await connect();await Promise.resolve();assert.equal(connected,'live');assert.ok(streamed);assert.equal(usageError,'Token ledger unavailable')})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_template_is_self_contained_and_dialog_is_accessible():
    assert '<dialog id="renameDialog" aria-labelledby="renameTitle">' in DASHBOARD_HTML
    assert 'aria-label="Search models"' in DASHBOARD_HTML
    assert 'prefers-reduced-motion:reduce' in DASHBOARD_HTML
    assert '<script src=' not in DASHBOARD_HTML
    assert '<link rel="stylesheet"' not in DASHBOARD_HTML
    assert 'DESIGN PREVIEW' not in DASHBOARD_HTML


def test_node_activity_distinguishes_inference_assignment_loading_and_stale_data():
    run_js('''
const assert=require('node:assert/strict');
''' + function('fleetNodeActivity') + '''
const node={online:true,health:{state:'ready',accepting:true},capacity:{active:1,queued:2}};
let activity=fleetNodeActivity(node,{active_requests:1});
assert.equal(activity.label,'Inferencing');
assert.equal(activity.flow,true);
assert.equal(activity.queued,2);
// Local inference is visible but does not animate a fictitious Fleet route.
activity=fleetNodeActivity(node,{active_requests:0});
assert.equal(activity.working,true);
assert.equal(activity.flow,false);
node.health.state='loading';
assert.equal(fleetNodeActivity(node,{active_requests:1}).label,'Loading model');
assert.equal(fleetNodeActivity(node,{active_requests:1}).working,false);
node.health.state='ready';node.capacity.active=0;
assert.equal(fleetNodeActivity(node,{active_requests:1}).label,'Request assigned');
assert.equal(fleetNodeActivity(node,{}).label,'Idle');
node.health.accepting=false;
assert.equal(fleetNodeActivity(node,{joined_state:'paused'}).label,'Paused');
node.online=false;node.capacity.active=2;
activity=fleetNodeActivity(node,{active_requests:2});
assert.equal(activity.label,'Offline');
assert.equal(activity.flow,false);
assert.equal(activity.working,false);
assert.equal(activity.active,0);
assert.equal(activity.fleetActive,0);
assert.equal(activity.queued,0);
''')


def test_fleet_map_works_without_optional_management_and_joins_exact_enrollment():
    script = DASHBOARD_HTML.split('<script nonce="__CSP_NONCE__">')[1].split('</script>')[0]
    esc = next(line for line in script.splitlines() if line.startswith('const esc='))
    run_js('''
const assert=require('node:assert/strict');
const target={innerHTML:'',classList:{toggle(){}}},summary={textContent:''},hub={textContent:''};
const document={querySelector:id=>({'#fleetNetwork':target,'#networkSummary':summary,'#networkHubActivity':hub}[id])};
const glyph=()=>'',emptyState=(title,description)=>title+' '+description;
function updateRegion(target,html){target.innerHTML=html}
''' + esc + '\n' + function('fleetNodeActivity') + function('renderFleetNetwork') + '''
const nodes=[{node_id:'<img src=x onerror=alert(1)>',enrollment_id:'first',online:true,health:{state:'ready',accepting:true},residency:{alias:'<script>bad</script>'},capacity:{active:1}},
{node_id:'offline',enrollment_id:'second',online:false,capacity:{active:20}}];
const data={nodes,overview:{nodes:[{node_id:nodes[0].node_id,enrollment_id:'other',active_requests:99},{node_id:nodes[0].node_id,enrollment_id:'first',active_requests:1}]},scheduler:{active_total:1},mac_pool:{}};
renderFleetNetwork(data);
assert.equal(summary.textContent,'1 of 2 connected · 1 running inference');
assert.equal(hub.textContent,'1 active request');
assert.match(target.innerHTML,/&lt;img/);
assert.match(target.innerHTML,/&lt;script&gt;/);
assert.ok(!target.innerHTML.includes('<img'));
assert.ok(!target.innerHTML.includes('99'));
assert.match(target.innerHTML,/Last known resident/);
assert.ok(target.innerHTML.includes('>—</strong>'));
assert.equal((target.innerHTML.match(/ flow /g)||[]).length,1);
renderFleetNetwork({nodes:[],scheduler:{active_total:0}});
assert.match(target.innerHTML,/first Mac/);
assert.equal(summary.textContent,'0 of 0 connected · 0 running inference');
''')


def test_stream_eof_marks_activity_stale_before_reconnecting():
    script = DASHBOARD_HTML.split('<script nonce="__CSP_NONCE__">')[1].split('</script>')[0]
    stream = script.split('async function stream(){')[1].split('\nasync function connect')[0]
    run_js('''
const assert=require('node:assert/strict');
let streamController=null,connection=null;
const adminKey=()=> 'test',authHeaders=()=>({});
const setConnection=(kind)=>{connection=kind};
const fetch=async()=>({ok:true,body:{getReader:()=>({read:async()=>({done:true})})}});
const document={querySelector:()=>({textContent:''})};
const delay=async()=>{assert.equal(connection,'connecting');streamController=null};
async function stream(){''' + stream + '''
stream().then(()=>assert.equal(connection,'connecting')).catch(e=>{console.error(e);process.exitCode=1});
''')
    assert '.fleet-map:not([data-connection=live]) *{animation-play-state:paused!important}' in DASHBOARD_HTML
    assert 'activity below is from the last received snapshot' in DASHBOARD_HTML


def test_silent_stream_stops_showing_live_activity_after_fifteen_seconds():
    run_js('''
const assert=require('node:assert/strict');
const state={connection:'live',lastSnapshotAt:1000};
const setConnection=(kind)=>{state.connection=kind};
''' + function('checkFleetFreshness') + '''
checkFleetFreshness(15000);assert.equal(state.connection,'live');
checkFleetFreshness(17000);assert.equal(state.connection,'connecting');
state.connection='error';checkFleetFreshness(20000);assert.equal(state.connection,'error');
''')
