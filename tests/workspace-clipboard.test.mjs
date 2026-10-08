import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

function element() {
    return {
        style: {}, offsetWidth: 100,
        appendChild(child) { child.parentNode = this; },
        removeChild() {},
    };
}

globalThis.window = new EventTarget();
Object.defineProperty(globalThis, 'navigator', {
    value: {platform: 'Linux', userAgent: 'Node', maxTouchPoints: 0}, configurable: true,
});
globalThis.document = {documentElement: {}, body: element(), createElement: element};
globalThis.MutationObserver = class { observe() {} disconnect() {} };

const {desktopClipboard, pasteText, sendShortcut, terminalClipboard} = await import('../src/static/js/workspace-clipboard.js');
const {default: RFB} = await import('../src/static/novnc/869e3dcb0d8de7f5/core/rfb.js');
const {default: Keyboard} = await import('../src/static/novnc/869e3dcb0d8de7f5/core/input/keyboard.js');

function client(held = []) {
    const rfb = Object.create(RFB.prototype);
    rfb._listeners = new Map();
    rfb._rfbConnectionState = 'connected';
    rfb._viewOnly = false;
    rfb._qemuExtKeyEventSupported = true;
    rfb._clipboardServerCapabilitiesFormats = {1: true};
    rfb._clipboardServerCapabilitiesActions = {0x08000000: true};
    rfb._sock = {
        bytes: [],
        sQpush8(value) { this.bytes.push(value & 255); },
        sQpush16(value) { this.sQpush8(value >> 8); this.sQpush8(value); },
        sQpush32(value) { this.sQpush8(value >> 24); this.sQpush8(value >> 16); this.sQpush8(value >> 8); this.sQpush8(value); },
        sQpushBytes(value) { this.bytes.push(...value); },
        flush() {},
    };
    rfb.keys = [];
    rfb.sendKey = (...args) => {
        rfb.keys.push(args.slice(0, 3));
        return RFB.prototype.sendKey.call(rfb, ...args);
    };
    rfb._keyboard = new Keyboard(null);
    rfb._keyboard.onkeyevent = (...args) => rfb.sendKey(...args);
    for (const [keysym, code] of held) rfb._keyboard._sendKeyEvent(keysym, code, true);
    rfb.keys = [];
    rfb._sock.bytes = [];
    return rfb;
}

const text = '  clipboard Ω🦊\nsecond\tline  \ne\u0301\n';

test('paste announces exact text before one chord with missing modifiers and no Enter', () => {
    const rfb = client();
    assert.equal(pasteText(rfb, text, true), true);
    assert.equal(rfb._clipboardText, text);
    assert.deepEqual(rfb.keys, [
        [0xffe3, 'ControlLeft', true], [0xffe1, 'ShiftLeft', true],
        [0x56, 'KeyV', true], [0x56, 'KeyV', false],
        [0xffe1, 'ShiftLeft', false], [0xffe3, 'ControlLeft', false],
    ]);
    assert.equal(rfb._sock.bytes[0], 6);
    assert.equal(rfb._sock.bytes[12], 255);
    assert.equal(rfb._sock.bytes.length, 12 + 6 * 12);
    assert.deepEqual(rfb._keyboard._keyDownList, {});
});

test('left and right physical modifiers survive paste and then release normally', () => {
    for (const [control, shift] of [['ControlLeft', 'ShiftLeft'], ['ControlRight', 'ShiftRight']]) {
        const rfb = client([[0xffe3, control], [0xffe1, shift]]);
        const held = {...rfb._keyboard._keyDownList};
        pasteText(rfb, text, true);
        assert.deepEqual(rfb.keys, [[0x56, 'KeyV', true], [0x56, 'KeyV', false]]);
        assert.deepEqual(rfb._keyboard._keyDownList, held);
        for (const code of [shift, control]) {
            rfb._keyboard._handleKeyUp(Object.assign(new Event('keyup'), {code}));
        }
        assert.deepEqual(rfb._keyboard._keyDownList, {});
    }
});

