/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC authors
 * Licensed under MPL 2.0 or any later version (see LICENSE.txt)
 */

import * as Log from '../util/logging.js';
import { stopEvent } from '../util/events.js';
import * as KeyboardUtil from "./util.js";
import KeyTable from "./keysym.js";
import * as browser from "../util/browser.js";

export default class Keyboard {
    constructor(target) {
        this._target = target || null;

        this._keyDownList = {};

        this._altGrArmed = false;


        this._eventHandlers = {
            'keyup': this._handleKeyUp.bind(this),
            'keydown': this._handleKeyDown.bind(this),
            'blur': this._allKeysUp.bind(this),
        };

        this.onkeyevent = () => {};
    }

    _sendKeyEvent(keysym, code, down, numlock = null, capslock = null) {
        if (down) {
            this._keyDownList[code] = keysym;
        } else {

            if (!(code in this._keyDownList)) {
                return;
            }
            delete this._keyDownList[code];
        }

        Log.Debug("onkeyevent " + (down ? "down" : "up") +
                  ", keysym: " + keysym, ", code: " + code +
                  ", numlock: " + numlock + ", capslock: " + capslock);
        this.onkeyevent(keysym, code, down, numlock, capslock);
    }

    _getKeyCode(e) {
        const code = KeyboardUtil.getKeycode(e);
        if (code !== 'Unidentified') {
            return code;
        }


        if (e.keyCode) { // legacy events without a stable key identifier require a character fallback

            if (e.keyCode !== 229) { // code 229 identifies composition events
                return 'Platform' + e.keyCode;
            }
        }

        if (e.keyIdentifier) {

            if (e.keyIdentifier.substr(0, 2) !== 'U+') {
                return e.keyIdentifier;
            }

            const codepoint = parseInt(e.keyIdentifier.substr(2), 16);
            const char = String.fromCharCode(codepoint).toUpperCase();

            return 'Platform' + char.charCodeAt();
        }

        return 'Unidentified';
    }

    _handleKeyDown(e) {
        const code = this._getKeyCode(e);
        let keysym = KeyboardUtil.getKeysym(e);
        let numlock = e.getModifierState('NumLock');
        let capslock = e.getModifierState('CapsLock');

        if (browser.isMac() || browser.isIOS()) { // apple platforms report numlock as false even when the state is unknown
            numlock = null;
        }

        if (this._altGrArmed) { // windows represents altgr with adjacent synthetic control and alt events
            this._altGrArmed = false;
            clearTimeout(this._altGrTimeout);

            if ((code === "AltRight") &&
                ((e.timeStamp - this._altGrCtrlTime) < 50)) {

                keysym = KeyTable.XK_ISO_Level3_Shift; // manually held control can hide the synthetic event needed to detect altgr
            } else {
                this._sendKeyEvent(KeyTable.XK_Control_L, "ControlLeft", true, numlock, capslock);
            }
        }

        if (code === 'Unidentified') { // virtual keyboards can omit physical key identifiers
            if (keysym) {

                this._sendKeyEvent(keysym, code, true, numlock, capslock);
                this._sendKeyEvent(keysym, code, false, numlock, capslock);
            }

            stopEvent(e);
            return;
        }

        if (browser.isMac() || browser.isIOS()) { // apple alt keys use the alternate graphics behavior expected by remote servers
            switch (keysym) {
                case KeyTable.XK_Super_L:
                    keysym = KeyTable.XK_Alt_L;
                    break;
                case KeyTable.XK_Super_R:
                    keysym = KeyTable.XK_Super_L;
                    break;
                case KeyTable.XK_Alt_L:
                    keysym = KeyTable.XK_Mode_switch;
                    break;
                case KeyTable.XK_Alt_R:
                    keysym = KeyTable.XK_ISO_Level3_Shift;
                    break;
            }
        }

        if (code in this._keyDownList) { // a release must reuse the pressed keysym to avoid leaving a remote key held
            keysym = this._keyDownList[code];
        }

        if ((browser.isMac() || browser.isIOS()) && // apple platforms omit key releases while meta is held
            (e.metaKey && code !== 'MetaLeft' && code !== 'MetaRight')) {
            this._sendKeyEvent(keysym, code, true, numlock, capslock);
            this._sendKeyEvent(keysym, code, false, numlock, capslock);
            stopEvent(e);
            return;
        }

        if ((browser.isMac() || browser.isIOS()) && (code === 'CapsLock')) { // apple capslock reports state changes instead of separate press and release events
            this._sendKeyEvent(KeyTable.XK_Caps_Lock, 'CapsLock', true, numlock, capslock);
            this._sendKeyEvent(KeyTable.XK_Caps_Lock, 'CapsLock', false, numlock, capslock);
            stopEvent(e);
            return;
        }

        const jpBadKeys = [ KeyTable.XK_Zenkaku_Hankaku, // windows omits releases for some japanese input keys
                            KeyTable.XK_Eisu_toggle,
                            KeyTable.XK_Katakana,
                            KeyTable.XK_Hiragana,
                            KeyTable.XK_Romaji ];
        if (browser.isWindows() && jpBadKeys.includes(keysym)) {
            this._sendKeyEvent(keysym, code, true, numlock, capslock);
            this._sendKeyEvent(keysym, code, false, numlock, capslock);
            stopEvent(e);
            return;
        }

        stopEvent(e);


        if ((code === "ControlLeft") && browser.isWindows() &&
            !("ControlLeft" in this._keyDownList)) {
            this._altGrArmed = true;
            this._altGrTimeout = setTimeout(this._interruptAltGrSequence.bind(this), 100);
            this._altGrCtrlTime = e.timeStamp;
            return;
        }

        this._sendKeyEvent(keysym, code, true, numlock, capslock);
    }

