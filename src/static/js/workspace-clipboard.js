export function sendShortcut(rfb, code, keysym, shift = false) {
    // the pinned keyboard ledger records forwarded modifiers, dom flags can arrive without their keydown
    const held = rfb._keyboard?._keyDownList;
    if (!held || rfb.viewOnly) return false;

    const meta = Object.entries(held).filter(([code]) => code === 'MetaLeft' || code === 'MetaRight');
    const added = [];
    if (!('ControlLeft' in held || 'ControlRight' in held)) added.push([0xffe3, 'ControlLeft']);
    if (shift && !('ShiftLeft' in held || 'ShiftRight' in held)) added.push([0xffe1, 'ShiftLeft']);

    for (const [code, keysym] of meta) rfb.sendKey(keysym, code, false);
    for (const [keysym, code] of added) rfb.sendKey(keysym, code, true);
    rfb.sendKey(keysym, code, true);
    rfb.sendKey(keysym, code, false);
    for (const [keysym, code] of added.reverse()) rfb.sendKey(keysym, code, false);
    for (const [code, keysym] of meta) rfb.sendKey(keysym, code, true);
    return true;
}

export function pasteText(rfb, text, shift = false) {
    if (!rfb._keyboard?._keyDownList || rfb.viewOnly) return false;
    rfb.clipboardPasteFrom(text);
    return sendShortcut(rfb, 'KeyV', shift ? 0x56 : 0x76, shift);
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

function terminalCopy() {
    document.addEventListener('keydown', event => {
        if (!event.ctrlKey || !event.shiftKey || event.code !== 'KeyC') return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!event.isTrusted || !activeOwner()) return;
        const text = window.term?.getSelection();
        if (!text) return;

        void writeClipboard(text, value => {
            if (!activeOwner()) return;
            const field = document.createElement('textarea');
            field.value = value;
            field.setAttribute('aria-label', 'Copy selected text');
            field.style.cssText = 'position:fixed;top:8px;left:8px;width:80%;z-index:9999';
            document.body.append(field);
            field.focus();
            field.select();
            field.addEventListener('blur', () => field.remove(), {once: true});
            document.execCommand('copy');
        });
    }, true);
}

export function desktopClipboard(UI) {
    let bound = null;
    let remoteText = null;
    let copyIntent = null;
    let pendingPaste = null;
    const field = document.getElementById('noVNC_clipboard_text');

    function owner(rfb = UI.rfb) {
        return rfb && rfb === UI.rfb && UI.connected && !rfb.viewOnly && activeOwner();
    }

    function clearIntent() {
        copyIntent = null;
        pendingPaste = null;
    }

    function fallback(text, rfb = UI.rfb) {
        if (!owner(rfb)) return;
        field.value = text;
        UI.openClipboardPanel();
        field.focus();
        field.select();
    }

    function receive(event) {
        if (this !== UI.rfb) return;
        remoteText = event.detail.text;
        const intent = copyIntent;
        copyIntent = null;
        // primary selections also emit clipboard events, only a copy gesture may write to the host
        if (intent === this && owner(this)
            && navigator.userActivation?.isActive) {
            void writeClipboard(remoteText, text => fallback(text, this));
        }
    }

    function disconnected() {
        clearIntent();
        remoteText = null;
    }

    function bind() {
        if (bound === UI.rfb) return;
        bound?.removeEventListener('clipboard', receive);
        bound?.removeEventListener('disconnect', disconnected);
        bound = UI.rfb;
        disconnected();
        bound?.addEventListener('clipboard', receive);
        bound?.addEventListener('disconnect', disconnected);
    }

    new MutationObserver(bind).observe(document.getElementById('noVNC_container'), {childList: true});
    bind();

    document.addEventListener('keydown', event => {
        const copy = event.code === 'KeyC' && !event.altKey
            && ((event.ctrlKey && event.shiftKey) || event.metaKey);
        if (copy && event.ctrlKey && event.shiftKey) event.preventDefault();
        if (!owner() || document.activeElement !== UI.rfb._canvas || !event.isTrusted) return;

        if (copy) {
            event.preventDefault();
            event.stopImmediatePropagation();
            pendingPaste = null;
            const rfb = UI.rfb;
            copyIntent = remoteText === null ? rfb : null;
            if (remoteText !== null) void writeClipboard(remoteText, text => fallback(text, rfb));
            sendShortcut(rfb, 'KeyC', event.shiftKey ? 0x43 : 0x63, event.shiftKey);
            return;
        }

        const paste = event.code === 'KeyV' && !event.altKey && (event.ctrlKey || event.metaKey);
        if (paste) {
            copyIntent = null;
            pendingPaste = {rfb: UI.rfb, shift: event.shiftKey};
            // allow the browser paste event, suppress the guest chord until its clipboard is announced
            event.stopImmediatePropagation();
            return;
        }

        if (!['ControlLeft', 'ControlRight', 'ShiftLeft', 'ShiftRight', 'MetaLeft', 'MetaRight'].includes(event.code)) {
            clearIntent();
        }
    }, true);

    document.addEventListener('paste', event => {
        const pending = pendingPaste;
        pendingPaste = null;
        if (!pending || !event.isTrusted || !owner(pending.rfb)
            || document.activeElement !== pending.rfb._canvas
            || !event.clipboardData?.types.includes('text/plain')) return;

        event.preventDefault();
        event.stopImmediatePropagation();
        pasteText(pending.rfb, event.clipboardData.getData('text/plain'), pending.shift);
    }, true);

    document.addEventListener('pointerdown', clearIntent, true);
    document.addEventListener('contextmenu', clearIntent, true);
    window.addEventListener('blur', clearIntent);
    new MutationObserver(clearIntent).observe(window.frameElement, {attributes: true, attributeFilter: ['class']});

    const controls = document.createElement('div');
    controls.style.marginTop = '8px';
    for (const [label, action] of [
        ['Copy', () => {
            const rfb = UI.rfb;
            if (owner(rfb)) void writeClipboard(field.value, text => fallback(text, rfb));
        }],
        ['Paste to desktop', () => {
            if (!owner()) return;
            clearIntent();
            pasteText(UI.rfb, field.value);
            UI.closeClipboardPanel();
            UI.rfb.focus();
        }],
    ]) {
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
if (frameId === 'terminal-frame') terminalCopy();
if (frameId === 'vnc-frame') {
    // import in the frame realm so the connected singleton is reused
    const {default: UI} = await import(new URL('app/ui.js', location.href));
    desktopClipboard(UI);
}