test('mapped Meta keys are temporarily released and restored exactly', () => {
    const rfb = client([[0xffe9, 'MetaLeft'], [0xffeb, 'MetaRight']]);
    const held = {...rfb._keyboard._keyDownList};
    pasteText(rfb, text);
    assert.deepEqual(rfb.keys, [
        [0xffe9, 'MetaLeft', false], [0xffeb, 'MetaRight', false],
        [0xffe3, 'ControlLeft', true], [0x76, 'KeyV', true], [0x76, 'KeyV', false],
        [0xffe3, 'ControlLeft', false], [0xffe9, 'MetaLeft', true], [0xffeb, 'MetaRight', true],
    ]);
    assert.deepEqual(rfb._keyboard._keyDownList, held);
    rfb._keyboard._allKeysUp();
    assert.deepEqual(rfb._keyboard._keyDownList, {});
});

test('viewOnly and missing keyboard state block both clipboard and keys', () => {
    const rfb = client();
    rfb._viewOnly = true;
    assert.equal(pasteText(rfb, text), false);
    assert.deepEqual(rfb._sock.bytes, []);
    rfb._viewOnly = false;
    delete rfb._keyboard;
    assert.equal(pasteText(rfb, text), false);
    assert.deepEqual(rfb._sock.bytes, []);
});

test('disconnected RFB methods send no clipboard or keyboard protocol data', () => {
    const rfb = client();
    rfb._rfbConnectionState = 'disconnected';
    pasteText(rfb, text);
    assert.deepEqual(rfb._sock.bytes, []);
});

test('copy repairs absent modifier events with one C chord and preserves held modifiers', () => {
    const rfb = client();
    assert.equal(sendShortcut(rfb, 'KeyC', 0x43, true), true);
    assert.deepEqual(rfb.keys, [
        [0xffe3, 'ControlLeft', true], [0xffe1, 'ShiftLeft', true],
        [0x43, 'KeyC', true], [0x43, 'KeyC', false],
        [0xffe1, 'ShiftLeft', false], [0xffe3, 'ControlLeft', false],
    ]);
    assert.equal(rfb._sock.bytes[0], 255);
    assert.equal(rfb._sock.bytes.length, 6 * 12);
    assert.deepEqual(rfb._keyboard._keyDownList, {});
    const held = client([[0xffe4, 'ControlRight'], [0xffe2, 'ShiftRight']]);
    sendShortcut(held, 'KeyC', 0x43, true);
    assert.deepEqual(held.keys, [[0x43, 'KeyC', true], [0x43, 'KeyC', false]]);
    assert.deepEqual(held._keyboard._keyDownList, {ControlRight: 0xffe4, ShiftRight: 0xffe2});
});

