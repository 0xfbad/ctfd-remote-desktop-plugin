import {collapseInitialControls} from './workspace-controls.js';
import {releaseInputOnNavigation} from './workspace-input-state.js';

export function sendShortcut(rfb, code, keysym, shift = false, control = true) {
    const held = rfb._keyboard?._keyDownList; // dom flags may arrive without forwarded modifier key events

    if (!held || rfb.viewOnly)
        return false;

    const meta = Object.entries(held).filter(([code]) => code === 'MetaLeft' || code === 'MetaRight');
    const added = [];

    if (control && !('ControlLeft' in held || 'ControlRight' in held))
        added.push([0xffe3, 'ControlLeft']);
    if (shift && !('ShiftLeft' in held || 'ShiftRight' in held))
        added.push([0xffe1, 'ShiftLeft']);

    for (const [code, keysym] of meta)
        rfb.sendKey(keysym, code, false);
    for (const [keysym, code] of added)
        rfb.sendKey(keysym, code, true);

    rfb.sendKey(keysym, code, true);
    rfb.sendKey(keysym, code, false);

    for (const [keysym, code] of added.reverse())
        rfb.sendKey(keysym, code, false);
    for (const [code, keysym] of meta)
        rfb.sendKey(keysym, code, true);

    return true;
}

export function pasteText(rfb, text, chord = ['KeyV', 0x76]) {
    if (!rfb._keyboard?._keyDownList || rfb.viewOnly)
        return false;

    rfb.clipboardPasteFrom(text);
    return sendShortcut(rfb, ...chord);
}

function activeOwner() {
    const frame = window.frameElement;
    return frame && !frame.classList.contains('hidden') && frame.getClientRects().length > 0
        && parent.document.activeElement === frame && document.hasFocus();
}

async function writeClipboard(text, fallback) {
    try {
        await navigator.clipboard.writeText(text);
    } catch {
        fallback(text);
    }
}

export function terminalClipboard() {
    for (const type of ['copy', 'cut']) {
        document.addEventListener(type, () => {
            parent.document.dispatchEvent(new Event('workspace-clipboard-change'));
        });
    }

    document.addEventListener('keydown', event => {
        const copy = event.code === 'KeyC' && !event.altKey && event.ctrlKey && event.shiftKey;
        const term = window.term;

        if (!event.isTrusted || !activeOwner() || !term?.element?.contains(document.activeElement))
            return;

        if (event.code === 'KeyV' && !event.altKey && (event.ctrlKey || event.metaKey)) {
            event.stopImmediatePropagation();
            return;
        }

        if (!copy)
            return;

        event.preventDefault();
        const text = term.getSelection();

        if (!text)
            return;

        event.stopImmediatePropagation();

        if (document.execCommand('copy'))
            return;

        const field = document.createElement('textarea');
        field.value = text;
        field.setAttribute('aria-label', 'Copy selected text');
        field.style.cssText = 'position:fixed;top:8px;left:8px;width:80%;z-index:9999';
        document.body.append(field);
        field.focus();
        field.select();
        field.addEventListener('blur', () => field.remove(), {once: true});
        document.execCommand('copy');
    }, true);
}

