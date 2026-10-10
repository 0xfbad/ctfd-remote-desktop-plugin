import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const source = readFileSync(process.env.WORKSPACE_INPUT_SOURCE || new URL('../src/static/js/workspace-input.js', import.meta.url), 'utf8');

function fixture({missingFrame, missingClient, transitional = false} = {}) {
    const calls = [];
    const listeners = new Map();
    const frames = new Map();
    const term = {focus() { calls.push({target: 'terminal'}); }};
    const canvas = {focus(options) { calls.push({target: 'desktop', preventScroll: options?.preventScroll}); }};

    for (const [id, mode] of [['terminal-frame', 'terminal'], ['vnc-frame', 'desktop']]) {
        if (missingFrame === mode) continue;
        const page = {
            URL: transitional ? 'about:blank' : 'https://fixture/client',
            head: transitional ? null : {append() {}},
            getElementById() { return {}; },
            querySelector(selector) {
                assert.equal(selector, '#noVNC_container canvas');
                return missingClient === mode || transitional ? null : canvas;
            },
        };
        const frame = {
            focus(options) { calls.push({target: `${mode}-frame`, preventScroll: options?.preventScroll}); },
            contentWindow: {term: missingClient === mode || transitional ? undefined : term},
            contentDocument: page,
            addEventListener() {},
            get src() { return `https://fixture/${mode}`; },
            set src(_value) { assert.fail('Focus restoration must preserve the client document'); },
        };
        frames.set(id, frame);
    }

    const document = {
        getElementById: id => frames.get(id),
        addEventListener(name, callback) { listeners.set(name, callback); },
    };
    runInNewContext(source.replace('import.meta.url', "'https://fixture/static/js/workspace-input.js'"), {document, URL});

    function click(mode, {detail = 1, active = true} = {}) {
        const event = {
            detail,
            target: {
                closest(selector) {
                    return active && selector === '.mode-tab.active' && mode ? {dataset: {mode}} : null;
                },
            },
        };
        listeners.get('click')?.(event);
    }
    return {calls, click, frames, term, canvas};
}

test('pointer mode activation focuses the loaded client, including nested button content', () => {
    const f = fixture();
    f.click('terminal');
    f.click('desktop');
    assert.deepEqual(f.calls, [{target: 'terminal'}, {target: 'desktop', preventScroll: true}]);
});

test('keyboard and programmatic activation preserve navigation and other controls retain focus', () => {
    const f = fixture();
    for (const mode of ['terminal', 'desktop']) {
        f.click(mode, {detail: 0});
        f.click(mode, {active: false});
    }
    for (const mode of ['ssh', 'report', null]) f.click(mode);
    assert.deepEqual(f.calls, []);
});

test('pointer activation reserves a loading frame and missing frames remain safe', () => {
    for (const mode of ['terminal', 'desktop']) {
        for (const state of [{missingClient: mode}, {transitional: true}]) {
            const f = fixture(state);
            assert.doesNotThrow(() => f.click(mode));
            assert.deepEqual(f.calls, [{target: `${mode}-frame`, preventScroll: true}]);
        }
        const f = fixture({missingFrame: mode});
        assert.doesNotThrow(() => f.click(mode));
        assert.deepEqual(f.calls, []);
    }
});

test('keyboard activation does not reserve a loading client frame', () => {
    for (const mode of ['terminal', 'desktop']) {
        const f = fixture({missingClient: mode});
        f.click(mode, {detail: 0});
        assert.deepEqual(f.calls, []);
    }
});

test('repeated swaps and same-mode selection keep the existing client documents and instances', () => {
    const f = fixture();
    const terminal = f.frames.get('terminal-frame');
    const desktop = f.frames.get('vnc-frame');
    for (let i = 0; i < 10; i++) {
        f.click('terminal');
        f.click('desktop');
    }
    f.click('terminal');
    f.click('terminal');
    assert.equal(f.calls.length, 22);
    assert.equal(terminal.contentWindow.term, f.term);
    assert.equal(desktop.contentDocument.querySelector('#noVNC_container canvas'), f.canvas);
    assert.equal(terminal.src, 'https://fixture/terminal');
    assert.equal(desktop.src, 'https://fixture/desktop');
});