function clipboardFixture(t, {terminal = false, denied = false, missingItem = false,
    missingAPI = false, throws = false, commandOK = true, delayCompletion = false} = {}) {
    const globals = ['window', 'parent', 'document', 'navigator', 'MutationObserver', 'ClipboardItem', 'setTimeout', 'clearTimeout'];
    const originals = globals.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
    const handlers = new Map();
    const timers = new Map();
    const observers = [];
    const writes = [];
    const fallbacks = [];
    const calls = [];
    const completion = [];
    let selection = '';
    let duringKey = false;
    let timerId = 0;
    let now = 0;
    let doc;
    function node() {
        return {
            style: {}, value: '', selectionStart: 0, selectionEnd: 0,
            children: [], events: new Map(), classList: {contains: () => false},
            append(...children) {this.children.push(...children);},
            after(...following) {this.following = following;},
            setAttribute() {}, addEventListener(name, callback) {this.events.set(name, callback);}, remove() {},
            focus() {doc.activeElement = this;},
            select() {this.selectionStart = 0; this.selectionEnd = this.value.length;},
        };
    }
    const canvas = node();
    const field = node();
    const container = node();
    const frame = {hidden: false, classList: {contains: () => frame.hidden}, getClientRects: () => [1]};
    const win = {frameElement: frame, addEventListener: (name, callback) => handlers.set(name, callback)};
    doc = {
        activeElement: canvas, focused: true, body: node(),
        hasFocus() {return this.focused;}, createElement: node,
        getElementById: id => id === 'noVNC_clipboard_text' ? field : container,
        addEventListener: (name, callback) => handlers.set(name, callback),
        execCommand(command) {
            calls.push(command);
            if (commandOK) writes.push(this.activeElement === canvas ? selection
                : this.activeElement.value.slice(this.activeElement.selectionStart, this.activeElement.selectionEnd));
            return commandOK;
        },
    };
    const clipboard = missingAPI ? undefined : {
        write(items) {
            calls.push({api: 'write', duringKey});
            if (throws) throw new Error('write failed');
            if (denied) return Promise.reject(new Error('permission denied'));
            return items[0].items['text/plain'].then(async blob => {
                if (delayCompletion) await new Promise(resolve => completion.push(resolve));
                writes.push(await blob.text());
            });
        },
        async writeText(value) {
            calls.push({api: 'writeText', duringKey});
            if (denied) throw new Error('permission denied');
            writes.push(value);
        },
    };
    const replacements = {
        window: win, parent: {document: {activeElement: frame}}, document: doc,
        navigator: {platform: 'Linux', userAgent: 'Node', maxTouchPoints: 0, clipboard},
        MutationObserver: class {
            constructor(callback) {this.callback = callback;}
            observe(target) {observers.push({target, callback: this.callback});}
        },
        ClipboardItem: missingItem ? undefined : class {constructor(items) {this.items = items;}},
        setTimeout: (callback, delay) => {timers.set(++timerId, {callback, at: now + delay}); return timerId;},
        clearTimeout: id => timers.delete(id),
    };
    for (const [name, value] of Object.entries(replacements)) {
        Object.defineProperty(globalThis, name, {value, configurable: true, writable: true});
    }
    const rfb = client();
    rfb._canvas = canvas;
    const UI = {rfb, connected: true, openClipboardPanel: () => fallbacks.push(field.value)};
    if (terminal) {
        win.term = {element: {contains: target => target === canvas}, getSelection: () => selection};
        terminalClipboard();
    } else desktopClipboard(UI);
    t.after(async () => {
        handlers.get('blur')?.();
        await settle();
        for (const [name, descriptor] of originals) {
            if (descriptor) Object.defineProperty(globalThis, name, descriptor);
            else delete globalThis[name];
        }
    });
    function key(code, flags = {}) {
        const event = {code, ctrlKey: false, shiftKey: false, metaKey: false, altKey: false,
            isTrusted: true, prevented: false, stopped: false,
            preventDefault() {this.prevented = true;}, stopImmediatePropagation() {this.stopped = true;}, ...flags};
        duringKey = true;
        handlers.get('keydown')(event);
        duringKey = false;
        return event;
    }
    function receive(value, target = rfb) {
        const event = new Event('clipboard');
        event.detail = {text: value};
        target.dispatchEvent(event);
    }
    function paste(value, {target = canvas, types = ['text/plain'], isTrusted = true} = {}) {
        const event = {target, isTrusted, clipboardData: {types, getData: () => value},
            prevented: false, stopped: false,
            preventDefault() {this.prevented = true;}, stopImmediatePropagation() {this.stopped = true;}};
        handlers.get('paste')?.(event);
        if (!event.stopped) target.events.get('paste')?.(event);
        if (!event.prevented) {
            if (target === canvas) target.children.push({textContent: value});
            else target.value += value;
        }
        return event;
    }
    return {UI, rfb, doc, frame, canvas, container, field, handlers, timers, observers, writes, fallbacks, calls,
        key, receive, paste, select: value => {selection = value;},
        expire: () => {for (const timer of [...timers.values()]) timer.callback();},
        advance: milliseconds => {
            now += milliseconds;
            for (const timer of [...timers.values()]) if (timer.at <= now) timer.callback();
        },
        finishWrite: () => {for (const resolve of completion) resolve();}};
}

async function settle() {
    for (let i = 0; i < 16; i++) await Promise.resolve();
}

