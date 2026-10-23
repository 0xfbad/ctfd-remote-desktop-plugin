/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2018 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 */

export function getPointerEvent(e) {
    return e.changedTouches ? e.changedTouches[0] : e.touches ? e.touches[0] : e;
}

export function stopEvent(e) {
    e.stopPropagation();
    e.preventDefault();
}


let _captureRecursion = false;
let _elementForUnflushedEvents = null;
document.captureElement = null;
function _captureProxy(e) {

    if (_captureRecursion) return; // dispatching the cloned event reenters this handler


    const newEv = new e.constructor(e.type, e); // a dispatched event cannot be dispatched again

    _captureRecursion = true;
    if (document.captureElement) {
        document.captureElement.dispatchEvent(newEv);
    } else {
        _elementForUnflushedEvents.dispatchEvent(newEv);
    }
    _captureRecursion = false;


    e.stopPropagation();


    if (newEv.defaultPrevented) {
        e.preventDefault();
    }


    if (e.type === "mouseup") {
        releaseCapture();
    }
}


function _capturedElemChanged() {
    const proxyElem = document.getElementById("noVNC_mouse_capture_elem");
    proxyElem.style.cursor = window.getComputedStyle(document.captureElement).cursor;
}

const _captureObserver = new MutationObserver(_capturedElemChanged);

export function setCapture(target) {
    if (target.setCapture) {

        target.setCapture();
        document.captureElement = target;
    } else {


        releaseCapture();

        let proxyElem = document.getElementById("noVNC_mouse_capture_elem");

        if (proxyElem === null) {
            proxyElem = document.createElement("div");
            proxyElem.id = "noVNC_mouse_capture_elem";
            proxyElem.style.position = "fixed";
            proxyElem.style.top = "0px";
            proxyElem.style.left = "0px";
            proxyElem.style.width = "100%";
            proxyElem.style.height = "100%";
            proxyElem.style.zIndex = 10000;
            proxyElem.style.display = "none";
            document.body.appendChild(proxyElem);

            proxyElem.addEventListener('contextmenu', _captureProxy);

            proxyElem.addEventListener('mousemove', _captureProxy);
            proxyElem.addEventListener('mouseup', _captureProxy);
        }

        document.captureElement = target;


        _captureObserver.observe(target, {attributes: true});
        _capturedElemChanged();

        proxyElem.style.display = "";

        window.addEventListener('mousemove', _captureProxy); // window listeners keep tracking after the pointer leaves the viewport
        window.addEventListener('mouseup', _captureProxy);
    }
}

export function releaseCapture() {
    if (document.releaseCapture) {

        document.releaseCapture();
        document.captureElement = null;

    } else {
        if (!document.captureElement) {
            return;
        }

        _elementForUnflushedEvents = document.captureElement; // queued events still need the old captured element after capture is released
        document.captureElement = null;

        _captureObserver.disconnect();

        const proxyElem = document.getElementById("noVNC_mouse_capture_elem");
        proxyElem.style.display = "none";

        window.removeEventListener('mousemove', _captureProxy);
        window.removeEventListener('mouseup', _captureProxy);
    }
}
