"""Exercise the dashboard rename dialog with its browser boundary stubbed."""
import json
import shutil
import subprocess

import pytest

from mnemosyne_fleet.dashboard import DASHBOARD_HTML


def test_dashboard_rename_submission_cancel_and_conflict():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the dashboard interaction check")
    script = DASHBOARD_HTML.split('<script nonce="__CSP_NONCE__">', 1)[1].split('</script>', 1)[0]
    functions = '\n'.join(line for line in script.splitlines() if line.startswith(("async function renameModel(", "async function submitRename(")))
    result = subprocess.run([node, "-"], input='''
const assert = require('node:assert/strict');
const vm = require('node:vm');
new vm.Script(''' + json.dumps(script) + ''');
let calls = [], refreshed = 0, error;
const state = {};
const fields = {'#renameInput':{value:'',focus(){},select(){}}, '#renameError':{textContent:''}, '#catalogAction':{textContent:''}, '#renameDialog':{open:false,showModal(){this.open=true},close(){this.open=false}}};
const document = {querySelector: (id) => fields[id]};
async function api(path, options) { calls.push({path, options}); if(error) throw new Error(error); }
async function refreshStatus() { refreshed++; }
''' + functions + '''
(async () => {
 await renameModel('qwen-2', 'sha256:abc');
 assert.equal(fields['#renameInput'].value,'qwen-2');
 assert.equal(fields['#renameDialog'].open,true);
 assert.equal(calls.length,0);
 // An unchanged name closes without mutation.
 await submitRename();
 assert.equal(calls.length,0);
 assert.equal(fields['#renameDialog'].open,false);
 await renameModel('qwen-2','sha256:abc');
 fields['#renameInput'].value='qwen';
 error = 'model_catalog_mapping_conflict';
 await assert.rejects(submitRename(), /already in use/);
 assert.equal(fields['#renameDialog'].open,true);
 assert.equal(refreshed,0);
 error = 'model_mapping_in_use';
 await assert.rejects(submitRename(), /active or queued/);
 error = null;
 await submitRename();
 assert.deepEqual(calls.at(-1).options.json, {schema_version:1, public_model:'qwen-2', new_public_model:'qwen', deployment_id:'sha256:abc'});
 assert.equal(calls.at(-1).path,'/fleet/api/model-catalog/rename');
 assert.equal(fields['#renameDialog'].open,false);
 assert.equal(refreshed,1);
 assert.match(fields['#catalogAction'].textContent,/Update clients/);
})().catch(e => { console.error(e); process.exitCode=1; });
''', text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