test('terminal plain Ctrl+C remains upstream input with and without a selection', t => {
    const f = clipboardFixture(t, {terminal: true});
    for (const selection of ['', text]) {
        f.select(selection);
        for (const flags of [{ctrlKey: true}, {metaKey: true}]) {
            const event = f.key('KeyC', flags);
            assert.equal(event.stopped || event.prevented, false);
        }
    }
    assert.deepEqual(f.calls, []);
    assert.deepEqual(f.writes, []);
});

test('terminal native paste retains browser defaults and the existing xterm paste handler', t => {
    const f = clipboardFixture(t, {terminal: true});
    for (const flags of [{ctrlKey: true}, {metaKey: true}, {ctrlKey: true, shiftKey: true}]) {
        const event = f.key('KeyV', flags);
        assert.equal(event.stopped, true);
        assert.equal(event.prevented, false);
    }
    assert.equal(f.handlers.has('paste'), false);
    assert.deepEqual(f.calls, []);
});

test('terminal Shift+C performs synchronous copy and leaves other focused controls alone', t => {
    const f = clipboardFixture(t, {terminal: true});
    f.select(text);
    const event = f.key('KeyC', {ctrlKey: true, shiftKey: true});
    assert.equal(event.prevented && event.stopped, true);
    assert.deepEqual(f.calls, ['copy']);
    assert.deepEqual(f.writes, [text]);
    f.doc.activeElement = f.field;
    for (const code of ['KeyC', 'KeyV']) {
        const other = f.key(code, {ctrlKey: true});
        assert.equal(other.prevented || other.stopped, false);
    }
});

test('terminal failed synchronous copy exposes selected text in a focused fallback', t => {
    const f = clipboardFixture(t, {terminal: true, commandOK: false});
    f.select(text);
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    assert.equal(f.doc.activeElement.value, text);
    assert.deepEqual(f.calls, ['copy', 'copy']);
});

test('desktop plain Ctrl+C and Meta+C remain guest defaults without host clipboard intent', async t => {
    const f = clipboardFixture(t);
    for (const flags of [{ctrlKey: true}, {metaKey: true}]) {
        const event = f.key('KeyC', flags);
        assert.equal(event.prevented || event.stopped, false);
        f.receive(text);
        await settle();
    }
    assert.deepEqual(f.calls, []);
    assert.deepEqual(f.writes, []);
    assert.deepEqual(f.rfb.keys, []);
    assert.equal(f.timers.size, 0);

    f.rfb._keyboard._sendKeyEvent(0xffe3, 'ControlLeft', true);
    f.rfb.keys = [];
    f.rfb._keyboard._handleKeyDown(Object.assign(new Event('keydown'), {
        code: 'KeyC', key: 'c', ctrlKey: true, getModifierState: () => false,
    }));
    f.rfb._keyboard._handleKeyUp(Object.assign(new Event('keyup'), {code: 'KeyC'}));
    assert.deepEqual(f.rfb.keys, [[0x63, 'KeyC', true], [0x63, 'KeyC', false]]);
    assert.deepEqual(f.rfb._keyboard._keyDownList, {ControlLeft: 0xffe3});
    f.rfb._keyboard._handleKeyUp(Object.assign(new Event('keyup'), {code: 'ControlLeft'}));
    assert.deepEqual(f.rfb._keyboard._keyDownList, {});
});

test('desktop plain Ctrl+C cancels a pending host copy without replacing it', async t => {
    const f = clipboardFixture(t);
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    const calls = f.calls.length;
    const event = f.key('KeyC', {ctrlKey: true});
    f.receive(text);
    await settle();
    assert.equal(event.prevented || event.stopped, false);
    assert.equal(f.calls.length, calls);
    assert.deepEqual(f.writes, []);
    assert.deepEqual(f.fallbacks, []);
    assert.equal(f.timers.size, 0);
});

test('desktop write starts in the gesture and uses only a fresh response including repeated and empty copies', async t => {
    const f = clipboardFixture(t);
    f.receive('stale prior selection');
    for (const value of [text, text, '']) {
        const before = f.writes.length;
        f.key('KeyC', {ctrlKey: true, shiftKey: true});
        assert.equal(f.calls.at(-1).duringKey, true);
        await settle();
        assert.equal(f.writes.length, before);
        f.receive(value);
        await settle();
        assert.equal(f.writes.at(-1), value);
        assert.equal(f.timers.size, 0);
    }
    assert.deepEqual(f.writes, [text, text, '']);
    assert.deepEqual(f.fallbacks, []);
    assert.equal(f.rfb.keys.filter(key => key[1] === 'KeyC' && key[2]).length, 3);
});

