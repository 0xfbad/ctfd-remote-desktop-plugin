/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 */

import * as Log from '../core/util/logging.js';

export function initLogging(level) {
    "use strict";
    if (typeof level !== "undefined") {
        Log.initLogging(level);
    } else {
        const param = document.location.href.match(/logging=([A-Za-z0-9._-]*)/);
        Log.initLogging(param || undefined);
    }
}

export function getQueryVar(name, defVal) {
    "use strict";
    const re = new RegExp('.*[?&]' + name + '=([^&#]*)'),
          match = document.location.href.match(re);
    if (typeof defVal === 'undefined') { defVal = null; }

    if (match) {
        return decodeURIComponent(match[1]);
    }

    return defVal;
}

export function getHashVar(name, defVal) {
    "use strict";
    const re = new RegExp('.*[&#]' + name + '=([^&]*)'),
          match = document.location.hash.match(re);
    if (typeof defVal === 'undefined') { defVal = null; }

    if (match) {
        return decodeURIComponent(match[1]);
    }

    return defVal;
}

export function getConfigVar(name, defVal) {
    "use strict";
    const val = getHashVar(name);

    if (val === null) {
        return getQueryVar(name, defVal);
    }

    return val;
}

/*
 * Cookie handling. Dervied from: http://www.quirksmode.org/js/cookies.html
 */

export function createCookie(name, value, days) {
    "use strict";
    let date, expires;
    if (days) {
        date = new Date();
        date.setTime(date.getTime() + (days * 24 * 60 * 60 * 1000));
        expires = "; expires=" + date.toGMTString();
    } else {
        expires = "";
    }

    let secure;
    if (document.location.protocol === "https:") {
        secure = "; secure";
    } else {
        secure = "";
    }
    document.cookie = name + "=" + value + expires + "; path=/" + secure;
}

export function readCookie(name, defaultValue) {
    "use strict";
    const nameEQ = name + "=";
    const ca = document.cookie.split(';');

    for (let i = 0; i < ca.length; i += 1) {
        let c = ca[i];
        while (c.charAt(0) === ' ') {
            c = c.substring(1, c.length);
        }
        if (c.indexOf(nameEQ) === 0) {
            return c.substring(nameEQ.length, c.length);
        }
    }

    return (typeof defaultValue !== 'undefined') ? defaultValue : null;
}

export function eraseCookie(name) {
    "use strict";
    createCookie(name, "", -1);
}

let settings = {};

export function initSettings() {
    if (!window.chrome || !window.chrome.storage) {
        settings = {};
        return Promise.resolve();
    }

    return new Promise(resolve => window.chrome.storage.sync.get(resolve))
        .then((cfg) => { settings = cfg; });
}

export function setSetting(name, value) {
    settings[name] = value;
}

export function writeSetting(name, value) {
    "use strict";
    if (settings[name] === value) return;
    settings[name] = value;
    if (window.chrome && window.chrome.storage) {
        window.chrome.storage.sync.set(settings);
    } else {
        localStorageSet(name, value);
    }
}

export function readSetting(name, defaultValue) {
    "use strict";
    let value;
    if ((name in settings) || (window.chrome && window.chrome.storage)) {
        value = settings[name];
    } else {
        value = localStorageGet(name);
        settings[name] = value;
    }
    if (typeof value === "undefined") {
        value = null;
    }

    if (value === null && typeof defaultValue !== "undefined") {
        return defaultValue;
    }

    return value;
}

export function eraseSetting(name) {
    "use strict";

    delete settings[name];
    if (window.chrome && window.chrome.storage) {
        window.chrome.storage.sync.remove(name);
    } else {
        localStorageRemove(name);
    }
}

let loggedMsgs = [];
function logOnce(msg, level = "warn") {
    if (!loggedMsgs.includes(msg)) {
        switch (level) {
            case "error":
                Log.Error(msg);
                break;
            case "warn":
                Log.Warn(msg);
                break;
            case "debug":
                Log.Debug(msg);
                break;
            default:
                Log.Info(msg);
        }
        loggedMsgs.push(msg);
    }
}

let cookiesMsg = "Couldn't access noVNC settings, are cookies disabled?";

function localStorageGet(name) {
    let r;
    try {
        r = localStorage.getItem(name);
    } catch (e) {
        if (e instanceof DOMException) {
            logOnce(cookiesMsg);
            logOnce("'localStorage.getItem(" + name + ")' failed: " + e,
                    "debug");
        } else {
            throw e;
        }
    }
    return r;
}
function localStorageSet(name, value) {
    try {
        localStorage.setItem(name, value);
    } catch (e) {
        if (e instanceof DOMException) {
            logOnce(cookiesMsg);
            logOnce("'localStorage.setItem(" + name + "," + value +
                    ")' failed: " + e, "debug");
        } else {
            throw e;
        }
    }
}
function localStorageRemove(name) {
    try {
        localStorage.removeItem(name);
    } catch (e) {
        if (e instanceof DOMException) {
            logOnce(cookiesMsg);
            logOnce("'localStorage.removeItem(" + name + ")' failed: " + e,
                    "debug");
        } else {
            throw e;
        }
    }
}
