/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 */


export function decodeUTF8(utf8string, allowLatin1=false) {
    try {
        return decodeURIComponent(escape(utf8string));
    } catch (e) {
        if (e instanceof URIError) {
            if (allowLatin1) {


                return utf8string;
            }
        }
        throw e;
    }
}


export function encodeUTF8(DOMString) {
    return unescape(encodeURIComponent(DOMString));
}