test('first response stops the guest deadline while delayed browser permission completes', async t => {
    const f = clipboardFixture(t, {delayCompletion: true});
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    f.receive(text);
    await settle();
    assert.equal(f.timers.size, 0);
    f.expire();
    assert.deepEqual(f.writes, []);
    f.finishWrite();
    await settle();
    assert.deepEqual(f.writes, [text]);
    assert.deepEqual(f.fallbacks, []);
});

for (const options of [{denied: true}, {throws: true}, {missingAPI: true}]) {
    test(`desktop unavailable clipboard ${JSON.stringify(options)} waits for fresh text before fallback`, async t => {
        const f = clipboardFixture(t, options);
        f.key('KeyC', {ctrlKey: true, shiftKey: true});
        await settle();
        assert.deepEqual(f.fallbacks, []);
        f.receive(text);
        await settle();
        assert.deepEqual(f.fallbacks, [text]);
        assert.deepEqual(f.writes, []);
        assert.equal(f.timers.size, 0);
    });
}

test('desktop missing ClipboardItem uses fresh writeText without exporting prior selections', async t => {
    const f = clipboardFixture(t, {missingItem: true});
    f.receive('old');
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    assert.deepEqual(f.writes, []);
    f.receive(text);
    await settle();
    assert.deepEqual(f.writes, [text]);
    assert.deepEqual(f.calls, [{api: 'writeText', duringKey: false}]);
});

for (const missingItem of [false, true]) {
    test(`desktop cancellation after response prevents deferred data fulfillment ${missingItem ? 'writeText' : 'ClipboardItem'}`, async t => {
        const f = clipboardFixture(t, {missingItem});
        f.key('KeyC', {ctrlKey: true, shiftKey: true});
        f.receive(text);
        f.handlers.get('blur')();
        await settle();
        assert.deepEqual(f.writes, []);
        assert.deepEqual(f.fallbacks, []);
        assert.equal(f.timers.size, 0);
    });
}

test('desktop delayed guest response succeeds before the deadline and remains canceled after it', async t => {
    const f = clipboardFixture(t);
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    f.advance(1500);
    f.receive(text);
    await settle();
    assert.deepEqual(f.writes, [text]);
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    f.advance(2500);
    f.receive('late response');
    await settle();
    assert.deepEqual(f.writes, [text]);
    assert.deepEqual(f.fallbacks, []);
    assert.equal(f.timers.size, 0);
});

for (const cancel of ['blur', 'pointerdown', 'contextmenu', 'disconnect', 'deadline', 'next-input', 'mode']) {
    test(`desktop ${cancel} cancels pending copies without exporting later selections`, async t => {
        const f = clipboardFixture(t);
        f.key('KeyC', {ctrlKey: true, shiftKey: true});
        if (cancel === 'disconnect') f.rfb.dispatchEvent(new Event('disconnect'));
        else if (cancel === 'deadline') f.expire();
        else if (cancel === 'next-input') f.key('KeyA');
        else if (cancel === 'mode') f.observers.find(observer => observer.target === f.frame).callback();
        else f.handlers.get(cancel)();
        f.receive('unrelated');
        await settle();
        assert.deepEqual(f.writes, []);
        assert.deepEqual(f.fallbacks, []);
        assert.equal(f.timers.size, 0);
    });
}

test('desktop replacing a denied copy suppresses old fallback and copies only the first fresh response', async t => {
    const f = clipboardFixture(t, {denied: true});
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    f.receive(text);
    f.receive('later unrelated selection');
    await settle();
    assert.deepEqual(f.fallbacks, [text]);
    assert.equal(f.timers.size, 0);
});