export function desktopClipboard(UI) {
    let bound = null;
    let pendingCopy = null;
    let pendingPaste = null;
    let preferGuestClipboard = false;
    const field = document.getElementById('noVNC_clipboard_text');
    const apple = /mac|iphone|ipad|ipod/i.test(navigator.platform);

    function owner(rfb = UI.rfb) {
        return rfb && rfb === UI.rfb && UI.connected && !rfb.viewOnly && activeOwner();
    }

    function clearIntent() {
        const pending = pendingCopy;
        pendingCopy = null;
        pendingPaste = null;

        if (!pending)
            return;

        clearTimeout(pending.timer);
        pending.reject(new Error('copy canceled'));
    }

    function resetClipboardPreference() {
        preferGuestClipboard = false;
        clearIntent();
    }

    function fallback(text, rfb = UI.rfb) {
        if (!owner(rfb))
            return;

        field.value = text;
        UI.openClipboardPanel();
        field.focus();
        field.select();
    }

    function startCopy(rfb, explicit = true) {
        clearIntent();
        let resolve;
        let reject;
        const text = new Promise((yes, no) => {
            resolve = yes;
            reject = no;
        });
        const pending = {rfb, resolve, reject, timer: null};
        pendingCopy = pending;
        pending.timer = setTimeout(() => {
            if (pendingCopy === pending)
                clearIntent();
        }, 2000);

        function currentText(value) {
            if (pendingCopy !== pending || !owner(rfb))
                throw new Error('copy canceled');

            return value;
        }

        let write;
        try {
            if (typeof ClipboardItem === 'function' && navigator.clipboard?.write) {
                const blob = text.then(value => new Blob([currentText(value)], {type: 'text/plain'}));
                void blob.catch(() => {});
                write = navigator.clipboard.write([new ClipboardItem({'text/plain': blob})]); // webkit requires starting the write in the copy gesture before the guest response arrives
            } else {
                write = text.then(value => navigator.clipboard.writeText(currentText(value)));
            }
        } catch (error) {
            write = Promise.reject(error);
        }

        void Promise.all([text, Promise.resolve(write).then(() => true, () => false)]).then(([value, written]) => {
            if (pendingCopy !== pending)
                return;

            pendingCopy = null;
            clearTimeout(pending.timer);

            if (!written && explicit)
                fallback(value, rfb);
        }, () => {});
    }

    function receive(event) {
        if (this !== UI.rfb || !owner(this))
            return;

        preferGuestClipboard = true; // the desktop server filters primary selections
        const pending = pendingCopy;

        if (pending && pending.rfb === this) {
            clearTimeout(pending.timer);
            pending.resolve(event.detail.text);
            return;
        }

        if (navigator.userActivation?.isActive) // stops guest clipboard changes from overwriting the host clipboard while the user is idle
            void writeClipboard(event.detail.text, () => {});
    }

    function bind() {
        if (bound === UI.rfb)
            return;

        bound?.removeEventListener('clipboard', receive);
        bound?.removeEventListener('disconnect', resetClipboardPreference);
        bound = UI.rfb;

        if (bound) {
            bound._canvas.contentEditable = 'true';
            bound._canvas.addEventListener('beforeinput', event => event.preventDefault());
            bound._canvas.addEventListener('paste', event => event.preventDefault());
        }

        resetClipboardPreference();
        bound?.addEventListener('clipboard', receive);
        bound?.addEventListener('disconnect', resetClipboardPreference);
    }

    new MutationObserver(bind).observe(document.getElementById('noVNC_container'), {childList: true});
    bind();

    document.addEventListener('keydown', event => {
        const copy = event.code === 'KeyC' && !event.altKey && event.ctrlKey && event.shiftKey;

        if (copy)
            event.preventDefault();

        if (!owner() || !event.isTrusted)
            return;

        if (copy && document.activeElement === field
            && field.selectionStart !== field.selectionEnd) {
            const selected = field.value.slice(field.selectionStart, field.selectionEnd);
            event.stopImmediatePropagation();

            if (!document.execCommand('copy'))
                void writeClipboard(selected, () => {});
            return;
        }

        if (document.activeElement !== UI.rfb._canvas)
            return;

        if (copy) {
            event.stopImmediatePropagation();
            const rfb = UI.rfb;
            startCopy(rfb);
            sendShortcut(rfb, 'KeyC', 0x43, true);
            return;
        }

        const insert = !apple && event.code === 'Insert' && event.shiftKey
            && !event.ctrlKey && !event.altKey && !event.metaKey;
        const paste = insert || (event.code === 'KeyV' && !event.altKey && (event.ctrlKey || event.metaKey));

        if (paste) {
            clearIntent();
            const chord = insert ? ['Insert', 0xff63, true, false] : ['KeyV', event.shiftKey ? 0x56 : 0x76, event.shiftKey];

            if (preferGuestClipboard && insert)
                return; // the viewer forwards the held shift and insert unchanged

            if (preferGuestClipboard) {
                event.preventDefault();
                event.stopImmediatePropagation();
                sendShortcut(UI.rfb, ...chord);
                return;
            }

            const pending = {rfb: UI.rfb, chord};
            pendingPaste = pending;
            event.stopImmediatePropagation(); // the browser must provide clipboard data before the guest paste chord

            if (apple && !event.metaKey) { // apple browsers only paste for command chords
                event.preventDefault();
                void readHostPaste(pending);
            }
            return;
        }

        if (event.ctrlKey && !event.shiftKey && !event.altKey && !event.metaKey
            && ['KeyC', 'KeyX', 'Insert'].includes(event.code)) {
            startCopy(UI.rfb, false); // plain chords are guest shortcuts, so the key passes through and the host write is best effort
            return;
        }

        if (!['ControlLeft', 'ControlRight', 'ShiftLeft', 'ShiftRight', 'MetaLeft', 'MetaRight'].includes(event.code)) {
            clearIntent();
        }
    }, true);

    async function readHostPaste(pending) {
        try {
            const text = await navigator.clipboard.readText();

            if (pendingPaste !== pending || !owner(pending.rfb)
                || document.activeElement !== pending.rfb._canvas)
                return;

            pendingPaste = null;

            if (preferGuestClipboard) {
                sendShortcut(pending.rfb, ...pending.chord);
                return;
            }

            pasteText(pending.rfb, text, pending.chord);
        } catch {
            if (pendingPaste !== pending || !owner(pending.rfb))
                return;

            pendingPaste = null;
            UI.openClipboardPanel();
            field.focus();
        }
    }

    document.addEventListener('paste', event => {
        const pending = pendingPaste;
        pendingPaste = null;

        if (!pending || !event.isTrusted || !owner(pending.rfb)
            || document.activeElement !== pending.rfb._canvas)
            return;

        if (preferGuestClipboard) {
            event.preventDefault();
            event.stopImmediatePropagation();
            sendShortcut(pending.rfb, ...pending.chord);
            return;
        }

        if (event.clipboardData?.types.includes('text/plain')) {
            event.preventDefault();
            event.stopImmediatePropagation();
            pasteText(pending.rfb, event.clipboardData.getData('text/plain'), pending.chord);
            return;
        }

        if (event.clipboardData?.types.length !== 0 || typeof navigator.clipboard?.readText !== 'function')
            return;

        event.preventDefault();
        event.stopImmediatePropagation();
        pendingPaste = pending;
        void readHostPaste(pending); // webkit may withhold paste formats while allowing a clipboard read
    }, true);

    document.addEventListener('pointerdown', clearIntent, true);
    document.addEventListener('contextmenu', clearIntent, true);
    window.addEventListener('blur', () => {
        clearIntent();

        if (!parent.document.hasFocus())
            resetClipboardPreference();
    });

    const lifecycle = new AbortController();
    window.addEventListener('pagehide', event => {
        if (!event.persisted)
            lifecycle.abort();
    });
    parent.addEventListener('blur', () => {
        if (!parent.document.hasFocus())
            resetClipboardPreference();
    }, {signal: lifecycle.signal});
    parent.document.addEventListener('visibilitychange', () => {
        if (parent.document.hidden)
            resetClipboardPreference();
    }, {signal: lifecycle.signal});

    for (const type of ['copy', 'cut']) {
        document.addEventListener(type, () => {
            if (document.activeElement !== UI.rfb?._canvas)
                resetClipboardPreference();
        });
    }
    for (const type of ['copy', 'cut', 'workspace-clipboard-change']) {
        parent.document.addEventListener(type, resetClipboardPreference, {signal: lifecycle.signal});
    }

    new MutationObserver(clearIntent).observe(window.frameElement, {attributes: true, attributeFilter: ['class']});

    const controls = document.createElement('div');
    controls.style.marginTop = '8px';
    const actions = [
        ['Copy', () => {
            const rfb = UI.rfb;

            if (!owner(rfb))
                return;

            field.focus();
            field.select();

            if (!document.execCommand('copy'))
                void writeClipboard(field.value, text => fallback(text, rfb));
        }],
        ['Paste to desktop', () => {
            if (!owner())
                return;

            resetClipboardPreference();
            pasteText(UI.rfb, field.value);
            UI.closeClipboardPanel();
            UI.rfb.focus();
        }],
    ];

    for (const [label, action] of actions) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.style.marginRight = '8px';
        button.addEventListener('click', action);
        controls.append(button);
    }

    field.after(controls);
}

const frameId = window.frameElement?.id;

if (frameId === 'terminal-frame')
    terminalClipboard();
if (frameId === 'vnc-frame') {
    const {default: UI} = await import(new URL('app/ui.js', location.href)); // resolve against the frame document instead of this script so the connected singleton is reused
    const {releaseCapture} = await import(new URL('core/util/events.js', location.href));
    collapseInitialControls(UI);
    releaseInputOnNavigation(UI, releaseCapture);
    desktopClipboard(UI);
}
