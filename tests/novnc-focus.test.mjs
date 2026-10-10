import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const models = readFileSync(new URL('../src/models.py', import.meta.url), 'utf8');
const version = models.match(/NOVNC_VERSION = "([a-f0-9]+)"/)[1];
const source = readFileSync(process.env.NOVNC_UI_SOURCE || new URL(`../src/static/novnc/${version}/app/ui.js`, import.meta.url), 'utf8');

function classList(initial = []) {
    const values = new Set(initial);
    return {
        add: value => values.add(value),
        remove: value => values.delete(value),
        contains: value => values.has(value),
    };
}

function fixture({owner = 'frame', inner = 'body', hidden = false, visible = true, focused = true, standalone = false} = {}) {
    const calls = [];
    const timers = [];
    const body = {tagName: 'BODY'};
    const parentBody = {tagName: 'BODY'};
    const canvas = {tagName: 'CANVAS'};
    const input = {tagName: 'INPUT'};
    const field = {tagName: 'TEXTAREA'};
    const button = {tagName: 'BUTTON'};
    const elements = new Map();
    const frame = {
        id: 'vnc-frame',
        classList: classList(hidden ? ['hidden'] : []),
        getClientRects: () => visible && !frame.classList.contains('hidden') ? [{}] : [],
    };
    const parentDocument = {body: parentBody, activeElement: {report: field, button, body: parentBody, frame}[owner], hasFocus: () => focused};
    const document = {
        body,
        activeElement: {body, canvas, input, field, button}[inner],
        hasFocus: () => focused,
        documentElement: {classList: classList()},
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, {classList: classList(), textContent: ''});
            return elements.get(id);
        },
    };
    const context = {
        document,
        window: {frameElement: standalone ? null : frame},
        parent: {document: parentDocument},
        console,
        _: value => value,
        setTimeout(callback, delay) {
            timers.push({callback, delay});
            return timers.length;
        },
        clearTimeout() {},
    };
    const script = source.replace(/^import[\s\S]*?;\n/gm, '').replace('export default UI;', 'globalThis.UI = UI;');
    runInNewContext(script, context);
    const UI = context.UI;
    UI.rfb = {
        _canvas: canvas,
        focus() {
            calls.push('focus');
            document.activeElement = canvas;
            parentDocument.activeElement = frame;
        },
    };
    UI.getSetting = () => false;
    UI.showStatus = () => calls.push('status');
    UI.updateViewClip = () => {};
    UI.disableSetting = () => {};
    UI.enableSetting = () => {};
    UI.closeAllPanels = () => {
        calls.push('close-panels');
        if (document.activeElement === input || document.activeElement === field || document.activeElement === button) document.activeElement = body;
    };
    return {UI, document, parentDocument, calls, timers, canvas, frame};
}

for (const reconnect of [false, true]) {
    test(`${reconnect ? 'reconnect' : 'first connect'} preserves report and keyboard mode controls`, () => {
        for (const owner of ['report', 'button']) {
            const f = fixture({owner});
            f.UI.connected = reconnect;
            const active = f.parentDocument.activeElement;
            f.UI.connectFinished({});
            assert.equal(f.UI.connected, true);
            assert.equal(f.UI.inhibitReconnect, false);
            assert.equal(f.parentDocument.activeElement, active);
            assert.ok(!f.calls.includes('focus'));
        }
    });
}

test('connection focuses the reserved frame, idle page, and standalone client', () => {
    for (const options of [{}, {owner: 'body'}, {inner: 'canvas'}, {standalone: true, owner: 'report'}]) {
        const f = fixture(options);
        f.UI.connectFinished({});
        assert.equal(f.document.activeElement, f.canvas);
        assert.equal(f.calls.filter(call => call === 'focus').length, 1);
    }
});

test('connection does not focus hidden, collapsed, or background workspace frames', () => {
    for (const options of [{hidden: true}, {visible: false}, {focused: false}]) {
        const f = fixture(options);
        f.UI.connectFinished({});
        assert.ok(!f.calls.includes('focus'));
    }
});

test('connection snapshots local control ownership before visual state closes panels', () => {
    for (const inner of ['input', 'field', 'button']) {
        const f = fixture({inner});
        f.UI.connectFinished({});
        assert.ok(f.calls.includes('close-panels'));
        assert.ok(!f.calls.includes('focus'));
    }
});

test('automatic controlbar closure checks ownership and visibility when its timer fires', () => {
    for (const target of ['report', 'field', 'hidden', 'collapsed', 'background']) {
        const f = fixture();
        f.UI.connectFinished({});
        f.calls.length = 0;
        if (target === 'report') f.parentDocument.activeElement = {tagName: 'TEXTAREA'};
        if (target === 'field') f.document.activeElement = {tagName: 'TEXTAREA'};
        if (target === 'hidden') f.frame.classList.add('hidden');
        if (target === 'collapsed') f.frame.getClientRects = () => [];
        if (target === 'background') f.parentDocument.hasFocus = () => false;
        assert.equal(f.timers.length, 1);
        assert.equal(f.timers[0].delay, 2000);
        f.timers[0].callback();
        assert.deepEqual(f.calls, []);
    }
});

test('automatic controlbar closure focuses the owned canvas and standalone client', () => {
    for (const options of [{}, {standalone: true, owner: 'report'}]) {
        const f = fixture(options);
        f.UI.connectFinished({});
        f.calls.length = 0;
        f.timers[0].callback();
        assert.deepEqual(f.calls, ['close-panels', 'focus']);
    }
});

test('explicit controlbar close keeps the vendor focus behavior', () => {
    const f = fixture({inner: 'field'});
    f.UI.closeControlbar();
    assert.deepEqual(f.calls, ['close-panels', 'focus']);
});