for (const state of ['hidden', 'unfocused', 'readOnly', 'disconnected', 'untrusted']) {
    test(`desktop ${state} cannot send a copy chord or write host clipboard`, async t => {
        const f = clipboardFixture(t);
        if (state === 'hidden') f.frame.hidden = true;
        else if (state === 'unfocused') f.doc.focused = false;
        else if (state === 'readOnly') f.rfb._viewOnly = true;
        else if (state === 'disconnected') f.UI.connected = false;
        f.key('KeyC', {ctrlKey: true, shiftKey: true, isTrusted: state !== 'untrusted'});
        f.receive(text);
        await settle();
        assert.deepEqual(f.calls, []);
        assert.deepEqual(f.writes, []);
        assert.deepEqual(f.rfb.keys, []);
    });
}

test('desktop reconnect removes old listeners and binds copy to the replacement owner', async t => {
    const f = clipboardFixture(t);
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    const replacement = client();
    replacement._canvas = f.canvas;
    f.UI.rfb = replacement;
    f.observers.find(observer => observer.target === f.container).callback();
    f.receive('old server');
    await settle();
    assert.deepEqual(f.writes, []);
    assert.equal(f.rfb._listeners.get('clipboard').size, 0);
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    f.receive(text, replacement);
    await settle();
    assert.deepEqual(f.writes, [text]);
    assert.equal(replacement.keys.filter(key => key[1] === 'KeyC' && key[2]).length, 1);
});

test('desktop clipboard paste preserves exact text and one repaired chord without Enter', t => {
    const f = clipboardFixture(t);
    const key = f.key('KeyV', {ctrlKey: true});
    assert.equal(key.prevented, false);
    assert.equal(key.stopped, true);
    f.handlers.get('paste')({isTrusted: true, clipboardData: {types: ['text/plain'], getData: () => text},
        preventDefault() {}, stopImmediatePropagation() {}});
    assert.equal(f.rfb._clipboardText, text);
    assert.equal(f.rfb.keys.filter(key => key[1] === 'KeyV' && key[2]).length, 1);
    assert.equal(f.rfb.keys.some(key => key[1] === 'Enter'), false);
});

test('desktop canvas remains focused and normal typing reaches the pinned keyboard without DOM edits', t => {
    const f = clipboardFixture(t);
    assert.equal(f.canvas.contentEditable, 'true');
    assert.equal(f.doc.activeElement, f.canvas);
    assert.equal(f.field.contentEditable, undefined);
    const event = f.key('KeyA');
    assert.equal(event.prevented || event.stopped, false);
    const guestEvent = Object.assign(new Event('keydown', {cancelable: true}), {
        code: 'KeyA', key: 'a', getModifierState: () => false,
    });
    f.rfb._keyboard._handleKeyDown(guestEvent);
    f.rfb._keyboard._handleKeyUp(Object.assign(new Event('keyup'), {code: 'KeyA'}));
    assert.equal(guestEvent.defaultPrevented, true);
    assert.deepEqual(f.rfb.keys, [[0x61, 'KeyA', true], [0x61, 'KeyA', false]]);
    let prevented = false;
    f.canvas.events.get('beforeinput')?.({inputType: 'insertText', data: 'a', preventDefault() {prevented = true;}});
    if (!prevented) f.canvas.children.push({textContent: 'a'});
    assert.equal(prevented, true);
    assert.deepEqual(f.canvas.children, []);
});

test('desktop editable canvas Shift+V transfers once and leaves held modifiers for their physical releases', t => {
    const f = clipboardFixture(t);
    f.rfb._keyboard._sendKeyEvent(0xffe3, 'ControlLeft', true);
    f.rfb._keyboard._sendKeyEvent(0xffe1, 'ShiftLeft', true);
    f.rfb.keys = [];
    const key = f.key('KeyV', {ctrlKey: true, shiftKey: true});
    assert.equal(key.prevented, false);
    assert.equal(key.stopped, true);
    const event = f.paste(text);
    assert.equal(event.prevented && event.stopped, true);
    assert.equal(f.rfb._clipboardText, text);
    assert.deepEqual(f.rfb.keys, [[0x56, 'KeyV', true], [0x56, 'KeyV', false]]);
    assert.equal(f.doc.activeElement, f.canvas);
    assert.deepEqual(f.canvas.children, []);
    for (const code of ['ShiftLeft', 'ControlLeft']) {
        f.rfb._keyboard._handleKeyUp(Object.assign(new Event('keyup'), {code}));
    }
    assert.deepEqual(f.rfb._keyboard._keyDownList, {});
});

