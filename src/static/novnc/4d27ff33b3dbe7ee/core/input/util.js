import KeyTable from "./keysym.js";
import keysyms from "./keysymdef.js";
import vkeys from "./vkeys.js";
import fixedkeys from "./fixedkeys.js";
import DOMKeyTable from "./domkeytable.js";
import * as browser from "../util/browser.js";


export function getKeycode(evt) {

    if (evt.code) { // some browsers return empty key codes instead of leaving them undefined

        switch (evt.code) { // mozilla uses legacy names for some keys
            case 'OSLeft': return 'MetaLeft';
            case 'OSRight': return 'MetaRight';
        }

        return evt.code;
    }

    if (evt.keyCode in vkeys) { // nonprintable legacy events use windows virtual key codes
        let code = vkeys[evt.keyCode];


        if (browser.isMac() && (code === 'ContextMenu')) { // apple browsers report the wrong context menu code
            code = 'MetaRight';
        }

        if (evt.location === 2) { // legacy key codes do not distinguish modifier sides
            switch (code) {
                case 'ShiftLeft': return 'ShiftRight';
                case 'ControlLeft': return 'ControlRight';
                case 'AltLeft': return 'AltRight';
            }
        }


        if (evt.location === 3) {
            switch (code) {
                case 'Delete': return 'NumpadDecimal';
                case 'Insert': return 'Numpad0';
                case 'End': return 'Numpad1';
                case 'ArrowDown': return 'Numpad2';
                case 'PageDown': return 'Numpad3';
                case 'ArrowLeft': return 'Numpad4';
                case 'ArrowRight': return 'Numpad6';
                case 'Home': return 'Numpad7';
                case 'ArrowUp': return 'Numpad8';
                case 'PageUp': return 'Numpad9';
                case 'Enter': return 'NumpadEnter';
            }
        }

        return code;
    }

    return 'Unidentified';
}


export function getKey(evt) {

    if ((evt.key !== undefined) && (evt.key !== 'Unidentified')) {

        switch (evt.key) { // mozilla uses legacy names for some keys
            case 'OS': return 'Meta';
            case 'LaunchMyComputer': return 'LaunchApplication1';
            case 'LaunchCalculator': return 'LaunchApplication2';
        }


        switch (evt.key) { // ios can expose operating system names instead of standard key names
            case 'UIKeyInputUpArrow': return 'ArrowUp';
            case 'UIKeyInputDownArrow': return 'ArrowDown';
            case 'UIKeyInputLeftArrow': return 'ArrowLeft';
            case 'UIKeyInputRightArrow': return 'ArrowRight';
            case 'UIKeyInputEscape': return 'Escape';
        }


        if ((evt.key === '\x00') && (evt.code === 'NumpadDecimal')) { // chrome can report a null character for numpad delete
            return 'Delete';
        }

        return evt.key;
    }


    const code = getKeycode(evt);
    if (code in fixedkeys) {
        return fixedkeys[code];
    }


    if (evt.charCode) {
        return String.fromCharCode(evt.charCode);
    }


    return 'Unidentified';
}


export function getKeysym(evt) {
    const key = getKey(evt);

    if (key === 'Unidentified') {
        return null;
    }


    if (key in DOMKeyTable) {
        let location = evt.location;


        if ((key === 'Meta') && (location === 0)) { // safari reports the right command key at the standard location
            location = 2;
        }


        if ((key === 'Clear') && (location === 3)) { // safari reports clear as a numpad key for numlock
            let code = getKeycode(evt);
            if (code === 'NumLock') {
                location = 0;
            }
        }

        if ((location === undefined) || (location > 3)) {
            location = 0;
        }

        if (key === 'Meta') { // browsers can confuse the original meta key with the windows key
            let code = getKeycode(evt);
            if (code === 'AltLeft') {
                return KeyTable.XK_Meta_L;
            } else if (code === 'AltRight') {
                return KeyTable.XK_Meta_R;
            }
        }

        if (key === 'Clear') { // apple clear maps to numlock for remote systems
            let code = getKeycode(evt);
            if (code === 'NumLock') {
                return KeyTable.XK_Num_Lock;
            }
        }

        if (browser.isWindows()) { // japanese windows layouts alternate symbols without exposing the remote input state
            switch (key) {
                case 'Zenkaku':
                case 'Hankaku':
                    return KeyTable.XK_Zenkaku_Hankaku;
                case 'Romaji':
                case 'KanaMode':
                    return KeyTable.XK_Romaji;
            }
        }

        return DOMKeyTable[key][location];
    }

    if (key.length !== 1) {
        return null;
    }

    const codepoint = key.charCodeAt();
    if (codepoint) {
        return keysyms.lookup(codepoint);
    }

    return null;
}
