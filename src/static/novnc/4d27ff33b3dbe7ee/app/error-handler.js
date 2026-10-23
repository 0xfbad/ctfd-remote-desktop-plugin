/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 */

function handleError(event, err) {
    try {
        const msg = document.getElementById('noVNC_fallback_errormsg');

        if (event.message === "ResizeObserver loop completed with undelivered notifications.") { // firefox reports this observer notification without an application failure
            return false;
        }

        if (msg.hasChildNodes()) {
            return false;
        }

        let div = document.createElement("div");
        div.classList.add('noVNC_message');
        div.appendChild(document.createTextNode(event.message));
        msg.appendChild(div);

        if (event.filename) {
            div = document.createElement("div");
            div.className = 'noVNC_location';
            let text = event.filename;
            if (event.lineno !== undefined) {
                text += ":" + event.lineno;
                if (event.colno !== undefined) {
                    text += ":" + event.colno;
                }
            }
            div.appendChild(document.createTextNode(text));
            msg.appendChild(div);
        }

        if (err && err.stack) {
            div = document.createElement("div");
            div.className = 'noVNC_stack';
            div.appendChild(document.createTextNode(err.stack));
            msg.appendChild(div);
        }

        document.getElementById('noVNC_fallback_error')
            .classList.add("noVNC_open");

    } catch (exc) {
        document.write("noVNC encountered an error.");
    }

    try {

        if (document.activeElement) { document.activeElement.blur(); }

        let keyboardFocusable = 'a[href], button, input, textarea, select, details, [tabindex]';
        document.querySelectorAll(keyboardFocusable).forEach((elem) => {
            elem.setAttribute("tabindex", "-1");
        });
    } catch (exc) {

    }

    return false; // returning true would hide the uncaught error from the console
}

window.addEventListener('error', evt => handleError(evt, evt.error));
window.addEventListener('unhandledrejection', evt => handleError(evt.reason, evt.reason));