for (const state of ['no intent', 'readOnly', 'hidden', 'unfocused', 'disconnected', 'untrusted', 'nontext', 'stale connection']) {
    test(`desktop ${state} paste cannot insert canvas DOM or send clipboard and keys`, t => {
        const f = clipboardFixture(t);
        if (state !== 'no intent') f.key('KeyV', {ctrlKey: true, shiftKey: true});
        if (state === 'readOnly') f.rfb._viewOnly = true;
        else if (state === 'hidden') f.frame.hidden = true;
        else if (state === 'unfocused') f.doc.focused = false;
        else if (state === 'disconnected') f.UI.connected = false;
        else if (state === 'stale connection') {
            const replacement = client();
            replacement._canvas = f.canvas;
            f.UI.rfb = replacement;
            f.observers.find(observer => observer.target === f.container).callback();
        }
        const event = f.paste(text, {isTrusted: state !== 'untrusted', types: state === 'nontext' ? ['image/png'] : ['text/plain']});
        assert.equal(event.prevented, true);
        assert.deepEqual(f.canvas.children, []);
        assert.deepEqual(f.rfb._sock.bytes, []);
        assert.deepEqual(f.rfb.keys, []);
        assert.deepEqual(f.writes, []);
        assert.equal(f.doc.activeElement, f.canvas);
    });
}

test('desktop shared field native paste retains its default insertion without guest clipboard transfer', t => {
    const f = clipboardFixture(t);
    f.field.focus();
    const event = f.paste(text, {target: f.field});
    assert.equal(event.prevented || event.stopped, false);
    assert.equal(f.field.value, text);
    assert.deepEqual(f.rfb._sock.bytes, []);
    assert.deepEqual(f.canvas.children, []);
});

test('desktop explicit Copy button works without async clipboard and requires the active owner', t => {
    const f = clipboardFixture(t, {missingAPI: true});
    f.field.value = text;
    const button = f.field.following[0].children.find(child => child.textContent === 'Copy');
    button.events.get('click')();
    assert.deepEqual(f.writes, [text]);
    assert.deepEqual(f.calls, ['copy']);
    f.doc.focused = false;
    button.events.get('click')();
    assert.deepEqual(f.writes, [text]);
    assert.deepEqual(f.calls, ['copy']);
});

test('desktop shared field Shift+C copies only the selected Unicode range without guest input', t => {
    const f = clipboardFixture(t, {missingAPI: true});
    f.field.value = `before ${text} after`;
    f.field.selectionStart = 7;
    f.field.selectionEnd = 7 + text.length;
    f.field.focus();
    const event = f.key('KeyC', {ctrlKey: true, shiftKey: true});
    assert.equal(event.prevented && event.stopped, true);
    assert.deepEqual(f.calls, ['copy']);
    assert.deepEqual(f.writes, [text]);
    assert.equal(f.field.value, `before ${text} after`);
    assert.deepEqual(f.rfb.keys, []);
    assert.equal(f.timers.size, 0);
});

test('desktop shared field retains native plain copy and does not copy an empty range', t => {
    const f = clipboardFixture(t);
    f.field.value = text;
    f.field.focus();
    for (const flags of [{ctrlKey: true}, {metaKey: true}]) {
        const event = f.key('KeyC', flags);
        assert.equal(event.prevented || event.stopped, false);
    }
    const event = f.key('KeyC', {ctrlKey: true, shiftKey: true});
    assert.equal(event.prevented, true);
    assert.equal(event.stopped, false);
    assert.deepEqual(f.calls, []);
    assert.deepEqual(f.writes, []);
    assert.deepEqual(f.rfb.keys, []);
});

