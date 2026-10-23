/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 *
 * Browser feature support detection
 */

import * as Log from './logging.js';
import Base64 from '../base64.js';


export let isTouchDevice = ('ontouchstart' in document.documentElement) ||

                                 (document.ontouchstart !== undefined) || // chrome debugger touch emulation exposes touch support here

                                 (navigator.maxTouchPoints > 0) || // surface devices report touch capability through touch point counts
                                 (navigator.msMaxTouchPoints > 0);
window.addEventListener('touchstart', function onFirstTouch() {
    isTouchDevice = true;
    window.removeEventListener('touchstart', onFirstTouch, false);
}, false);

export let dragThreshold = 10 * (window.devicePixelRatio || 1); // pixel density approximates the same physical drag distance across displays

let _supportsCursorURIs = false;

try {
    const target = document.createElement('canvas');
    target.style.cursor = 'url("data:image/x-icon;base64,AAACAAEACAgAAAIAAgA4AQAAFgAAACgAAAAIAAAAEAAAAAEAIAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAD/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AAAAAAAAAAAAAAAAAAAAAA==") 2 2, default';

    if (target.style.cursor.indexOf("url") === 0) {
        Log.Info("Data URI scheme cursor supported");
        _supportsCursorURIs = true;
    } else {
        Log.Warn("Data URI scheme cursor not supported");
    }
} catch (exc) {
    Log.Error("Data URI scheme cursor test exception: " + exc);
}

export const supportsCursorURIs = _supportsCursorURIs;

let _hasScrollbarGutter = true;
try {

    const container = document.createElement('div');
    container.style.visibility = 'hidden';
    container.style.overflow = 'scroll';
    document.body.appendChild(container);


    const child = document.createElement('div');
    container.appendChild(child);

    const scrollbarWidth = (container.offsetWidth - child.offsetWidth);


    container.parentNode.removeChild(container);

    _hasScrollbarGutter = scrollbarWidth != 0;
} catch (exc) {
    Log.Error("Scrollbar test exception: " + exc);
}
export const hasScrollbarGutter = _hasScrollbarGutter;

export let supportsWebCodecsH264Decode = false;

async function _checkWebCodecsH264DecodeSupport() {
    if (!('VideoDecoder' in window)) {
        return false;
    }


    const config = {
        codec: 'avc1.42401f',
        codedWidth: 1920,
        codedHeight: 1080,
        optimizeForLatency: true,
    };

    let support = await VideoDecoder.isConfigSupported(config);
    if (!support.supported) {
        return false;
    }

    const data = new Uint8Array(Base64.decode( // firefox can report decoder support without decoding a real frame
        'AAAAAWdCwBTZnpuAgICgAAADACAAAAZB4oVNAAAAAWjJYyyAAAABBgX//4Hc' +
        'Rem95tlIt5Ys2CDZI+7veDI2NCAtIGNvcmUgMTY0IHIzMTA4IDMxZTE5Zjkg' +
        'LSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDIz' +
        'IC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2NC5odG1sIC0gb3B0aW9u' +
        'czogY2FiYWM9MCByZWY9NSBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgxOjB4' +
        'MTExIG1lPWhleCBzdWJtZT04IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAgbWl4' +
        'ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0yIDh4' +
        'OGRjdD0wIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJv' +
        'bWFfcXBfb2Zmc2V0PS0yIHRocmVhZHM9MSBsb29rYWhlYWRfdGhyZWFkcz0x' +
        'IHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9' +
        'MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVz' +
        'PTAgd2VpZ2h0cD0wIGtleWludD1pbmZpbml0ZSBrZXlpbnRfbWluPTI1IHNj' +
        'ZW5lY3V0PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9NTAgcmM9' +
        'YWJyIG1idHJlZT0xIGJpdHJhdGU9NDAwIHJhdGV0b2w9MS4wIHFjb21wPTAu' +
        'NjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFx' +
        'PTE6MS4wMACAAAABZYiEBrxmKAAPVccAAS044AA5DRJMnkycJk4TPw=='));

    let gotframe = false;
    let error = null;

    let decoder = new VideoDecoder({
        output: (frame) => { gotframe = true; },
        error: (e) => { error = e; },
    });
    let chunk = new EncodedVideoChunk({
        timestamp: 0,
        type: 'key',
        data: data,
    });

    decoder.configure(config);
    decoder.decode(chunk);
    try {
        await decoder.flush();
    } catch (e) {


        error = e; // firefox can throw while checking this decoder configuration
    }

    if (!gotframe) { // firefox on windows can omit the error callback, require a decoded frame
        return false;
    }

    if (error !== null) {
        return false;
    }

    return true;
}
supportsWebCodecsH264Decode = await _checkWebCodecsH264DecodeSupport();

export function isMac() { // feature detection avoids assumptions about browser and platform names
    return !!(/mac/i).exec(navigator.platform);
}

export function isWindows() {
    return !!(/win/i).exec(navigator.platform);
}

export function isIOS() {
    return (!!(/ipad/i).exec(navigator.platform) ||
            !!(/iphone/i).exec(navigator.platform) ||
            !!(/ipod/i).exec(navigator.platform));
}

export function isAndroid() {

    return !!navigator.userAgent.match('Android '); // android reports linux as its platform
}

export function isChromeOS() {

    return !!navigator.userAgent.match(' CrOS '); // chromeos reports linux as its platform
}

export function isSafari() {
    return !!navigator.userAgent.match('Safari/...') &&
           !navigator.userAgent.match('Chrome/...') &&
           !navigator.userAgent.match('Chromium/...') &&
           !navigator.userAgent.match('Epiphany/...');
}

export function isFirefox() {
    return !!navigator.userAgent.match('Firefox/...') &&
           !navigator.userAgent.match('Seamonkey/...');
}

export function isChrome() {
    return !!navigator.userAgent.match('Chrome/...') &&
           !navigator.userAgent.match('Chromium/...') &&
           !navigator.userAgent.match('Edg/...') &&
           !navigator.userAgent.match('OPR/...');
}

export function isChromium() {
    return !!navigator.userAgent.match('Chromium/...');
}

export function isOpera() {
    return !!navigator.userAgent.match('OPR/...');
}

export function isEdge() {
    return !!navigator.userAgent.match('Edg/...');
}

export function isGecko() {
    return !!navigator.userAgent.match('Gecko/...');
}

export function isWebKit() {
    return !!navigator.userAgent.match('AppleWebKit/...') &&
           !navigator.userAgent.match('Chrome/...');
}

export function isBlink() {
    return !!navigator.userAgent.match('Chrome/...');
}
