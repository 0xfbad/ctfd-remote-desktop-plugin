import assert from 'node:assert/strict';
import test from 'node:test';

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

const {pasteText, sendShortcut} = await import('../src/static/js/workspace-clipboard.js');
const {default: RFB} = await import('../src/static/novnc/869e3dcb0d8de7f5/core/rfb.js');
const {default: Keyboard} = await import('../src/static/novnc/869e3dcb0d8de7f5/core/input/keyboard.js');

function client(held = []) {
    const rfb = Object.create(RFB.prototype);
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