for (const state of ['hidden', 'unfocused', 'readOnly', 'disconnected', 'untrusted']) {
    test(`desktop shared field ${state} cannot copy a selection`, t => {
        const f = clipboardFixture(t);
        f.field.value = text;
        f.field.focus();
        f.field.select();
        if (state === 'hidden') f.frame.hidden = true;
        else if (state === 'unfocused') f.doc.focused = false;
        else if (state === 'readOnly') f.rfb._viewOnly = true;
        else if (state === 'disconnected') f.UI.connected = false;
        f.key('KeyC', {ctrlKey: true, shiftKey: true, isTrusted: state !== 'untrusted'});
        assert.deepEqual(f.calls, []);
        assert.deepEqual(f.writes, []);
        assert.deepEqual(f.rfb.keys, []);
        assert.equal(f.timers.size, 0);
    });
}

for (const denied of [false, true]) {
    test(`desktop shared field failed native copy preserves content and range with ${denied ? 'denied' : 'allowed'} async clipboard`, async t => {
        const f = clipboardFixture(t, {commandOK: false, denied});
        f.field.value = `before ${text} after`;
        f.field.selectionStart = 7;
        f.field.selectionEnd = 7 + text.length;
        f.field.focus();
        f.key('KeyC', {ctrlKey: true, shiftKey: true});
        await settle();
        assert.deepEqual(f.writes, denied ? [] : [text]);
        assert.deepEqual(f.calls, ['copy', {api: 'writeText', duringKey: true}]);
        assert.equal(f.field.value, `before ${text} after`);
        assert.equal(f.field.selectionStart, 7);
        assert.equal(f.field.selectionEnd, 7 + text.length);
        assert.deepEqual(f.fallbacks, []);
        assert.deepEqual(f.rfb.keys, []);
        assert.equal(f.timers.size, 0);
    });
}

test('desktop replacing a copy after response but before fulfillment cannot write old text', async t => {
    const f = clipboardFixture(t);
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    f.receive('old response');
    f.key('KeyC', {ctrlKey: true, shiftKey: true});
    f.receive(text);
    await settle();
    assert.deepEqual(f.writes, [text]);
    assert.deepEqual(f.fallbacks, []);
});

function loaderFixture({transitional = false} = {}) {
    const frames = new Map();
    for (const id of ['vnc-frame', 'terminal-frame']) {
        const scripts = [];
        const head = {append: script => scripts.push(script)};
        const listeners = new Map();
        const page = {
            URL: transitional && id === 'terminal-frame' ? 'about:blank' : 'https://fixture/_rd_/workspace',
            head: transitional && id === 'vnc-frame' ? null : head,
            getElementById: name => scripts.find(script => script.id === name), createElement: () => ({}),
        };
        const frame = {contentDocument: page, addEventListener: (name, callback) => listeners.set(name, callback)};
        frames.set(id, {frame, page, head, scripts, listeners});
    }
    const document = {getElementById: id => frames.get(id)?.frame, addEventListener() {}};
    const path = new URL('../src/static/js/workspace-input.js', import.meta.url);
    const source = readFileSync(path, 'utf8');
    runInNewContext(source.replace('import.meta.url', "'https://fixture/plugins/remote/static/js/workspace-input.js'"), {document, URL});
    return frames;
}

test('workspace loader waits for transitional documents without losing the other frame listener', () => {
    const frames = loaderFixture({transitional: true});
    for (const item of frames.values()) {
        assert.equal(item.listeners.has('load'), true);
        assert.equal(item.scripts.length, 0);
        item.page.URL = 'https://fixture/_rd_/loaded';
        item.page.head = item.head;
        item.listeners.get('load')();
        assert.equal(item.scripts.length, 1);
        assert.equal(item.scripts[0].type, 'module');
        item.listeners.get('load')();
        assert.equal(item.scripts.length, 1);
    }
});

test('workspace loader installs into already loaded frames exactly once', () => {
    const frames = loaderFixture();
    for (const item of frames.values()) {
        assert.equal(item.scripts.length, 1);
        assert.equal(item.scripts[0].src, 'https://fixture/plugins/remote/static/js/workspace-clipboard.js');
        item.listeners.get('load')();
        assert.equal(item.scripts.length, 1);
    }
});