    _handleKeyUp(e) {
        stopEvent(e);

        const code = this._getKeyCode(e);

        this._interruptAltGrSequence();


        if ((browser.isMac() || browser.isIOS()) && (code === 'CapsLock')) {
            this._sendKeyEvent(KeyTable.XK_Caps_Lock, 'CapsLock', true);
            this._sendKeyEvent(KeyTable.XK_Caps_Lock, 'CapsLock', false);
            return;
        }

        this._sendKeyEvent(this._keyDownList[code], code, false);

        if (browser.isWindows() && ((code === 'ShiftLeft') || // windows can omit a shift release while the other shift key remains held
                                    (code === 'ShiftRight'))) {
            if ('ShiftRight' in this._keyDownList) {
                this._sendKeyEvent(this._keyDownList['ShiftRight'],
                                   'ShiftRight', false);
            }
            if ('ShiftLeft' in this._keyDownList) {
                this._sendKeyEvent(this._keyDownList['ShiftLeft'],
                                   'ShiftLeft', false);
            }
        }
    }

    _interruptAltGrSequence() {
        if (this._altGrArmed) {
            this._altGrArmed = false;
            clearTimeout(this._altGrTimeout);
            this._sendKeyEvent(KeyTable.XK_Control_L, "ControlLeft", true);
        }
    }

    _allKeysUp() {
        Log.Debug(">> Keyboard.allKeysUp");


        this._interruptAltGrSequence();

        for (let code in this._keyDownList) {
            this._sendKeyEvent(this._keyDownList[code], code, false);
        }
        Log.Debug("<< Keyboard.allKeysUp");
    }

    grab() {


        this._target.addEventListener('keydown', this._eventHandlers.keydown);
        this._target.addEventListener('keyup', this._eventHandlers.keyup);


        window.addEventListener('blur', this._eventHandlers.blur);


    }

    ungrab() {


        this._target.removeEventListener('keydown', this._eventHandlers.keydown);
        this._target.removeEventListener('keyup', this._eventHandlers.keyup);
        window.removeEventListener('blur', this._eventHandlers.blur);


        this._allKeysUp();


    }
}
