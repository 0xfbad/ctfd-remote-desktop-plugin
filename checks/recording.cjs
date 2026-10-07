const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../src/templates/remote_desktop_dashboard.html'), 'utf8');
const source = template.slice(template.indexOf('const recordingStates ='), template.indexOf('function killContainer('));
let checks = 0;
for (const [status, label] of [
    ['recording', 'Command recording available'],
    ['unavailable', 'Command recording unavailable'],
    ['discontinuous', 'Command history incomplete'],
    ['unknown', 'Command recording not checked'],
    [undefined, 'Command recording not checked'],
    ['unexpected', 'Command recording not checked'],
    ['__proto__', 'Command recording not checked'],
    ['constructor', 'Command recording not checked'],
]) {
    const tbody = {innerHTML: ''};
    const context = vm.createContext({
        document: {getElementById: () => tbody}, activeContainers: {},
        userLink: () => 'fixture', formatTime: () => '30:00', window: {init: {urlRoot: ''}},
    });
    vm.runInContext(source, context);
    context.updateContainersTable([{
        user_id: 7, container_name: 'fixture', docker_context: 'local', paused: false,
        timer: {active: true, time_remaining: 1800, extensions_used: 0, max_extensions: 3},
        recording_status: status,
    }]);
    assert.ok(tbody.innerHTML.includes(`title="${label}" aria-label="${label}"`));
    assert.equal(tbody.innerHTML.includes('undefined'), false);
    assert.equal((tbody.innerHTML.match(/<td/g) || []).length, 7);
    checks += 3;
}
console.log(JSON.stringify({checks, pass: true}));
