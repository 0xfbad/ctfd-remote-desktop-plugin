const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../src/templates/remote_desktop.html'), 'utf8');
const source = template.slice(template.indexOf('const creationPoll ='), template.indexOf('function addTime()')) +
    template.slice(template.indexOf("window.addEventListener('pagehide'"), template.indexOf("document.addEventListener('DOMContentLoaded'"));
const session = {created_at: 123, vnc_url: '/desktop', timer: null};
let checks = 0;

function runtime(fetch) {
    const start = {disabled: false, innerHTML: '', append(node) { this.text = node; }};
    const stop = {disabled: true, innerHTML: ''};
    const elements = {'start-btn': start, 'stop-btn': stop, 'vnc-frame': {}};
    const timers = new Map();
    const errors = [];
    const events = new Map();
    let id = 0;
    const context = vm.createContext({
        fetch, AbortController, FormData, Promise,
        document: {
            getElementById: key => elements[key],
            createTextNode: text => text,
        },
        window: {init: {csrfNonce: 'test'}},
        confirm: () => true,
        console: {error: (...args) => errors.push(args)},
        sessionTimer: null, timeRemaining: 0, continuationsUsed: 0,
        maxContinuations: 3, expirationWarningShown: false,
        reloads: 0, messages: [],
        reloadPage() { context.reloads++; },
        showSessionError(message) { context.messages.push(message); },
        showStatusMessage() {}, hideSessionError() {}, updateTimerDisplay() {}, updateAddTimeButton() {},
        updateCountdown() {},
        setTimeout: (callback, delay) => { timers.set(++id, {callback, delay}); return id; },
        setInterval: (callback, delay) => { timers.set(++id, {callback, delay, interval: true}); return id; },
        clearTimeout: key => timers.delete(key),
        clearInterval: key => timers.delete(key),
    });
    context.window.addEventListener = (event, callback) => events.set(event, callback);
    vm.runInContext(source, context);
    return {context, start, stop, timers, errors, events};
}

const response = (data, ok = true) => ({ok, status: ok ? 200 : 503, headers: {get: () => null}, json: async () => data});
const settle = async () => { await new Promise(resolve => setImmediate(resolve)); };
function check(condition, message) { assert.ok(condition, message); checks++; }

async function main() {
    for (const data of [{}, [], null, {session: {}}, {session: false}, {session: {...session, timer: {active: true}}}]) {
        const r = runtime(async () => response(data));
        r.context.syncWithServer();
        await settle();
        check(r.context.reloads === 0 && r.errors.length === 1, 'malformed status cannot expire the session');
    }
    for (const result of [response({session: null}, false), {ok: true, json: async () => { throw Error('html'); }}]) {
        const r = runtime(async () => result);
        r.context.syncWithServer();
        await settle();
        check(r.context.reloads === 0 && r.errors.length === 1, 'failed request cannot expire the session');
    }
    {
        const r = runtime(async () => response({session: null}));
        r.context.syncWithServer();
        await settle();
        r.context.syncWithServer();
        await settle();
        check(r.context.reloads === 1, 'explicit absence reloads once');
    }
    {
        const r = runtime(async () => response({session}));
        for (let i = 0; i < 120; i++) { r.context.syncWithServer(); await settle(); }
        check(r.context.reloads === 0 && r.errors.length === 0, 'a present session without timer remains present');
    }
    {
        const timer = {active: true, time_remaining: 321, extensions_used: 1, max_extensions: 3};
        const r = runtime(async () => response({session: {...session, timer}}));
        r.context.syncWithServer();
        await settle();
        check(r.context.timeRemaining === 321 && !r.stop.disabled, 'valid timers update controls');
    }
    {
        let resolve;
        let calls = 0;
        let signal;
        const r = runtime((_path, options) => {
            calls++; signal = options.signal;
            return new Promise(done => { resolve = done; });
        });
        for (let i = 0; i < 100; i++) r.context.syncWithServer();
        await settle();
        check(calls === 1, 'only one status request can be pending');
        r.context.cancelPolling();
        check(signal.aborted, 'cancellation aborts the outstanding request');
        resolve(response({session: null}));
        await settle();
        check(r.context.reloads === 0, 'cancelled late absence cannot reload');
    }
    for (const status of ['none', 'queued', 'selecting', 'reserved', 'creating', 'waiting_ready', 'cancel_requested', 'cleanup_pending', 'selecting_host', 'starting_container', 'waiting_vnc']) {
        const r = runtime(async () => response({status, message: '<img src=x onerror=alert(1)>'}));
        r.context.pollCreationStatus();
        await settle();
        check(r.errors.length === 0 && r.start.text.includes('<img'), 'all current creation states use text nodes');
    }
    for (const data of [{status: 'ready', session: {}}, {status: 'unexpected'}, {status: 'creating', message: {}}]) {
        const r = runtime(async () => response(data));
        r.context.pollCreationStatus();
        await settle();
        check(r.context.reloads === 0 && r.errors.length === 1, 'invalid creation replies cannot schedule navigation');
    }
    {
        let resolve;
        let signal;
        let calls = 0;
        const r = runtime((_path, options) => {
            calls++; signal = options.signal;
            return new Promise(done => { resolve = done; });
        });
        r.start.disabled = true;
        r.context.pollCreationStatus();
        await settle();
        for (let i = 0; i < 100; i++) r.context.checkStatus();
        await settle();
        check(calls === 1, 'only one readiness request can be pending');
        const timeout = [...r.timers.values()].find(timer => timer.delay === 120000);
        check(Boolean(timeout), 'the unchanged readiness deadline has its own timer');
        timeout.callback();
        check(signal.aborted && !r.start.disabled && r.context.messages.length === 1, 'deadline stops a hung request');
        resolve(response({status: 'ready', session}));
        await settle();
        check(r.context.reloads === 0 && ![...r.timers.values()].some(timer => timer.delay === 1000), 'late ready cannot schedule navigation');
    }
    for (const cancel of ['cancelPolling', 'startSession', 'stopSession', 'pagehide']) {
        let calls = 0;
        const r = runtime(async () => response(++calls === 1 ? {status: 'ready', session} : {status: 'creating'}));
        r.context.pollCreationStatus();
        await settle();
        const ready = [...r.timers.values()].find(timer => timer.delay === 1000);
        check(Boolean(ready), 'valid readiness schedules navigation');
        if (cancel === 'pagehide') r.events.get('pagehide')?.();
        else r.context[cancel]();
        ready.callback();
        check(r.context.reloads === 0, 'lifecycle cancellation fences delayed navigation');
    }
    {
        let calls = 0;
        const r = runtime(async () => { calls++; return response({session}); });
        r.context.syncWithServer();
        await settle();
        r.events.get('pagehide')();
        r.events.get('pageshow')({persisted: true});
        await settle();
        check(calls === 2 && r.context.reloads === 0, 'a restored desktop resumes status polling');
    }
    {
        let calls = 0;
        const r = runtime(async () => { calls++; return response({status: 'creating'}); });
        r.context.pollCreationStatus();
        await settle();
        r.events.get('pagehide')();
        r.events.get('pageshow')({persisted: true});
        await settle();
        check(calls === 2 && r.context.reloads === 0, 'restored readiness resumes GET polling without a create');
    }
    {
        let calls = 0;
        const r = runtime(async () => { calls++; return response({status: 'creating'}); });
        for (let i = 0; i < 1000; i++) r.context.startSession();
        await settle();
        check(calls === 2, 'repeated Start clicks send one create and one readiness request');
    }
    console.log(JSON.stringify({checks, pass: true}));
}

main().catch(error => { console.error(error); process.exitCode = 1; });
