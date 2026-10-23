/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2020 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 *
 */

import { toUnsigned32bit, toSigned32bit } from './util/int.js';
import * as Log from './util/logging.js';
import { encodeUTF8, decodeUTF8 } from './util/strings.js';
import { dragThreshold, supportsWebCodecsH264Decode } from './util/browser.js';
import { clientToElement } from './util/element.js';
import { setCapture } from './util/events.js';
import EventTargetMixin from './util/eventtarget.js';
import Display from "./display.js";
import Inflator from "./inflator.js";
import Deflator from "./deflator.js";
import Keyboard from "./input/keyboard.js";
import GestureHandler from "./input/gesturehandler.js";
import Cursor from "./util/cursor.js";
import Websock from "./websock.js";
import KeyTable from "./input/keysym.js";
import XtScancode from "./input/xtscancodes.js";
import { encodings } from "./encodings.js";
import RSAAESAuthenticationState from "./ra2.js";
import legacyCrypto from "./crypto/crypto.js";

import RawDecoder from "./decoders/raw.js";
import CopyRectDecoder from "./decoders/copyrect.js";
import RREDecoder from "./decoders/rre.js";
import HextileDecoder from "./decoders/hextile.js";
import ZlibDecoder from './decoders/zlib.js';
import TightDecoder from "./decoders/tight.js";
import TightPNGDecoder from "./decoders/tightpng.js";
import ZRLEDecoder from "./decoders/zrle.js";
import JPEGDecoder from "./decoders/jpeg.js";
import H264Decoder from "./decoders/h264.js";


const DISCONNECT_TIMEOUT = 3;
const DEFAULT_BACKGROUND = 'rgb(40, 40, 40)';


const MOUSE_MOVE_DELAY = 17;


const WHEEL_STEP = 50;
const WHEEL_LINE_HEIGHT = 19;


const GESTURE_ZOOMSENS = 75;
const GESTURE_SCRLSENS = 50;
const DOUBLE_TAP_TIMEOUT = 1000;
const DOUBLE_TAP_THRESHOLD = 50;


const securityTypeNone              = 1;
const securityTypeVNCAuth           = 2;
const securityTypeRA2ne             = 6;
const securityTypeTight             = 16;
const securityTypeVeNCrypt          = 19;
const securityTypeXVP               = 22;
const securityTypeARD               = 30;
const securityTypeMSLogonII         = 113;


const securityTypeUnixLogon         = 129;


const securityTypePlain             = 256;


const extendedClipboardFormatText   = 1;
/*eslint-disable no-unused-vars */
const extendedClipboardFormatRtf    = 1 << 1;
const extendedClipboardFormatHtml   = 1 << 2;
const extendedClipboardFormatDib    = 1 << 3;
const extendedClipboardFormatFiles  = 1 << 4;
/*eslint-enable */


const extendedClipboardActionCaps    = 1 << 24;
const extendedClipboardActionRequest = 1 << 25;
const extendedClipboardActionPeek    = 1 << 26;
const extendedClipboardActionNotify  = 1 << 27;
const extendedClipboardActionProvide = 1 << 28;

export default class RFB extends EventTargetMixin {
    constructor(target, urlOrChannel, options) {
        if (!target) {
            throw new Error("Must specify target");
        }
        if (!urlOrChannel) {
            throw new Error("Must specify URL, WebSocket or RTCDataChannel");
        }

        if (!window.isSecureContext) { // cryptographic browser apis can require a secure context
            Log.Error("noVNC requires a secure context (TLS). Expect crashes!");
        }

        super();

        this._target = target;

        if (typeof urlOrChannel === "string") {
            this._url = urlOrChannel;
        } else {
            this._url = null;
            this._rawChannel = urlOrChannel;
        }


        options = options || {};
        this._rfbCredentials = options.credentials || {};
        this._shared = 'shared' in options ? !!options.shared : true;
        this._repeaterID = options.repeaterID || '';
        this._wsProtocols = options.wsProtocols || [];


        this._rfbConnectionState = '';
        this._rfbInitState = '';
        this._rfbAuthScheme = -1;
        this._rfbCleanDisconnect = true;
        this._rfbRSAAESAuthenticationState = null;


        this._rfbVersion = 0;
        this._rfbMaxVersion = 3.8;
        this._rfbTightVNC = false;
        this._rfbVeNCryptState = 0;
        this._rfbXvpVer = 0;

        this._fbWidth = 0;
        this._fbHeight = 0;

        this._fbName = "";

        this._capabilities = { power: false };

        this._supportsFence = false;

        this._supportsContinuousUpdates = false;
        this._enabledContinuousUpdates = false;

        this._supportsSetDesktopSize = false;
        this._screenID = 0;
        this._screenFlags = 0;
        this._pendingRemoteResize = false;
        this._lastResize = 0;

        this._qemuExtKeyEventSupported = false;

        this._extendedPointerEventSupported = false;

        this._clipboardText = null;
        this._clipboardServerCapabilitiesActions = {};
        this._clipboardServerCapabilitiesFormats = {};


        this._sock = null;
        this._display = null;
        this._flushing = false;
        this._keyboard = null;
        this._gestures = null;
        this._resizeObserver = null;


        this._disconnTimer = null;
        this._resizeTimeout = null;
        this._mouseMoveTimer = null;


        this._decoders = {};

        this._FBU = {
            rects: 0,
            x: 0,
            y: 0,
            width: 0,
            height: 0,
            encoding: null,
        };


        this._mousePos = {};
        this._mouseButtonMask = 0;
        this._mouseLastMoveTime = 0;
        this._viewportDragging = false;
        this._viewportDragPos = {};
        this._viewportHasMoved = false;
        this._accumulatedWheelDeltaX = 0;
        this._accumulatedWheelDeltaY = 0;


        this._gestureLastTapTime = null;
        this._gestureFirstDoubleTapEv = null;
        this._gestureLastMagnitudeX = 0;
        this._gestureLastMagnitudeY = 0;


        this._eventHandlers = {
            focusCanvas: this._focusCanvas.bind(this),
            handleResize: this._handleResize.bind(this),
            handleMouse: this._handleMouse.bind(this),
            handleWheel: this._handleWheel.bind(this),
            handleGesture: this._handleGesture.bind(this),
            handleRSAAESCredentialsRequired: this._handleRSAAESCredentialsRequired.bind(this),
            handleRSAAESServerVerification: this._handleRSAAESServerVerification.bind(this),
        };


        Log.Debug(">> RFB.constructor");


        this._screen = document.createElement('div');
        this._screen.style.display = 'flex';
        this._screen.style.width = '100%';
        this._screen.style.height = '100%';
        this._screen.style.overflow = 'auto';
        this._screen.style.background = DEFAULT_BACKGROUND;
        this._canvas = document.createElement('canvas');
        this._canvas.style.margin = 'auto';

        this._canvas.style.outline = 'none'; // some browsers add a focus outline to the target
        this._canvas.width = 0;
        this._canvas.height = 0;
        this._canvas.tabIndex = -1;
        this._screen.appendChild(this._canvas);


        this._cursor = new Cursor();

        this._cursorImage = RFB.cursors.none; // tightvnc can omit the initial cursor until it changes, other viewers start with an arrow


        this._decoders[encodings.encodingRaw] = new RawDecoder();
        this._decoders[encodings.encodingCopyRect] = new CopyRectDecoder();
        this._decoders[encodings.encodingRRE] = new RREDecoder();
        this._decoders[encodings.encodingHextile] = new HextileDecoder();
        this._decoders[encodings.encodingZlib] = new ZlibDecoder();
        this._decoders[encodings.encodingTight] = new TightDecoder();
        this._decoders[encodings.encodingTightPNG] = new TightPNGDecoder();
        this._decoders[encodings.encodingZRLE] = new ZRLEDecoder();
        this._decoders[encodings.encodingJPEG] = new JPEGDecoder();
        this._decoders[encodings.encodingH264] = new H264Decoder();

        try { // capability checks can throw before teardown is safe
            this._display = new Display(this._canvas);
        } catch (exc) {
            Log.Error("Display exception: " + exc);
            throw exc;
        }

        this._keyboard = new Keyboard(this._canvas);
        this._keyboard.onkeyevent = this._handleKeyEvent.bind(this);
        this._remoteCapsLock = null;
        this._remoteNumLock = null;

        this._gestures = new GestureHandler();

        this._sock = new Websock();
        this._sock.on('open', this._socketOpen.bind(this));
        this._sock.on('close', this._socketClose.bind(this));
        this._sock.on('message', this._handleMessage.bind(this));
        this._sock.on('error', this._socketError.bind(this));

        this._expectedClientWidth = null;
        this._expectedClientHeight = null;
        this._resizeObserver = new ResizeObserver(this._eventHandlers.handleResize);


        this._updateConnectionState('connecting');

        Log.Debug("<< RFB.constructor");

        this.dragViewport = false;
        this.focusOnClick = true;

        this._viewOnly = false;
        this._clipViewport = false;
        this._clippingViewport = false;
        this._scaleViewport = false;
        this._resizeSession = false;

        this._showDotCursor = false;
        if (options.showDotCursor !== undefined) {
            Log.Warn("Specifying showDotCursor as a RFB constructor argument is deprecated");
            this._showDotCursor = options.showDotCursor;
        }

        this._qualityLevel = 6;
        this._compressionLevel = 2;
    }

    get viewOnly() { return this._viewOnly; }
    set viewOnly(viewOnly) {
        this._viewOnly = viewOnly;

        if (this._rfbConnectionState === "connecting" ||
            this._rfbConnectionState === "connected") {
            if (viewOnly) {
                this._keyboard.ungrab();
            } else {
                this._keyboard.grab();
            }
        }
    }

    get capabilities() { return this._capabilities; }

    get clippingViewport() { return this._clippingViewport; }
    _setClippingViewport(on) {
        if (on === this._clippingViewport) {
            return;
        }
        this._clippingViewport = on;
        this.dispatchEvent(new CustomEvent("clippingviewport",
                                           { detail: this._clippingViewport }));
    }

    get touchButton() { return 0; }
    set touchButton(button) { Log.Warn("Using old API!"); }

    get clipViewport() { return this._clipViewport; }
    set clipViewport(viewport) {
        this._clipViewport = viewport;
        this._updateClip();
    }

    get scaleViewport() { return this._scaleViewport; }
    set scaleViewport(scale) {
        this._scaleViewport = scale;


        if (scale && this._clipViewport) { // scaling takes precedence over viewport clipping
            this._updateClip();
        }
        this._updateScale();
        if (!scale && this._clipViewport) {
            this._updateClip();
        }
    }

    get resizeSession() { return this._resizeSession; }
    set resizeSession(resize) {
        this._resizeSession = resize;
        if (resize) {
            this._requestRemoteResize();
        }
    }

    get showDotCursor() { return this._showDotCursor; }
    set showDotCursor(show) {
        this._showDotCursor = show;
        this._refreshCursor();
    }

    get background() { return this._screen.style.background; }
    set background(cssValue) { this._screen.style.background = cssValue; }

    get qualityLevel() {
        return this._qualityLevel;
    }
    set qualityLevel(qualityLevel) {
        if (!Number.isInteger(qualityLevel) || qualityLevel < 0 || qualityLevel > 9) {
            Log.Error("qualityLevel must be an integer between 0 and 9");
            return;
        }

        if (this._qualityLevel === qualityLevel) {
            return;
        }

        this._qualityLevel = qualityLevel;

        if (this._rfbConnectionState === 'connected') {
            this._sendEncodings();
        }
    }

    get compressionLevel() {
        return this._compressionLevel;
    }
    set compressionLevel(compressionLevel) {
        if (!Number.isInteger(compressionLevel) || compressionLevel < 0 || compressionLevel > 9) {
            Log.Error("compressionLevel must be an integer between 0 and 9");
            return;
        }

        if (this._compressionLevel === compressionLevel) {
            return;
        }

        this._compressionLevel = compressionLevel;

        if (this._rfbConnectionState === 'connected') {
            this._sendEncodings();
        }
    }

    disconnect() {
        this._updateConnectionState('disconnecting');
        this._sock.off('error');
        this._sock.off('message');
        this._sock.off('open');
        if (this._rfbRSAAESAuthenticationState !== null) {
            this._rfbRSAAESAuthenticationState.disconnect();
        }
    }

    approveServer() {
        if (this._rfbRSAAESAuthenticationState !== null) {
            this._rfbRSAAESAuthenticationState.approveServer();
        }
    }

    sendCredentials(creds) {
        this._rfbCredentials = creds;
        this._resumeAuthentication();
    }

    sendCtrlAltDel() {
        if (this._rfbConnectionState !== 'connected' || this._viewOnly) { return; }
        Log.Info("Sending Ctrl-Alt-Del");

        this.sendKey(KeyTable.XK_Control_L, "ControlLeft", true);
        this.sendKey(KeyTable.XK_Alt_L, "AltLeft", true);
        this.sendKey(KeyTable.XK_Delete, "Delete", true);
        this.sendKey(KeyTable.XK_Delete, "Delete", false);
        this.sendKey(KeyTable.XK_Alt_L, "AltLeft", false);
        this.sendKey(KeyTable.XK_Control_L, "ControlLeft", false);
    }

    machineShutdown() {
        this._xvpOp(1, 2);
    }

    machineReboot() {
        this._xvpOp(1, 3);
    }

    machineReset() {
        this._xvpOp(1, 4);
    }

    sendKey(keysym, code, down) {
        if (this._rfbConnectionState !== 'connected' || this._viewOnly) { return; }

        if (down === undefined) {
            this.sendKey(keysym, code, true);
            this.sendKey(keysym, code, false);
            return;
        }

        const scancode = XtScancode[code];

        if (this._qemuExtKeyEventSupported && scancode) {

            keysym = keysym || 0;

            Log.Info("Sending key (" + (down ? "down" : "up") + "): keysym " + keysym + ", scancode " + scancode);

            RFB.messages.QEMUExtendedKeyEvent(this._sock, keysym, down, scancode);
        } else {
            if (!keysym) {
                return;
            }
            Log.Info("Sending keysym (" + (down ? "down" : "up") + "): " + keysym);
            RFB.messages.keyEvent(this._sock, keysym, down ? 1 : 0);
        }
    }

    focus(options) {
        this._canvas.focus(options);
    }

    blur() {
        this._canvas.blur();
    }

    clipboardPasteFrom(text) {
        if (this._rfbConnectionState !== 'connected' || this._viewOnly) { return; }

        if (this._clipboardServerCapabilitiesFormats[extendedClipboardFormatText] &&
            this._clipboardServerCapabilitiesActions[extendedClipboardActionNotify]) {

            this._clipboardText = text;
            RFB.messages.extendedClipboardNotify(this._sock, [extendedClipboardFormatText]);
        } else {
            let length, i;
            let data;

            length = 0;
            // eslint-disable-next-line no-unused-vars
            for (let codePoint of text) {
                length++;
            }

            data = new Uint8Array(length);

            i = 0;
            for (let codePoint of text) {
                let code = codePoint.codePointAt(0);


                if (code > 0xff) { // legacy clipboard messages support only latin1 text
                    code = 0x3f;
                }

                data[i++] = code;
            }

            RFB.messages.clientCutText(this._sock, data);
        }
    }

    getImageData() {
        return this._display.getImageData();
    }

    toDataURL(type, encoderOptions) {
        return this._display.toDataURL(type, encoderOptions);
    }

    toBlob(callback, type, quality) {
        return this._display.toBlob(callback, type, quality);
    }

    _connect() {
        Log.Debug(">> RFB.connect");

        if (this._url) {
            Log.Info(`connecting to ${this._url}`);
            this._sock.open(this._url, this._wsProtocols);
        } else {
            Log.Info(`attaching ${this._rawChannel} to Websock`);
            this._sock.attach(this._rawChannel);

            if (this._sock.readyState === 'closed') {
                throw Error("Cannot use already closed WebSocket/RTCDataChannel");
            }

            if (this._sock.readyState === 'open') {

                this._socketOpen();
            }
        }


        this._target.appendChild(this._screen);

        this._gestures.attach(this._canvas);

        this._cursor.attach(this._canvas);
        this._refreshCursor();


        this._resizeObserver.observe(this._screen);


        this._canvas.addEventListener("mousedown", this._eventHandlers.focusCanvas);
        this._canvas.addEventListener("touchstart", this._eventHandlers.focusCanvas);


        this._canvas.addEventListener('mousedown', this._eventHandlers.handleMouse);
        this._canvas.addEventListener('mouseup', this._eventHandlers.handleMouse);
        this._canvas.addEventListener('mousemove', this._eventHandlers.handleMouse);

        this._canvas.addEventListener('click', this._eventHandlers.handleMouse); // middle click paste is not canceled by preventing the mouse down event


        this._canvas.addEventListener('contextmenu', this._eventHandlers.handleMouse);


        this._canvas.addEventListener("wheel", this._eventHandlers.handleWheel);


        this._canvas.addEventListener("gesturestart", this._eventHandlers.handleGesture);
        this._canvas.addEventListener("gesturemove", this._eventHandlers.handleGesture);
        this._canvas.addEventListener("gestureend", this._eventHandlers.handleGesture);

        Log.Debug("<< RFB.connect");
    }

    _disconnect() {
        Log.Debug(">> RFB.disconnect");
        this._cursor.detach();
        this._canvas.removeEventListener("gesturestart", this._eventHandlers.handleGesture);
        this._canvas.removeEventListener("gesturemove", this._eventHandlers.handleGesture);
        this._canvas.removeEventListener("gestureend", this._eventHandlers.handleGesture);
        this._canvas.removeEventListener("wheel", this._eventHandlers.handleWheel);
        this._canvas.removeEventListener('mousedown', this._eventHandlers.handleMouse);
        this._canvas.removeEventListener('mouseup', this._eventHandlers.handleMouse);
        this._canvas.removeEventListener('mousemove', this._eventHandlers.handleMouse);
        this._canvas.removeEventListener('click', this._eventHandlers.handleMouse);
        this._canvas.removeEventListener('contextmenu', this._eventHandlers.handleMouse);
        this._canvas.removeEventListener("mousedown", this._eventHandlers.focusCanvas);
        this._canvas.removeEventListener("touchstart", this._eventHandlers.focusCanvas);
        this._resizeObserver.disconnect();
        this._keyboard.ungrab();
        this._gestures.detach();
        this._sock.close();
        try {
            this._target.removeChild(this._screen);
        } catch (e) {
            if (e.name === 'NotFoundError') {


            } else { // early connection failures can precede screen creation
                throw e;
            }
        }
        clearTimeout(this._resizeTimeout);
        clearTimeout(this._mouseMoveTimer);
        Log.Debug("<< RFB.disconnect");
    }

    _socketOpen() {
        if ((this._rfbConnectionState === 'connecting') &&
            (this._rfbInitState === '')) {
            this._rfbInitState = 'ProtocolVersion';
            Log.Debug("Starting VNC handshake");
        } else {
            this._fail("Unexpected server connection while " +
                       this._rfbConnectionState);
        }
    }

    _socketClose(e) {
        Log.Debug("WebSocket on-close event");
        let msg = "";
        if (e.code) {
            msg = "(code: " + e.code;
            if (e.reason) {
                msg += ", reason: " + e.reason;
            }
            msg += ")";
        }
        switch (this._rfbConnectionState) {
            case 'connecting':
                this._fail("Connection closed " + msg);
                break;
            case 'connected':

                this._updateConnectionState('disconnecting');
                this._updateConnectionState('disconnected');
                break;
            case 'disconnecting':

                this._updateConnectionState('disconnected');
                break;
            case 'disconnected':
                this._fail("Unexpected server disconnect " +
                           "when already disconnected " + msg);
                break;
            default:
                this._fail("Unexpected server disconnect before connecting " +
                           msg);
                break;
        }
        this._sock.off('close');

        this._rawChannel = null;
    }

    _socketError(e) {
        Log.Warn("WebSocket on-error event");
    }

    _focusCanvas(event) {
        if (!this.focusOnClick) {
            return;
        }

        this.focus({ preventScroll: true });
    }

    _setDesktopName(name) {
        this._fbName = name;
        this.dispatchEvent(new CustomEvent(
            "desktopname",
            { detail: { name: this._fbName } }));
    }

    _saveExpectedClientSize() {
        this._expectedClientWidth = this._screen.clientWidth;
        this._expectedClientHeight = this._screen.clientHeight;
    }

    _currentClientSize() {
        return [this._screen.clientWidth, this._screen.clientHeight];
    }

    _clientHasExpectedSize() {
        const [currentWidth, currentHeight] = this._currentClientSize();
        return currentWidth == this._expectedClientWidth &&
            currentHeight == this._expectedClientHeight;
    }


    _handleResize() {

        if (this._clientHasExpectedSize()) {
            return;
        }


        window.requestAnimationFrame(() => {
            this._updateClip();
            this._updateScale();
            this._saveExpectedClientSize();
        });

        this._requestRemoteResize();
    }

    _updateClip() {
        const curClip = this._display.clipViewport;
        let newClip = this._clipViewport;

        if (this._scaleViewport) {

            newClip = false;
        }

        if (curClip !== newClip) {
            this._display.clipViewport = newClip;
        }

        if (newClip) {


            const size = this._screenSize(); // clipping limits the framebuffer to its container
            this._display.viewportChangeSize(size.w, size.h);
            this._fixScrollbars();
            this._setClippingViewport(size.w < this._display.width ||
                                      size.h < this._display.height);
        } else {
            this._setClippingViewport(false);
        }

        if (curClip !== newClip) {
            this._saveExpectedClientSize();
        }
    }

    _updateScale() {
        if (!this._scaleViewport) {
            this._display.scale = 1.0;
        } else {
            const size = this._screenSize();
            this._display.autoscale(size.w, size.h);
        }
        this._fixScrollbars();
    }

    _requestRemoteResize() { // the server must advertise desktop resizing before receiving resize requests
        if (!this._resizeSession) {
            return;
        }
        if (this._viewOnly) {
            return;
        }
        if (!this._supportsSetDesktopSize) {
            return;
        }


        if (this._pendingRemoteResize) {
            return;
        }


        if ((Date.now() - this._lastResize) < 100) {
            clearTimeout(this._resizeTimeout);
            this._resizeTimeout = setTimeout(this._requestRemoteResize.bind(this),
                                             100 - (Date.now() - this._lastResize));
            return;
        }
        this._resizeTimeout = null;

        const size = this._screenSize();


        if (size.w === this._fbWidth && size.h === this._fbHeight) {
            return;
        }

        this._pendingRemoteResize = true;
        this._lastResize = Date.now();
        RFB.messages.setDesktopSize(this._sock,
                                    Math.floor(size.w), Math.floor(size.h),
                                    this._screenID, this._screenFlags);

        Log.Debug('Requested new desktop size: ' +
                   size.w + 'x' + size.h);
    }


    _screenSize() {
        let r = this._screen.getBoundingClientRect();
        return { w: r.width, h: r.height };
    }

    _fixScrollbars() {

        const orig = this._screen.style.overflow; // safari can retain stale scrollbar layout until overflow is toggled
        this._screen.style.overflow = 'hidden';


        this._screen.getBoundingClientRect(); // reading dimensions forces safari to recalculate layout
        this._screen.style.overflow = orig;
    }

    _updateConnectionState(state) {
        const oldstate = this._rfbConnectionState;

        if (state === oldstate) {
            Log.Debug("Already in state '" + state + "', ignoring");
            return;
        }


        if (oldstate === 'disconnected') { // disconnected objects cannot reconnect
            Log.Error("Tried changing state of a disconnected RFB object");
            return;
        }


        switch (state) {
            case 'connected':
                if (oldstate !== 'connecting') {
                    Log.Error("Bad transition to connected state, " +
                               "previous connection state: " + oldstate);
                    return;
                }
                break;

            case 'disconnected':
                if (oldstate !== 'disconnecting') {
                    Log.Error("Bad transition to disconnected state, " +
                               "previous connection state: " + oldstate);
                    return;
                }
                break;

            case 'connecting':
                if (oldstate !== '') {
                    Log.Error("Bad transition to connecting state, " +
                               "previous connection state: " + oldstate);
                    return;
                }
                break;

            case 'disconnecting':
                if (oldstate !== 'connected' && oldstate !== 'connecting') {
                    Log.Error("Bad transition to disconnecting state, " +
                               "previous connection state: " + oldstate);
                    return;
                }
                break;

            default:
                Log.Error("Unknown connection state: " + state);
                return;
        }

        this._rfbConnectionState = state;

        Log.Debug("New state '" + state + "', was '" + oldstate + "'.");

        if (this._disconnTimer && state !== 'disconnecting') {
            Log.Debug("Clearing disconnect timer");
            clearTimeout(this._disconnTimer);
            this._disconnTimer = null;


            this._sock.off('close');
        }

        switch (state) {
            case 'connecting':
                this._connect();
                break;

            case 'connected':
                this.dispatchEvent(new CustomEvent("connect", { detail: {} }));
                break;

            case 'disconnecting':
                this._disconnect();

                this._disconnTimer = setTimeout(() => {
                    Log.Error("Disconnection timed out.");
                    this._updateConnectionState('disconnected');
                }, DISCONNECT_TIMEOUT * 1000);
                break;

            case 'disconnected':
                this.dispatchEvent(new CustomEvent(
                    "disconnect", { detail:
                                    { clean: this._rfbCleanDisconnect } }));
                break;
        }
    }

    _fail(details) { // failure details are logged without exposing them to the interface
        switch (this._rfbConnectionState) {
            case 'disconnecting':
                Log.Error("Failed when disconnecting: " + details);
                break;
            case 'connected':
                Log.Error("Failed while connected: " + details);
                break;
            case 'connecting':
                Log.Error("Failed when connecting: " + details);
                break;
            default:
                Log.Error("RFB failure: " + details);
                break;
        }
        this._rfbCleanDisconnect = false;


        this._updateConnectionState('disconnecting');
        this._updateConnectionState('disconnected');

        return false;
    }

    _setCapability(cap, val) {
        this._capabilities[cap] = val;
        this.dispatchEvent(new CustomEvent("capabilities",
                                           { detail: { capabilities: this._capabilities } }));
    }

    _handleMessage() {
        if (this._sock.rQwait("message", 1)) {
            Log.Warn("handleMessage called on an empty receive queue");
            return;
        }

        switch (this._rfbConnectionState) {
            case 'disconnected':
                Log.Error("Got data while disconnected");
                break;
            case 'connected':
                while (true) {
                    if (this._flushing) {
                        break;
                    }
                    if (!this._normalMsg()) {
                        break;
                    }
                    if (this._sock.rQwait("message", 1)) {
                        break;
                    }
                }
                break;
            case 'connecting':
                while (this._rfbConnectionState === 'connecting') {
                    if (!this._initMsg()) {
                        break;
                    }
                }
                break;
            default:
                Log.Error("Got data while in an invalid state");
                break;
        }
    }

    _handleKeyEvent(keysym, code, down, numlock, capslock) {

        if (code == 'CapsLock' && down) { // remote lock state updates arrive later, clear the cached state to avoid duplicate corrections
            this._remoteCapsLock = null;
        }
        if (this._remoteCapsLock !== null && capslock !== null && this._remoteCapsLock !== capslock && down) {
            Log.Debug("Fixing remote caps lock");

            this.sendKey(KeyTable.XK_Caps_Lock, 'CapsLock', true);
            this.sendKey(KeyTable.XK_Caps_Lock, 'CapsLock', false);


            this._remoteCapsLock = null;
        }


        if (code == 'NumLock' && down) {
            this._remoteNumLock = null;
        }
        if (this._remoteNumLock !== null && numlock !== null && this._remoteNumLock !== numlock && down) {
            Log.Debug("Fixing remote num lock");
            this.sendKey(KeyTable.XK_Num_Lock, 'NumLock', true);
            this.sendKey(KeyTable.XK_Num_Lock, 'NumLock', false);
            this._remoteNumLock = null;
        }
        this.sendKey(keysym, code, down);
    }

    static _convertButtonMask(buttons) {

        const buttonMaskMap = {
            0: 1 << 0,
            1: 1 << 2,
            2: 1 << 1,
            3: 1 << 7,
            4: 1 << 8,
        };

        let bmask = 0;
        for (let i = 0; i < 5; i++) {
            if (buttons & (1 << i)) {
                bmask |= buttonMaskMap[i];
            }
        }
        return bmask;
    }

    _handleMouse(ev) {

        if (ev.type === 'click') { // mouse events can control the viewport even while disconnected or viewing only

            if (ev.target !== this._canvas) { // click events require document listeners because target listeners can miss them
                return;
            }
        }

        ev.stopPropagation();
        ev.preventDefault();

        if ((ev.type === 'click') || (ev.type === 'contextmenu')) {
            return;
        }

        let pos = clientToElement(ev.clientX, ev.clientY,
                                  this._canvas);

        let bmask = RFB._convertButtonMask(ev.buttons);

        let down = ev.type == 'mousedown';
        switch (ev.type) {
            case 'mousedown':
            case 'mouseup':
                if (this.dragViewport) {
                    if (down && !this._viewportDragging) {
                        this._viewportDragging = true;
                        this._viewportDragPos = {'x': pos.x, 'y': pos.y};
                        this._viewportHasMoved = false;

                        this._flushMouseMoveTimer(pos.x, pos.y);

                        this._mouseButtonMask = bmask;
                        break;
                    } else {
                        this._viewportDragging = false;

                        if (this._viewportHasMoved) {
                            this._mouseButtonMask = bmask;
                            break;
                        }

                        this._sendMouse(pos.x, pos.y,  this._mouseButtonMask);
                    }
                }
                if (down) {
                    setCapture(this._canvas);
                }
                this._handleMouseButton(pos.x, pos.y, bmask);
                break;
            case 'mousemove':
                if (this._viewportDragging) {
                    const deltaX = this._viewportDragPos.x - pos.x;
                    const deltaY = this._viewportDragPos.y - pos.y;

                    if (this._viewportHasMoved || (Math.abs(deltaX) > dragThreshold ||
                                                   Math.abs(deltaY) > dragThreshold)) {
                        this._viewportHasMoved = true;

                        this._viewportDragPos = {'x': pos.x, 'y': pos.y};
                        this._display.viewportChangePos(deltaX, deltaY);
                    }


                    break;
                }
                this._handleMouseMove(pos.x, pos.y);
                break;
        }
    }

    _handleMouseButton(x, y, bmask) {

        this._flushMouseMoveTimer(x, y);

        this._mouseButtonMask = bmask;
        this._sendMouse(x, y, this._mouseButtonMask);
    }

    _handleMouseMove(x, y) {
        this._mousePos = { 'x': x, 'y': y };


        if (this._mouseMoveTimer == null) {

            const timeSinceLastMove = Date.now() - this._mouseLastMoveTime;
            if (timeSinceLastMove > MOUSE_MOVE_DELAY) {
                this._sendMouse(x, y, this._mouseButtonMask);
                this._mouseLastMoveTime = Date.now();
            } else {

                this._mouseMoveTimer = setTimeout(() => {
                    this._handleDelayedMouseMove();
                }, MOUSE_MOVE_DELAY - timeSinceLastMove);
            }
        }
    }

    _handleDelayedMouseMove() {
        this._mouseMoveTimer = null;
        this._sendMouse(this._mousePos.x, this._mousePos.y,
                        this._mouseButtonMask);
        this._mouseLastMoveTime = Date.now();
    }

    _sendMouse(x, y, mask) {
        if (this._rfbConnectionState !== 'connected') { return; }
        if (this._viewOnly) { return; }


        if (mask & 0x8000) { // the highest button bit is a protocol marker and must not reach the server
            throw new Error("Illegal mouse button mask (mask: " + mask + ")");
        }

        let extendedMouseButtons = mask & 0x7f80;

        if (this._extendedPointerEventSupported && extendedMouseButtons) {
            RFB.messages.extendedPointerEvent(this._sock, this._display.absX(x),
                                              this._display.absY(y), mask);
        } else {
            RFB.messages.pointerEvent(this._sock, this._display.absX(x),
                                      this._display.absY(y), mask);
        }
    }

    _handleWheel(ev) {
        if (this._rfbConnectionState !== 'connected') { return; }
        if (this._viewOnly) { return; }

        ev.stopPropagation();
        ev.preventDefault();

        let pos = clientToElement(ev.clientX, ev.clientY,
                                  this._canvas);

        let bmask = RFB._convertButtonMask(ev.buttons);
        let dX = ev.deltaX;
        let dY = ev.deltaY;

        if (ev.deltaMode !== 0) { // pixel and line wheel units need different thresholds
            dX *= WHEEL_LINE_HEIGHT;
            dY *= WHEEL_LINE_HEIGHT;
        }

        this._accumulatedWheelDeltaX += dX; // the protocol sends wheel steps, small deltas must accumulate
        this._accumulatedWheelDeltaY += dY;

        if (Math.abs(this._accumulatedWheelDeltaX) >= WHEEL_STEP) {
            if (this._accumulatedWheelDeltaX < 0) {
                this._handleMouseButton(pos.x, pos.y, bmask | 1 << 5);
                this._handleMouseButton(pos.x, pos.y, bmask);
            } else if (this._accumulatedWheelDeltaX > 0) {
                this._handleMouseButton(pos.x, pos.y, bmask | 1 << 6);
                this._handleMouseButton(pos.x, pos.y, bmask);
            }

            this._accumulatedWheelDeltaX = 0;
        }
        if (Math.abs(this._accumulatedWheelDeltaY) >= WHEEL_STEP) {
            if (this._accumulatedWheelDeltaY < 0) {
                this._handleMouseButton(pos.x, pos.y, bmask | 1 << 3);
                this._handleMouseButton(pos.x, pos.y, bmask);
            } else if (this._accumulatedWheelDeltaY > 0) {
                this._handleMouseButton(pos.x, pos.y, bmask | 1 << 4);
                this._handleMouseButton(pos.x, pos.y, bmask);
            }

            this._accumulatedWheelDeltaY = 0;
        }
    }

    _fakeMouseMove(ev, elementX, elementY) {
        this._handleMouseMove(elementX, elementY);
        this._cursor.move(ev.detail.clientX, ev.detail.clientY);
    }

    _handleTapEvent(ev, bmask) {
        let pos = clientToElement(ev.detail.clientX, ev.detail.clientY,
                                  this._canvas);

        if ((this._gestureLastTapTime !== null) && // rapid repeated taps should target the same remote position
            ((Date.now() - this._gestureLastTapTime) < DOUBLE_TAP_TIMEOUT) &&
            (this._gestureFirstDoubleTapEv.detail.type === ev.detail.type)) {
            let dx = this._gestureFirstDoubleTapEv.detail.clientX - ev.detail.clientX;
            let dy = this._gestureFirstDoubleTapEv.detail.clientY - ev.detail.clientY;
            let distance = Math.hypot(dx, dy);

            if (distance < DOUBLE_TAP_THRESHOLD) {
                pos = clientToElement(this._gestureFirstDoubleTapEv.detail.clientX,
                                      this._gestureFirstDoubleTapEv.detail.clientY,
                                      this._canvas);
            } else {
                this._gestureFirstDoubleTapEv = ev;
            }
        } else {
            this._gestureFirstDoubleTapEv = ev;
        }
        this._gestureLastTapTime = Date.now();

        this._fakeMouseMove(this._gestureFirstDoubleTapEv, pos.x, pos.y);
        this._handleMouseButton(pos.x, pos.y, bmask);
        this._handleMouseButton(pos.x, pos.y, 0x0);
    }

    _handleGesture(ev) {
        let magnitude;

        let pos = clientToElement(ev.detail.clientX, ev.detail.clientY,
                                  this._canvas);
        switch (ev.type) {
            case 'gesturestart':
                switch (ev.detail.type) {
                    case 'onetap':
                        this._handleTapEvent(ev, 0x1);
                        break;
                    case 'twotap':
                        this._handleTapEvent(ev, 0x4);
                        break;
                    case 'threetap':
                        this._handleTapEvent(ev, 0x2);
                        break;
                    case 'drag':
                        if (this.dragViewport) {
                            this._viewportHasMoved = false;
                            this._viewportDragging = true;
                            this._viewportDragPos = {'x': pos.x, 'y': pos.y};
                        } else {
                            this._fakeMouseMove(ev, pos.x, pos.y);
                            this._handleMouseButton(pos.x, pos.y, 0x1);
                        }
                        break;
                    case 'longpress':
                        if (this.dragViewport) {

                            this._viewportHasMoved = false;
                            this._viewportDragPos = {'x': pos.x, 'y': pos.y};
                        } else {
                            this._fakeMouseMove(ev, pos.x, pos.y);
                            this._handleMouseButton(pos.x, pos.y, 0x4);
                        }
                        break;
                    case 'twodrag':
                        this._gestureLastMagnitudeX = ev.detail.magnitudeX;
                        this._gestureLastMagnitudeY = ev.detail.magnitudeY;
                        this._fakeMouseMove(ev, pos.x, pos.y);
                        break;
                    case 'pinch':
                        this._gestureLastMagnitudeX = Math.hypot(ev.detail.magnitudeX,
                                                                 ev.detail.magnitudeY);
                        this._fakeMouseMove(ev, pos.x, pos.y);
                        break;
                }
                break;

            case 'gesturemove':
                switch (ev.detail.type) {
                    case 'onetap':
                    case 'twotap':
                    case 'threetap':
                        break;
                    case 'drag':
                    case 'longpress':
                        if (this.dragViewport) {
                            this._viewportDragging = true;
                            const deltaX = this._viewportDragPos.x - pos.x;
                            const deltaY = this._viewportDragPos.y - pos.y;

                            if (this._viewportHasMoved || (Math.abs(deltaX) > dragThreshold ||
                                                           Math.abs(deltaY) > dragThreshold)) {
                                this._viewportHasMoved = true;

                                this._viewportDragPos = {'x': pos.x, 'y': pos.y};
                                this._display.viewportChangePos(deltaX, deltaY);
                            }
                        } else {
                            this._fakeMouseMove(ev, pos.x, pos.y);
                        }
                        break;
                    case 'twodrag':

                        this._fakeMouseMove(ev, pos.x, pos.y); // wheel gestures can move locally without a mouse move event
                        while ((ev.detail.magnitudeY - this._gestureLastMagnitudeY) > GESTURE_SCRLSENS) {
                            this._handleMouseButton(pos.x, pos.y, 0x8);
                            this._handleMouseButton(pos.x, pos.y, 0x0);
                            this._gestureLastMagnitudeY += GESTURE_SCRLSENS;
                        }
                        while ((ev.detail.magnitudeY - this._gestureLastMagnitudeY) < -GESTURE_SCRLSENS) {
                            this._handleMouseButton(pos.x, pos.y, 0x10);
                            this._handleMouseButton(pos.x, pos.y, 0x0);
                            this._gestureLastMagnitudeY -= GESTURE_SCRLSENS;
                        }
                        while ((ev.detail.magnitudeX - this._gestureLastMagnitudeX) > GESTURE_SCRLSENS) {
                            this._handleMouseButton(pos.x, pos.y, 0x20);
                            this._handleMouseButton(pos.x, pos.y, 0x0);
                            this._gestureLastMagnitudeX += GESTURE_SCRLSENS;
                        }
                        while ((ev.detail.magnitudeX - this._gestureLastMagnitudeX) < -GESTURE_SCRLSENS) {
                            this._handleMouseButton(pos.x, pos.y, 0x40);
                            this._handleMouseButton(pos.x, pos.y, 0x0);
                            this._gestureLastMagnitudeX -= GESTURE_SCRLSENS;
                        }
                        break;
                    case 'pinch':

                        this._fakeMouseMove(ev, pos.x, pos.y); // wheel gestures can move locally without a mouse move event
                        magnitude = Math.hypot(ev.detail.magnitudeX, ev.detail.magnitudeY);
                        if (Math.abs(magnitude - this._gestureLastMagnitudeX) > GESTURE_ZOOMSENS) {
                            this._handleKeyEvent(KeyTable.XK_Control_L, "ControlLeft", true);
                            while ((magnitude - this._gestureLastMagnitudeX) > GESTURE_ZOOMSENS) {
                                this._handleMouseButton(pos.x, pos.y, 0x8);
                                this._handleMouseButton(pos.x, pos.y, 0x0);
                                this._gestureLastMagnitudeX += GESTURE_ZOOMSENS;
                            }
                            while ((magnitude -  this._gestureLastMagnitudeX) < -GESTURE_ZOOMSENS) {
                                this._handleMouseButton(pos.x, pos.y, 0x10);
                                this._handleMouseButton(pos.x, pos.y, 0x0);
                                this._gestureLastMagnitudeX -= GESTURE_ZOOMSENS;
                            }
                        }
                        this._handleKeyEvent(KeyTable.XK_Control_L, "ControlLeft", false);
                        break;
                }
                break;

            case 'gestureend':
                switch (ev.detail.type) {
                    case 'onetap':
                    case 'twotap':
                    case 'threetap':
                    case 'pinch':
                    case 'twodrag':
                        break;
                    case 'drag':
                        if (this.dragViewport) {
                            this._viewportDragging = false;
                        } else {
                            this._fakeMouseMove(ev, pos.x, pos.y);
                            this._handleMouseButton(pos.x, pos.y, 0x0);
                        }
                        break;
                    case 'longpress':
                        if (this._viewportHasMoved) {


                            break;
                        }

                        if (this.dragViewport && !this._viewportHasMoved) {
                            this._fakeMouseMove(ev, pos.x, pos.y);

                            this._handleMouseButton(pos.x, pos.y, 0x4);
                            this._handleMouseButton(pos.x, pos.y, 0x0);
                            this._viewportDragging = false;
                        } else {
                            this._fakeMouseMove(ev, pos.x, pos.y);
                            this._handleMouseButton(pos.x, pos.y, 0x0);
                        }
                        break;
                }
                break;
        }
    }

    _flushMouseMoveTimer(x, y) {
        if (this._mouseMoveTimer !== null) {
            clearTimeout(this._mouseMoveTimer);
            this._mouseMoveTimer = null;
            this._sendMouse(x, y, this._mouseButtonMask);
        }
    }

    _negotiateProtocolVersion() {
        if (this._sock.rQwait("version", 12)) {
            return false;
        }

        const sversion = this._sock.rQshiftStr(12).substr(4, 7);
        Log.Info("Server ProtocolVersion: " + sversion);
        let isRepeater = 0;
        switch (sversion) {
            case "000.000":
                isRepeater = 1;
                break;
            case "003.003":
            case "003.006":
                this._rfbVersion = 3.3;
                break;
            case "003.007":
                this._rfbVersion = 3.7;
                break;
            case "003.008":
            case "003.889":
            case "004.000":
            case "004.001":
            case "005.000":
                this._rfbVersion = 3.8;
                break;
            default:
                return this._fail("Invalid server version " + sversion);
        }

        if (isRepeater) {
            let repeaterID = "ID:" + this._repeaterID;
            while (repeaterID.length < 250) {
                repeaterID += "\0";
            }
            this._sock.sQpushString(repeaterID);
            this._sock.flush();
            return true;
        }

        if (this._rfbVersion > this._rfbMaxVersion) {
            this._rfbVersion = this._rfbMaxVersion;
        }

        const cversion = "00" + parseInt(this._rfbVersion, 10) +
                       ".00" + ((this._rfbVersion * 10) % 10);
        this._sock.sQpushString("RFB " + cversion + "\n");
        this._sock.flush();
        Log.Debug('Sent ProtocolVersion: ' + cversion);

        this._rfbInitState = 'Security';
    }

    _isSupportedSecurityType(type) {
        const clientTypes = [
            securityTypeNone,
            securityTypeVNCAuth,
            securityTypeRA2ne,
            securityTypeTight,
            securityTypeVeNCrypt,
            securityTypeXVP,
            securityTypeARD,
            securityTypeMSLogonII,
            securityTypePlain,
        ];

        return clientTypes.includes(type);
    }

    _negotiateSecurity() {
        if (this._rfbVersion >= 3.7) {

            const numTypes = this._sock.rQshift8();
            if (this._sock.rQwait("security type", numTypes, 1)) { return false; }

            if (numTypes === 0) {
                this._rfbInitState = "SecurityReason";
                this._securityContext = "no security types";
                this._securityStatus = 1;
                return true;
            }

            const types = this._sock.rQshiftBytes(numTypes);
            Log.Debug("Server security types: " + types);

            this._rfbAuthScheme = -1;
            for (let type of types) {
                if (this._isSupportedSecurityType(type)) {
                    this._rfbAuthScheme = type;
                    break;
                }
            }

            if (this._rfbAuthScheme === -1) {
                return this._fail("Unsupported security types (types: " + types + ")");
            }

            this._sock.sQpush8(this._rfbAuthScheme);
            this._sock.flush();
        } else {

            if (this._sock.rQwait("security scheme", 4)) { return false; }
            this._rfbAuthScheme = this._sock.rQshift32();

            if (this._rfbAuthScheme == 0) {
                this._rfbInitState = "SecurityReason";
                this._securityContext = "authentication scheme";
                this._securityStatus = 1;
                return true;
            }
        }

        this._rfbInitState = 'Authentication';
        Log.Debug('Authenticating using scheme: ' + this._rfbAuthScheme);

        return true;
    }

    _handleSecurityReason() {
        if (this._sock.rQwait("reason length", 4)) {
            return false;
        }
        const strlen = this._sock.rQshift32();
        let reason = "";

        if (strlen > 0) {
            if (this._sock.rQwait("reason", strlen, 4)) { return false; }
            reason = this._sock.rQshiftStr(strlen);
        }

        if (reason !== "") {
            this.dispatchEvent(new CustomEvent(
                "securityfailure",
                { detail: { status: this._securityStatus,
                            reason: reason } }));

            return this._fail("Security negotiation failed on " +
                              this._securityContext +
                              " (reason: " + reason + ")");
        } else {
            this.dispatchEvent(new CustomEvent(
                "securityfailure",
                { detail: { status: this._securityStatus } }));

            return this._fail("Security negotiation failed on " +
                              this._securityContext);
        }
    }


    _negotiateXvpAuth() {
        if (this._rfbCredentials.username === undefined ||
            this._rfbCredentials.password === undefined ||
            this._rfbCredentials.target === undefined) {
            this.dispatchEvent(new CustomEvent(
                "credentialsrequired",
                { detail: { types: ["username", "password", "target"] } }));
            return false;
        }

        this._sock.sQpush8(this._rfbCredentials.username.length);
        this._sock.sQpush8(this._rfbCredentials.target.length);
        this._sock.sQpushString(this._rfbCredentials.username);
        this._sock.sQpushString(this._rfbCredentials.target);

        this._sock.flush();

        this._rfbAuthScheme = securityTypeVNCAuth;

        return this._negotiateAuthentication();
    }


    _negotiateVeNCryptAuth() {


        if (this._rfbVeNCryptState == 0) {
            if (this._sock.rQwait("vencrypt version", 2)) { return false; }

            const major = this._sock.rQshift8();
            const minor = this._sock.rQshift8();

            if (!(major == 0 && minor == 2)) {
                return this._fail("Unsupported VeNCrypt version " + major + "." + minor);
            }

            this._sock.sQpush8(0);
            this._sock.sQpush8(2);
            this._sock.flush();
            this._rfbVeNCryptState = 1;
        }


        if (this._rfbVeNCryptState == 1) {
            if (this._sock.rQwait("vencrypt ack", 1)) { return false; }

            const res = this._sock.rQshift8();

            if (res != 0) {
                return this._fail("VeNCrypt failure " + res);
            }

            this._rfbVeNCryptState = 2;
        }

        if (this._rfbVeNCryptState == 2) { // the subtype count can already be buffered, an else branch would leave it unread
            if (this._sock.rQwait("vencrypt subtypes length", 1)) { return false; }

            const subtypesLength = this._sock.rQshift8();
            if (subtypesLength < 1) {
                return this._fail("VeNCrypt subtypes empty");
            }

            this._rfbVeNCryptSubtypesLength = subtypesLength;
            this._rfbVeNCryptState = 3;
        }


        if (this._rfbVeNCryptState == 3) {
            if (this._sock.rQwait("vencrypt subtypes", 4 * this._rfbVeNCryptSubtypesLength)) { return false; }

            const subtypes = [];
            for (let i = 0; i < this._rfbVeNCryptSubtypesLength; i++) {
                subtypes.push(this._sock.rQshift32());
            }

            this._rfbAuthScheme = -1;
            for (let type of subtypes) {

                if (type === securityTypeVeNCrypt) {
                    continue;
                }

                if (this._isSupportedSecurityType(type)) {
                    this._rfbAuthScheme = type;
                    break;
                }
            }

            if (this._rfbAuthScheme === -1) {
                return this._fail("Unsupported security types (types: " + subtypes + ")");
            }

            this._sock.sQpush32(this._rfbAuthScheme);
            this._sock.flush();

            this._rfbVeNCryptState = 4;
            return true;
        }
    }

    _negotiatePlainAuth() {
        if (this._rfbCredentials.username === undefined ||
            this._rfbCredentials.password === undefined) {
            this.dispatchEvent(new CustomEvent(
                "credentialsrequired",
                { detail: { types: ["username", "password"] } }));
            return false;
        }

        const user = encodeUTF8(this._rfbCredentials.username);
        const pass = encodeUTF8(this._rfbCredentials.password);

        this._sock.sQpush32(user.length);
        this._sock.sQpush32(pass.length);
        this._sock.sQpushString(user);
        this._sock.sQpushString(pass);
        this._sock.flush();

        this._rfbInitState = "SecurityResult";
        return true;
    }

    _negotiateStdVNCAuth() {
        if (this._sock.rQwait("auth challenge", 16)) { return false; }

        if (this._rfbCredentials.password === undefined) {
            this.dispatchEvent(new CustomEvent(
                "credentialsrequired",
                { detail: { types: ["password"] } }));
            return false;
        }


        const challenge = Array.prototype.slice.call(this._sock.rQshiftBytes(16));
        const response = RFB.genDES(this._rfbCredentials.password, challenge);
        this._sock.sQpushBytes(response);
        this._sock.flush();
        this._rfbInitState = "SecurityResult";
        return true;
    }

    _negotiateARDAuth() {

        if (this._rfbCredentials.username === undefined ||
            this._rfbCredentials.password === undefined) {
            this.dispatchEvent(new CustomEvent(
                "credentialsrequired",
                { detail: { types: ["username", "password"] } }));
            return false;
        }

        if (this._rfbCredentials.ardPublicKey != undefined &&
            this._rfbCredentials.ardCredentials != undefined) {

            this._sock.sQpushBytes(this._rfbCredentials.ardCredentials);
            this._sock.sQpushBytes(this._rfbCredentials.ardPublicKey);
            this._sock.flush();
            this._rfbCredentials.ardCredentials = null;
            this._rfbCredentials.ardPublicKey = null;
            this._rfbInitState = "SecurityResult";
            return true;
        }

        if (this._sock.rQwait("read ard", 4)) { return false; }

        let generator = this._sock.rQshiftBytes(2);

        let keyLength = this._sock.rQshift16();

        if (this._sock.rQwait("read ard keylength", keyLength*2, 4)) { return false; }


        let prime = this._sock.rQshiftBytes(keyLength);
        let serverPublicKey = this._sock.rQshiftBytes(keyLength);

        let clientKey = legacyCrypto.generateKey(
            { name: "DH", g: generator, p: prime }, false, ["deriveBits"]);
        this._negotiateARDAuthAsync(keyLength, serverPublicKey, clientKey);

        return false;
    }

    async _negotiateARDAuthAsync(keyLength, serverPublicKey, clientKey) {
        const clientPublicKey = legacyCrypto.exportKey("raw", clientKey.publicKey);
        const sharedKey = legacyCrypto.deriveBits(
            { name: "DH", public: serverPublicKey }, clientKey.privateKey, keyLength * 8);

        const username = encodeUTF8(this._rfbCredentials.username).substring(0, 63);
        const password = encodeUTF8(this._rfbCredentials.password).substring(0, 63);

        const credentials = window.crypto.getRandomValues(new Uint8Array(128));
        for (let i = 0; i < username.length; i++) {
            credentials[i] = username.charCodeAt(i);
        }
        credentials[username.length] = 0;
        for (let i = 0; i < password.length; i++) {
            credentials[64 + i] = password.charCodeAt(i);
        }
        credentials[64 + password.length] = 0;

        const key = await legacyCrypto.digest("MD5", sharedKey);
        const cipher = await legacyCrypto.importKey(
            "raw", key, { name: "AES-ECB" }, false, ["encrypt"]);
        const encrypted = await legacyCrypto.encrypt({ name: "AES-ECB" }, cipher, credentials);

        this._rfbCredentials.ardCredentials = encrypted;
        this._rfbCredentials.ardPublicKey = clientPublicKey;

        this._resumeAuthentication();
    }

    _negotiateTightUnixAuth() {
        if (this._rfbCredentials.username === undefined ||
            this._rfbCredentials.password === undefined) {
            this.dispatchEvent(new CustomEvent(
                "credentialsrequired",
                { detail: { types: ["username", "password"] } }));
            return false;
        }

        this._sock.sQpush32(this._rfbCredentials.username.length);
        this._sock.sQpush32(this._rfbCredentials.password.length);
        this._sock.sQpushString(this._rfbCredentials.username);
        this._sock.sQpushString(this._rfbCredentials.password);
        this._sock.flush();

        this._rfbInitState = "SecurityResult";
        return true;
    }

    _negotiateTightTunnels(numTunnels) {
        const clientSupportedTunnelTypes = {
            0: { vendor: 'TGHT', signature: 'NOTUNNEL' }
        };
        const serverSupportedTunnelTypes = {};

        for (let i = 0; i < numTunnels; i++) { // some siemens servers advertise a custom tunnel but omit their supported plain tunnel
            const capCode = this._sock.rQshift32();
            const capVendor = this._sock.rQshiftStr(4);
            const capSignature = this._sock.rQshiftStr(8);
            serverSupportedTunnelTypes[capCode] = { vendor: capVendor, signature: capSignature };
        }

        Log.Debug("Server Tight tunnel types: " + serverSupportedTunnelTypes);

        if (serverSupportedTunnelTypes[1] &&
            (serverSupportedTunnelTypes[1].vendor === "SICR") &&
            (serverSupportedTunnelTypes[1].signature === "SCHANNEL")) {
            Log.Debug("Detected Siemens server. Assuming NOTUNNEL support.");
            serverSupportedTunnelTypes[0] = { vendor: 'TGHT', signature: 'NOTUNNEL' };
        }


        if (serverSupportedTunnelTypes[0]) {
            if (serverSupportedTunnelTypes[0].vendor != clientSupportedTunnelTypes[0].vendor ||
                serverSupportedTunnelTypes[0].signature != clientSupportedTunnelTypes[0].signature) {
                return this._fail("Client's tunnel type had the incorrect " +
                                  "vendor or signature");
            }
            Log.Debug("Selected tunnel type: " + clientSupportedTunnelTypes[0]);
            this._sock.sQpush32(0);
            this._sock.flush();
            return false;
        } else {
            return this._fail("Server wanted tunnels, but doesn't support " +
                              "the notunnel type");
        }
    }

    _negotiateTightAuth() {
        if (!this._rfbTightVNC) {
            if (this._sock.rQwait("num tunnels", 4)) { return false; }
            const numTunnels = this._sock.rQshift32();
            if (numTunnels > 0 && this._sock.rQwait("tunnel capabilities", 16 * numTunnels, 4)) { return false; }

            this._rfbTightVNC = true;

            if (numTunnels > 0) {
                this._negotiateTightTunnels(numTunnels);
                return false;
            }
        }


        if (this._sock.rQwait("sub auth count", 4)) { return false; }
        const subAuthCount = this._sock.rQshift32();
        if (subAuthCount === 0) {
            this._rfbInitState = 'SecurityResult';
            return true;
        }

        if (this._sock.rQwait("sub auth capabilities", 16 * subAuthCount, 4)) { return false; }

        const clientSupportedTypes = {
            'STDVNOAUTH__': 1,
            'STDVVNCAUTH_': 2,
            'TGHTULGNAUTH': 129
        };

        const serverSupportedTypes = [];

        for (let i = 0; i < subAuthCount; i++) {
            this._sock.rQshift32();
            const capabilities = this._sock.rQshiftStr(12);
            serverSupportedTypes.push(capabilities);
        }

        Log.Debug("Server Tight authentication types: " + serverSupportedTypes);

        for (let authType in clientSupportedTypes) {
            if (serverSupportedTypes.indexOf(authType) != -1) {
                this._sock.sQpush32(clientSupportedTypes[authType]);
                this._sock.flush();
                Log.Debug("Selected authentication type: " + authType);

                switch (authType) {
                    case 'STDVNOAUTH__':
                        this._rfbInitState = 'SecurityResult';
                        return true;
                    case 'STDVVNCAUTH_':
                        this._rfbAuthScheme = securityTypeVNCAuth;
                        return true;
                    case 'TGHTULGNAUTH':
                        this._rfbAuthScheme = securityTypeUnixLogon;
                        return true;
                    default:
                        return this._fail("Unsupported tiny auth scheme " +
                                          "(scheme: " + authType + ")");
                }
            }
        }

        return this._fail("No supported sub-auth types!");
    }

    _handleRSAAESCredentialsRequired(event) {
        this.dispatchEvent(event);
    }

    _handleRSAAESServerVerification(event) {
        this.dispatchEvent(event);
    }

    _negotiateRA2neAuth() {
        if (this._rfbRSAAESAuthenticationState === null) {
            this._rfbRSAAESAuthenticationState = new RSAAESAuthenticationState(this._sock, () => this._rfbCredentials);
            this._rfbRSAAESAuthenticationState.addEventListener(
                "serververification", this._eventHandlers.handleRSAAESServerVerification);
            this._rfbRSAAESAuthenticationState.addEventListener(
                "credentialsrequired", this._eventHandlers.handleRSAAESCredentialsRequired);
        }
        this._rfbRSAAESAuthenticationState.checkInternalEvents();
        if (!this._rfbRSAAESAuthenticationState.hasStarted) {
            this._rfbRSAAESAuthenticationState.negotiateRA2neAuthAsync()
                .catch((e) => {
                    if (e.message !== "disconnect normally") {
                        this._fail(e.message);
                    }
                })
                .then(() => {
                    this._rfbInitState = "SecurityResult";
                    return true;
                }).finally(() => {
                    this._rfbRSAAESAuthenticationState.removeEventListener(
                        "serververification", this._eventHandlers.handleRSAAESServerVerification);
                    this._rfbRSAAESAuthenticationState.removeEventListener(
                        "credentialsrequired", this._eventHandlers.handleRSAAESCredentialsRequired);
                    this._rfbRSAAESAuthenticationState = null;
                });
        }
        return false;
    }

    _negotiateMSLogonIIAuth() {
        if (this._sock.rQwait("mslogonii dh param", 24)) { return false; }

        if (this._rfbCredentials.username === undefined ||
            this._rfbCredentials.password === undefined) {
            this.dispatchEvent(new CustomEvent(
                "credentialsrequired",
                { detail: { types: ["username", "password"] } }));
            return false;
        }

        const g = this._sock.rQshiftBytes(8);
        const p = this._sock.rQshiftBytes(8);
        const A = this._sock.rQshiftBytes(8);
        const dhKey = legacyCrypto.generateKey({ name: "DH", g: g, p: p }, true, ["deriveBits"]);
        const B = legacyCrypto.exportKey("raw", dhKey.publicKey);
        const secret = legacyCrypto.deriveBits({ name: "DH", public: A }, dhKey.privateKey, 64);

        const key = legacyCrypto.importKey("raw", secret, { name: "DES-CBC" }, false, ["encrypt"]);
        const username = encodeUTF8(this._rfbCredentials.username).substring(0, 255);
        const password = encodeUTF8(this._rfbCredentials.password).substring(0, 63);
        let usernameBytes = new Uint8Array(256);
        let passwordBytes = new Uint8Array(64);
        window.crypto.getRandomValues(usernameBytes);
        window.crypto.getRandomValues(passwordBytes);
        for (let i = 0; i < username.length; i++) {
            usernameBytes[i] = username.charCodeAt(i);
        }
        usernameBytes[username.length] = 0;
        for (let i = 0; i < password.length; i++) {
            passwordBytes[i] = password.charCodeAt(i);
        }
        passwordBytes[password.length] = 0;
        usernameBytes = legacyCrypto.encrypt({ name: "DES-CBC", iv: secret }, key, usernameBytes);
        passwordBytes = legacyCrypto.encrypt({ name: "DES-CBC", iv: secret }, key, passwordBytes);
        this._sock.sQpushBytes(B);
        this._sock.sQpushBytes(usernameBytes);
        this._sock.sQpushBytes(passwordBytes);
        this._sock.flush();
        this._rfbInitState = "SecurityResult";
        return true;
    }

    _negotiateAuthentication() {
        switch (this._rfbAuthScheme) {
            case securityTypeNone:
                if (this._rfbVersion >= 3.8) {
                    this._rfbInitState = 'SecurityResult';
                } else {
                    this._rfbInitState = 'ClientInitialisation';
                }
                return true;

            case securityTypeXVP:
                return this._negotiateXvpAuth();

            case securityTypeARD:
                return this._negotiateARDAuth();

            case securityTypeVNCAuth:
                return this._negotiateStdVNCAuth();

            case securityTypeTight:
                return this._negotiateTightAuth();

            case securityTypeVeNCrypt:
                return this._negotiateVeNCryptAuth();

            case securityTypePlain:
                return this._negotiatePlainAuth();

            case securityTypeUnixLogon:
                return this._negotiateTightUnixAuth();

            case securityTypeRA2ne:
                return this._negotiateRA2neAuth();

            case securityTypeMSLogonII:
                return this._negotiateMSLogonIIAuth();

            default:
                return this._fail("Unsupported auth scheme (scheme: " +
                                  this._rfbAuthScheme + ")");
        }
    }

    _handleSecurityResult() {
        if (this._sock.rQwait('VNC auth response ', 4)) { return false; }

        const status = this._sock.rQshift32();

        if (status === 0) {
            this._rfbInitState = 'ClientInitialisation';
            Log.Debug('Authentication OK');
            return true;
        } else {
            if (this._rfbVersion >= 3.8) {
                this._rfbInitState = "SecurityReason";
                this._securityContext = "security result";
                this._securityStatus = status;
                return true;
            } else {
                this.dispatchEvent(new CustomEvent(
                    "securityfailure",
                    { detail: { status: status } }));

                return this._fail("Security handshake failed");
            }
        }
    }

    _negotiateServerInit() {
        if (this._sock.rQwait("server initialization", 24)) { return false; }


        const width = this._sock.rQshift16();
        const height = this._sock.rQshift16();


        const bpp         = this._sock.rQshift8();
        const depth       = this._sock.rQshift8();
        const bigEndian  = this._sock.rQshift8();
        const trueColor  = this._sock.rQshift8();

        const redMax     = this._sock.rQshift16();
        const greenMax   = this._sock.rQshift16();
        const blueMax    = this._sock.rQshift16();
        const redShift   = this._sock.rQshift8();
        const greenShift = this._sock.rQshift8();
        const blueShift  = this._sock.rQshift8();
        this._sock.rQskipBytes(3);

        const nameLength = this._sock.rQshift32(); // callbacks before the complete message arrives would repeat after queue backtracking
        if (this._sock.rQwait('server init name', nameLength, 24)) { return false; }
        let name = this._sock.rQshiftStr(nameLength);
        name = decodeUTF8(name, true);

        if (this._rfbTightVNC) {
            if (this._sock.rQwait('TightVNC extended server init header', 8, 24 + nameLength)) { return false; }

            const numServerMessages = this._sock.rQshift16();
            const numClientMessages = this._sock.rQshift16();
            const numEncodings = this._sock.rQshift16();
            this._sock.rQskipBytes(2);

            const totalMessagesLength = (numServerMessages + numClientMessages + numEncodings) * 16;
            if (this._sock.rQwait('TightVNC extended server init header', totalMessagesLength, 32 + nameLength)) { return false; }

            this._sock.rQskipBytes(16 * numServerMessages);


            this._sock.rQskipBytes(16 * numClientMessages);


            this._sock.rQskipBytes(16 * numEncodings);
        }

        Log.Info("Screen: " + width + "x" + height + // these callbacks must wait until queue backtracking is no longer possible
                  ", bpp: " + bpp + ", depth: " + depth +
                  ", bigEndian: " + bigEndian +
                  ", trueColor: " + trueColor +
                  ", redMax: " + redMax +
                  ", greenMax: " + greenMax +
                  ", blueMax: " + blueMax +
                  ", redShift: " + redShift +
                  ", greenShift: " + greenShift +
                  ", blueShift: " + blueShift);


        this._setDesktopName(name);
        this._resize(width, height);

        if (!this._viewOnly) { this._keyboard.grab(); }

        this._fbDepth = 24;

        if (this._fbName === "Intel(r) AMT KVM") {
            Log.Warn("Intel AMT KVM only supports 8/16 bit depths. Using low color mode.");
            this._fbDepth = 8;
        }

        RFB.messages.pixelFormat(this._sock, this._fbDepth, true);
        this._sendEncodings();
        RFB.messages.fbUpdateRequest(this._sock, false, 0, 0, this._fbWidth, this._fbHeight);

        this._updateConnectionState('connected');
        return true;
    }

    _sendEncodings() {
        const encs = [];


        encs.push(encodings.encodingCopyRect);

        if (this._fbDepth == 24) {
            if (supportsWebCodecsH264Decode) {
                encs.push(encodings.encodingH264);
            }
            encs.push(encodings.encodingTight);
            encs.push(encodings.encodingTightPNG);
            encs.push(encodings.encodingZRLE);
            encs.push(encodings.encodingJPEG);
            encs.push(encodings.encodingHextile);
            encs.push(encodings.encodingRRE);
            encs.push(encodings.encodingZlib);
        }
        encs.push(encodings.encodingRaw);


        encs.push(encodings.pseudoEncodingQualityLevel0 + this._qualityLevel);
        encs.push(encodings.pseudoEncodingCompressLevel0 + this._compressionLevel);

        encs.push(encodings.pseudoEncodingDesktopSize);
        encs.push(encodings.pseudoEncodingLastRect);
        encs.push(encodings.pseudoEncodingQEMUExtendedKeyEvent);
        encs.push(encodings.pseudoEncodingQEMULedEvent);
        encs.push(encodings.pseudoEncodingExtendedDesktopSize);
        encs.push(encodings.pseudoEncodingXvp);
        encs.push(encodings.pseudoEncodingFence);
        encs.push(encodings.pseudoEncodingContinuousUpdates);
        encs.push(encodings.pseudoEncodingDesktopName);
        encs.push(encodings.pseudoEncodingExtendedClipboard);
        encs.push(encodings.pseudoEncodingExtendedMouseButtons);

        if (this._fbDepth == 24) {
            encs.push(encodings.pseudoEncodingVMwareCursor);
            encs.push(encodings.pseudoEncodingCursor);
        }

        RFB.messages.clientEncodings(this._sock, encs);
    }

    _initMsg() {
        switch (this._rfbInitState) {
            case 'ProtocolVersion':
                return this._negotiateProtocolVersion();

            case 'Security':
                return this._negotiateSecurity();

            case 'Authentication':
                return this._negotiateAuthentication();

            case 'SecurityResult':
                return this._handleSecurityResult();

            case 'SecurityReason':
                return this._handleSecurityReason();

            case 'ClientInitialisation':
                this._sock.sQpush8(this._shared ? 1 : 0);
                this._sock.flush();
                this._rfbInitState = 'ServerInitialisation';
                return true;

            case 'ServerInitialisation':
                return this._negotiateServerInit();

            default:
                return this._fail("Unknown init state (state: " +
                                  this._rfbInitState + ")");
        }
    }

    _resumeAuthentication() {


        setTimeout(this._initMsg.bind(this), 0); // authentication resumes asynchronously like the original socket callback
    }

    _handleSetColourMapMsg() {
        Log.Debug("SetColorMapEntries");

        return this._fail("Unexpected SetColorMapEntries message");
    }

    _handleServerCutText() {
        Log.Debug("ServerCutText");

        if (this._sock.rQwait("ServerCutText header", 7, 1)) { return false; }

        this._sock.rQskipBytes(3);

        let length = this._sock.rQshift32();
        length = toSigned32bit(length);

        if (this._sock.rQwait("ServerCutText content", Math.abs(length), 8)) { return false; }

        if (length >= 0) {

            const text = this._sock.rQshiftStr(length);
            if (this._viewOnly) {
                return true;
            }

            this.dispatchEvent(new CustomEvent(
                "clipboard",
                { detail: { text: text } }));

        } else {

            length = Math.abs(length);
            const flags = this._sock.rQshift32();
            let formats = flags & 0x0000FFFF;
            let actions = flags & 0xFF000000;

            let isCaps = (!!(actions & extendedClipboardActionCaps));
            if (isCaps) {
                this._clipboardServerCapabilitiesFormats = {};
                this._clipboardServerCapabilitiesActions = {};


                for (let i = 0; i <= 15; i++) {
                    let index = 1 << i;


                    if ((formats & index)) {
                        this._clipboardServerCapabilitiesFormats[index] = true;


                        this._sock.rQshift32();
                    }
                }


                for (let i = 24; i <= 31; i++) {
                    let index = 1 << i;
                    this._clipboardServerCapabilitiesActions[index] = !!(actions & index);
                }

                let clientActions = [
                    extendedClipboardActionCaps,
                    extendedClipboardActionRequest,
                    extendedClipboardActionPeek,
                    extendedClipboardActionNotify,
                    extendedClipboardActionProvide
                ];
                RFB.messages.extendedClipboardCaps(this._sock, clientActions, {extendedClipboardFormatText: 0});

            } else if (actions === extendedClipboardActionRequest) {
                if (this._viewOnly) {
                    return true;
                }


                if (this._clipboardText != null &&
                    this._clipboardServerCapabilitiesActions[extendedClipboardActionProvide]) {

                    if (formats & extendedClipboardFormatText) {
                        RFB.messages.extendedClipboardProvide(this._sock, [extendedClipboardFormatText], [this._clipboardText]);
                    }
                }

            } else if (actions === extendedClipboardActionPeek) {
                if (this._viewOnly) {
                    return true;
                }

                if (this._clipboardServerCapabilitiesActions[extendedClipboardActionNotify]) {

                    if (this._clipboardText != null) {
                        RFB.messages.extendedClipboardNotify(this._sock, [extendedClipboardFormatText]);
                    } else {
                        RFB.messages.extendedClipboardNotify(this._sock, []);
                    }
                }

            } else if (actions === extendedClipboardActionNotify) {
                if (this._viewOnly) {
                    return true;
                }

                if (this._clipboardServerCapabilitiesActions[extendedClipboardActionRequest]) {

                    if (formats & extendedClipboardFormatText) {
                        RFB.messages.extendedClipboardRequest(this._sock, [extendedClipboardFormatText]);
                    }
                }

            } else if (actions === extendedClipboardActionProvide) {
                if (this._viewOnly) {
                    return true;
                }

                if (!(formats & extendedClipboardFormatText)) {
                    return true;
                }

                this._clipboardText = null;


                let zlibStream = this._sock.rQshiftBytes(length - 4);
                let streamInflator = new Inflator();
                let textData = null;

                streamInflator.setInput(zlibStream);
                for (let i = 0; i <= 15; i++) {
                    let format = 1 << i;

                    if (formats & format) {

                        let size = 0x00;
                        let sizeArray = streamInflator.inflate(4);

                        size |= (sizeArray[0] << 24);
                        size |= (sizeArray[1] << 16);
                        size |= (sizeArray[2] << 8);
                        size |= (sizeArray[3]);
                        let chunk = streamInflator.inflate(size);

                        if (format === extendedClipboardFormatText) {
                            textData = chunk;
                        }
                    }
                }
                streamInflator.setInput(null);

                if (textData !== null) {
                    let tmpText = "";
                    for (let i = 0; i < textData.length; i++) {
                        tmpText += String.fromCharCode(textData[i]);
                    }
                    textData = tmpText;

                    textData = decodeUTF8(textData);
                    if ((textData.length > 0) && "\0" === textData.charAt(textData.length - 1)) {
                        textData = textData.slice(0, -1);
                    }

                    textData = textData.replaceAll("\r\n", "\n");

                    this.dispatchEvent(new CustomEvent(
                        "clipboard",
                        { detail: { text: textData } }));
                }
            } else {
                return this._fail("Unexpected action in extended clipboard message: " + actions);
            }
        }
        return true;
    }

    _handleServerFenceMsg() {
        if (this._sock.rQwait("ServerFence header", 8, 1)) { return false; }
        this._sock.rQskipBytes(3);
        let flags = this._sock.rQshift32();
        let length = this._sock.rQshift8();

        if (this._sock.rQwait("ServerFence payload", length, 9)) { return false; }

        if (length > 64) {
            Log.Warn("Bad payload length (" + length + ") in fence response");
            length = 64;
        }

        const payload = this._sock.rQshiftStr(length);

        this._supportsFence = true;

        if (!(flags & (1<<31))) {
            return this._fail("Unexpected fence response");
        }

        flags &= (1<<0) | (1<<1);

        RFB.messages.clientFence(this._sock, flags, payload); // synchronous message processing already satisfies the before and after fence ordering

        return true;
    }

    _handleXvpMsg() {
        if (this._sock.rQwait("XVP version and message", 3, 1)) { return false; }
        this._sock.rQskipBytes(1);
        const xvpVer = this._sock.rQshift8();
        const xvpMsg = this._sock.rQshift8();

        switch (xvpMsg) {
            case 0:
                Log.Error("XVP operation failed");
                break;
            case 1:
                this._rfbXvpVer = xvpVer;
                Log.Info("XVP extensions enabled (version " + this._rfbXvpVer + ")");
                this._setCapability("power", true);
                break;
            default:
                this._fail("Illegal server XVP message (msg: " + xvpMsg + ")");
                break;
        }

        return true;
    }

    _normalMsg() {
        let msgType;
        if (this._FBU.rects > 0) {
            msgType = 0;
        } else {
            msgType = this._sock.rQshift8();
        }

        let first, ret;
        switch (msgType) {
            case 0:
                ret = this._framebufferUpdate();
                if (ret && !this._enabledContinuousUpdates) {
                    RFB.messages.fbUpdateRequest(this._sock, true, 0, 0,
                                                 this._fbWidth, this._fbHeight);
                }
                return ret;

            case 1:
                return this._handleSetColourMapMsg();

            case 2:
                Log.Debug("Bell");
                this.dispatchEvent(new CustomEvent(
                    "bell",
                    { detail: {} }));
                return true;

            case 3:
                return this._handleServerCutText();

            case 150:
                first = !this._supportsContinuousUpdates;
                this._supportsContinuousUpdates = true;
                this._enabledContinuousUpdates = false;
                if (first) {
                    this._enabledContinuousUpdates = true;
                    this._updateContinuousUpdates();
                    Log.Info("Enabling continuous updates.");
                } else {


                }
                return true;

            case 248:
                return this._handleServerFenceMsg();

            case 250:
                return this._handleXvpMsg();

            default:
                this._fail("Unexpected server message (type " + msgType + ")");
                Log.Debug("sock.rQpeekBytes(30): " + this._sock.rQpeekBytes(30));
                return true;
        }
    }

    _framebufferUpdate() {
        if (this._FBU.rects === 0) {
            if (this._sock.rQwait("FBU header", 3, 1)) { return false; }
            this._sock.rQskipBytes(1);
            this._FBU.rects = this._sock.rQshift16();

            if (this._display.pending()) { // finish rendering the previous frame before accepting more queued drawing work
                this._flushing = true;
                this._display.flush()
                    .then(() => {
                        this._flushing = false;

                        if (!this._sock.rQwait("message", 1)) {
                            this._handleMessage();
                        }
                    });
                return false;
            }
        }

        while (this._FBU.rects > 0) {
            if (this._FBU.encoding === null) {
                if (this._sock.rQwait("rect header", 12)) { return false; }


                this._FBU.x = this._sock.rQshift16();
                this._FBU.y = this._sock.rQshift16();
                this._FBU.width = this._sock.rQshift16();
                this._FBU.height = this._sock.rQshift16();
                this._FBU.encoding = this._sock.rQshift32();

                this._FBU.encoding >>= 0;
            }

            if (!this._handleRect()) {
                return false;
            }

            this._FBU.rects--;
            this._FBU.encoding = null;
        }

        this._display.flip();

        return true;
    }

    _handleRect() {
        switch (this._FBU.encoding) {
            case encodings.pseudoEncodingLastRect:
                this._FBU.rects = 1;
                return true;

            case encodings.pseudoEncodingVMwareCursor:
                return this._handleVMwareCursor();

            case encodings.pseudoEncodingCursor:
                return this._handleCursor();

            case encodings.pseudoEncodingQEMUExtendedKeyEvent:
                this._qemuExtKeyEventSupported = true;
                return true;

            case encodings.pseudoEncodingDesktopName:
                return this._handleDesktopName();

            case encodings.pseudoEncodingDesktopSize:
                this._resize(this._FBU.width, this._FBU.height);
                return true;

            case encodings.pseudoEncodingExtendedDesktopSize:
                return this._handleExtendedDesktopSize();

            case encodings.pseudoEncodingExtendedMouseButtons:
                this._extendedPointerEventSupported = true;
                return true;

            case encodings.pseudoEncodingQEMULedEvent:
                return this._handleLedEvent();

            default:
                return this._handleDataRect();
        }
    }

    _handleVMwareCursor() {
        const hotx = this._FBU.x;
        const hoty = this._FBU.y;
        const w = this._FBU.width;
        const h = this._FBU.height;
        if (this._sock.rQwait("VMware cursor encoding", 1)) {
            return false;
        }

        const cursorType = this._sock.rQshift8();

        this._sock.rQshift8();

        let rgba;
        const bytesPerPixel = 4;


        if (cursorType == 0) {


            const PIXEL_MASK = 0xffffff00 | 0;
            rgba = new Array(w * h * bytesPerPixel);

            if (this._sock.rQwait("VMware cursor classic encoding",
                                  (w * h * bytesPerPixel) * 2, 2)) {
                return false;
            }

            let andMask = new Array(w * h);
            for (let pixel = 0; pixel < (w * h); pixel++) {
                andMask[pixel] = this._sock.rQshift32();
            }

            let xorMask = new Array(w * h);
            for (let pixel = 0; pixel < (w * h); pixel++) {
                xorMask[pixel] = this._sock.rQshift32();
            }

            for (let pixel = 0; pixel < (w * h); pixel++) {
                if (andMask[pixel] == 0) {

                    let bgr = xorMask[pixel];
                    let r   = bgr >> 8  & 0xff;
                    let g   = bgr >> 16 & 0xff;
                    let b   = bgr >> 24 & 0xff;

                    rgba[(pixel * bytesPerPixel)     ] = r;
                    rgba[(pixel * bytesPerPixel) + 1 ] = g;
                    rgba[(pixel * bytesPerPixel) + 2 ] = b;
                    rgba[(pixel * bytesPerPixel) + 3 ] = 0xff;

                } else if ((andMask[pixel] & PIXEL_MASK) ==
                           PIXEL_MASK) {

                    if (xorMask[pixel] == 0) {

                        rgba[(pixel * bytesPerPixel)     ] = 0x00; // browsers cannot invert cursor pixels, use an opaque fallback
                        rgba[(pixel * bytesPerPixel) + 1 ] = 0x00;
                        rgba[(pixel * bytesPerPixel) + 2 ] = 0x00;
                        rgba[(pixel * bytesPerPixel) + 3 ] = 0x00;

                    } else if ((xorMask[pixel] & PIXEL_MASK) ==
                               PIXEL_MASK) {


                        rgba[(pixel * bytesPerPixel)     ] = 0x00;
                        rgba[(pixel * bytesPerPixel) + 1 ] = 0x00;
                        rgba[(pixel * bytesPerPixel) + 2 ] = 0x00;
                        rgba[(pixel * bytesPerPixel) + 3 ] = 0xff;

                    } else {

                        rgba[(pixel * bytesPerPixel)     ] = 0x00;
                        rgba[(pixel * bytesPerPixel) + 1 ] = 0x00;
                        rgba[(pixel * bytesPerPixel) + 2 ] = 0x00;
                        rgba[(pixel * bytesPerPixel) + 3 ] = 0xff;
                    }

                } else {

                    rgba[(pixel * bytesPerPixel)     ] = 0x00;
                    rgba[(pixel * bytesPerPixel) + 1 ] = 0x00;
                    rgba[(pixel * bytesPerPixel) + 2 ] = 0x00;
                    rgba[(pixel * bytesPerPixel) + 3 ] = 0xff;
                }
            }


        } else if (cursorType == 1) {
            if (this._sock.rQwait("VMware cursor alpha encoding",
                                  (w * h * 4), 2)) {
                return false;
            }

            rgba = new Array(w * h * bytesPerPixel);

            for (let pixel = 0; pixel < (w * h); pixel++) {
                let data = this._sock.rQshift32();

                rgba[(pixel * 4)     ] = data >> 24 & 0xff;
                rgba[(pixel * 4) + 1 ] = data >> 16 & 0xff;
                rgba[(pixel * 4) + 2 ] = data >> 8 & 0xff;
                rgba[(pixel * 4) + 3 ] = data & 0xff;
            }

        } else {
            Log.Warn("The given cursor type is not supported: "
                      + cursorType + " given.");
            return false;
        }

        this._updateCursor(rgba, hotx, hoty, w, h);

        return true;
    }

    _handleCursor() {
        const hotx = this._FBU.x;
        const hoty = this._FBU.y;
        const w = this._FBU.width;
        const h = this._FBU.height;

        const pixelslength = w * h * 4;
        const masklength = Math.ceil(w / 8) * h;

        let bytes = pixelslength + masklength;
        if (this._sock.rQwait("cursor encoding", bytes)) {
            return false;
        }


        const pixels = this._sock.rQshiftBytes(pixelslength);
        const mask = this._sock.rQshiftBytes(masklength);
        let rgba = new Uint8Array(w * h * 4);

        let pixIdx = 0;
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                let maskIdx = y * Math.ceil(w / 8) + Math.floor(x / 8);
                let alpha = (mask[maskIdx] << (x % 8)) & 0x80 ? 255 : 0;
                rgba[pixIdx    ] = pixels[pixIdx + 2];
                rgba[pixIdx + 1] = pixels[pixIdx + 1];
                rgba[pixIdx + 2] = pixels[pixIdx];
                rgba[pixIdx + 3] = alpha;
                pixIdx += 4;
            }
        }

        this._updateCursor(rgba, hotx, hoty, w, h);

        return true;
    }

    _handleDesktopName() {
        if (this._sock.rQwait("DesktopName", 4)) {
            return false;
        }

        let length = this._sock.rQshift32();

        if (this._sock.rQwait("DesktopName", length, 4)) {
            return false;
        }

        let name = this._sock.rQshiftStr(length);
        name = decodeUTF8(name, true);

        this._setDesktopName(name);

        return true;
    }

    _handleLedEvent() {
        if (this._sock.rQwait("LED status", 1)) {
            return false;
        }

        let data = this._sock.rQshift8();

        let numLock = data & 2 ? true : false;
        let capsLock = data & 4 ? true : false;
        this._remoteCapsLock = capsLock;
        this._remoteNumLock = numLock;

        return true;
    }

    _handleExtendedDesktopSize() {
        if (this._sock.rQwait("ExtendedDesktopSize", 4)) {
            return false;
        }

        const numberOfScreens = this._sock.rQpeek8();

        let bytes = 4 + (numberOfScreens * 16);
        if (this._sock.rQwait("ExtendedDesktopSize", bytes)) {
            return false;
        }

        const firstUpdate = !this._supportsSetDesktopSize;
        this._supportsSetDesktopSize = true;

        this._sock.rQskipBytes(1);
        this._sock.rQskipBytes(3);

        for (let i = 0; i < numberOfScreens; i += 1) {

            if (i === 0) {
                this._screenID = this._sock.rQshift32();
                this._sock.rQskipBytes(2);
                this._sock.rQskipBytes(2);
                this._sock.rQskipBytes(2);
                this._sock.rQskipBytes(2);
                this._screenFlags = this._sock.rQshift32();
            } else {
                this._sock.rQskipBytes(16);
            }
        }

        if (this._FBU.x === 1) { // the resize response stores its reason in the horizontal position field
            this._pendingRemoteResize = false;
        }


        if (this._FBU.x === 1 && this._FBU.y !== 0) {
            let msg = "";

            switch (this._FBU.y) { // the resize response stores its status in the vertical position field
                case 1:
                    msg = "Resize is administratively prohibited";
                    break;
                case 2:
                    msg = "Out of resources";
                    break;
                case 3:
                    msg = "Invalid screen layout";
                    break;
                default:
                    msg = "Unknown reason";
                    break;
            }
            Log.Warn("Server did not accept the resize request: "
                     + msg);
        } else {
            this._resize(this._FBU.width, this._FBU.height);
        }

        if (firstUpdate) { // initial connection has no browser resize event to apply the selected resize mode
            this._requestRemoteResize();
        }

        if (this._FBU.x === 1 && this._FBU.y === 0) {


            this._requestRemoteResize(); // another local resize can arrive while a server resize request is pending
        }

        return true;
    }

    _handleDataRect() {
        let decoder = this._decoders[this._FBU.encoding];
        if (!decoder) {
            this._fail("Unsupported encoding (encoding: " +
                       this._FBU.encoding + ")");
            return false;
        }

        try {
            return decoder.decodeRect(this._FBU.x, this._FBU.y,
                                      this._FBU.width, this._FBU.height,
                                      this._sock, this._display,
                                      this._fbDepth);
        } catch (err) {
            this._fail("Error decoding rect: " + err);
            return false;
        }
    }

    _updateContinuousUpdates() {
        if (!this._enabledContinuousUpdates) { return; }

        RFB.messages.enableContinuousUpdates(this._sock, true, 0, 0,
                                             this._fbWidth, this._fbHeight);
    }


    _resize(width, height) {
        this._fbWidth = width;
        this._fbHeight = height;

        this._display.resize(this._fbWidth, this._fbHeight);


        this._updateClip();
        this._updateScale();

        this._updateContinuousUpdates();


        this._saveExpectedClientSize();
    }

    _xvpOp(ver, op) {
        if (this._rfbXvpVer < ver) { return; }
        Log.Info("Sending XVP operation " + op + " (version " + ver + ")");
        RFB.messages.xvpOp(this._sock, ver, op);
    }

    _updateCursor(rgba, hotx, hoty, w, h) {
        this._cursorImage = {
            rgbaPixels: rgba,
            hotx: hotx, hoty: hoty, w: w, h: h,
        };
        this._refreshCursor();
    }

    _shouldShowDotCursor() {

        if (!this._showDotCursor) {

            return false;
        }

        for (let i = 3; i < this._cursorImage.rgbaPixels.length; i += 4) {
            if (this._cursorImage.rgbaPixels[i]) {
                return false;
            }
        }

        return true;
    }

    _refreshCursor() {
        if (this._rfbConnectionState !== "connecting" &&
            this._rfbConnectionState !== "connected") {
            return;
        }
        const image = this._shouldShowDotCursor() ? RFB.cursors.dot : this._cursorImage;
        this._cursor.change(image.rgbaPixels,
                            image.hotx, image.hoty,
                            image.w, image.h
        );
    }

    static genDES(password, challenge) {
        const passwordChars = password.split('').map(c => c.charCodeAt(0));
        const key = legacyCrypto.importKey(
            "raw", passwordChars, { name: "DES-ECB" }, false, ["encrypt"]);
        return legacyCrypto.encrypt({ name: "DES-ECB" }, key, challenge);
    }
}


RFB.messages = {
    keyEvent(sock, keysym, down) {
        sock.sQpush8(4);
        sock.sQpush8(down);

        sock.sQpush16(0);

        sock.sQpush32(keysym);

        sock.flush();
    },

    QEMUExtendedKeyEvent(sock, keysym, down, keycode) {
        function getRFBkeycode(xtScanCode) {
            const upperByte = (keycode >> 8);
            const lowerByte = (keycode & 0x00ff);
            if (upperByte === 0xe0 && lowerByte < 0x7f) {
                return lowerByte | 0x80;
            }
            return xtScanCode;
        }

        sock.sQpush8(255);
        sock.sQpush8(0);

        sock.sQpush16(down);

        sock.sQpush32(keysym);

        const RFBkeycode = getRFBkeycode(keycode);

        sock.sQpush32(RFBkeycode);

        sock.flush();
    },

    pointerEvent(sock, x, y, mask) {
        sock.sQpush8(5);

        mask = mask & 0x7f; // the marker must stay clear to distinguish the normal pointer message
        sock.sQpush8(mask);

        sock.sQpush16(x);
        sock.sQpush16(y);

        sock.flush();
    },

    extendedPointerEvent(sock, x, y, mask) {
        sock.sQpush8(5);

        let higherBits = (mask >> 7) & 0xff;


        if (higherBits & 0xfc) {
            throw new Error("Invalid mouse button mask: " + mask);
        }

        let lowerBits = mask & 0x7f;
        lowerBits |= 0x80;

        sock.sQpush8(lowerBits);
        sock.sQpush16(x);
        sock.sQpush16(y);
        sock.sQpush8(higherBits);

        sock.flush();
    },


    _buildExtendedClipboardFlags(actions, formats) {
        let data = new Uint8Array(4);
        let formatFlag = 0x00000000;
        let actionFlag = 0x00000000;

        for (let i = 0; i < actions.length; i++) {
            actionFlag |= actions[i];
        }

        for (let i = 0; i < formats.length; i++) {
            formatFlag |= formats[i];
        }

        data[0] = actionFlag >> 24;
        data[1] = 0x00;
        data[2] = 0x00;
        data[3] = formatFlag;

        return data;
    },

    extendedClipboardProvide(sock, formats, inData) {

        let deflator = new Deflator();
        let dataToDeflate = [];

        for (let i = 0; i < formats.length; i++) {

            if (formats[i] != extendedClipboardFormatText) {
                throw new Error("Unsupported extended clipboard format for Provide message.");
            }


            inData[i] = inData[i].replace(/\r\n|\r|\n/gm, "\r\n"); // the clipboard protocol requires paired carriage return and line feed


            let text = encodeUTF8(inData[i] + "\0");

            dataToDeflate.push( (text.length >> 24) & 0xFF,
                                (text.length >> 16) & 0xFF,
                                (text.length >>  8) & 0xFF,
                                (text.length & 0xFF));

            for (let j = 0; j < text.length; j++) {
                dataToDeflate.push(text.charCodeAt(j));
            }
        }

        let deflatedData = deflator.deflate(new Uint8Array(dataToDeflate));


        let data = new Uint8Array(4 + deflatedData.length);
        data.set(RFB.messages._buildExtendedClipboardFlags([extendedClipboardActionProvide],
                                                           formats));
        data.set(deflatedData, 4);

        RFB.messages.clientCutText(sock, data, true);
    },

    extendedClipboardNotify(sock, formats) {
        let flags = RFB.messages._buildExtendedClipboardFlags([extendedClipboardActionNotify],
                                                              formats);
        RFB.messages.clientCutText(sock, flags, true);
    },

    extendedClipboardRequest(sock, formats) {
        let flags = RFB.messages._buildExtendedClipboardFlags([extendedClipboardActionRequest],
                                                              formats);
        RFB.messages.clientCutText(sock, flags, true);
    },

    extendedClipboardCaps(sock, actions, formats) {
        let formatKeys = Object.keys(formats);
        let data  = new Uint8Array(4 + (4 * formatKeys.length));

        formatKeys.map(x => parseInt(x));
        formatKeys.sort((a, b) =>  a - b);

        data.set(RFB.messages._buildExtendedClipboardFlags(actions, []));

        let loopOffset = 4;
        for (let i = 0; i < formatKeys.length; i++) {
            data[loopOffset]     = formats[formatKeys[i]] >> 24;
            data[loopOffset + 1] = formats[formatKeys[i]] >> 16;
            data[loopOffset + 2] = formats[formatKeys[i]] >> 8;
            data[loopOffset + 3] = formats[formatKeys[i]] >> 0;

            loopOffset += 4;
            data[3] |= (1 << formatKeys[i]);
        }

        RFB.messages.clientCutText(sock, data, true);
    },

    clientCutText(sock, data, extended = false) {
        sock.sQpush8(6);

        sock.sQpush8(0);
        sock.sQpush8(0);
        sock.sQpush8(0);

        let length;
        if (extended) {
            length = toUnsigned32bit(-data.length);
        } else {
            length = data.length;
        }

        sock.sQpush32(length);
        sock.sQpushBytes(data);
        sock.flush();
    },

    setDesktopSize(sock, width, height, id, flags) {
        sock.sQpush8(251);

        sock.sQpush8(0);

        sock.sQpush16(width);
        sock.sQpush16(height);

        sock.sQpush8(1);

        sock.sQpush8(0);


        sock.sQpush32(id);
        sock.sQpush16(0);
        sock.sQpush16(0);
        sock.sQpush16(width);
        sock.sQpush16(height);
        sock.sQpush32(flags);

        sock.flush();
    },

    clientFence(sock, flags, payload) {
        sock.sQpush8(248);

        sock.sQpush8(0);
        sock.sQpush8(0);
        sock.sQpush8(0);

        sock.sQpush32(flags);

        sock.sQpush8(payload.length);
        sock.sQpushString(payload);

        sock.flush();
    },

    enableContinuousUpdates(sock, enable, x, y, width, height) {
        sock.sQpush8(150);

        sock.sQpush8(enable);

        sock.sQpush16(x);
        sock.sQpush16(y);
        sock.sQpush16(width);
        sock.sQpush16(height);

        sock.flush();
    },

    pixelFormat(sock, depth, trueColor) {
        let bpp;

        if (depth > 16) {
            bpp = 32;
        } else if (depth > 8) {
            bpp = 16;
        } else {
            bpp = 8;
        }

        const bits = Math.floor(depth/3);

        sock.sQpush8(0);

        sock.sQpush8(0);
        sock.sQpush8(0);
        sock.sQpush8(0);

        sock.sQpush8(bpp);
        sock.sQpush8(depth);
        sock.sQpush8(0);
        sock.sQpush8(trueColor ? 1 : 0);

        sock.sQpush16((1 << bits) - 1);
        sock.sQpush16((1 << bits) - 1);
        sock.sQpush16((1 << bits) - 1);

        sock.sQpush8(bits * 0);
        sock.sQpush8(bits * 1);
        sock.sQpush8(bits * 2);

        sock.sQpush8(0);
        sock.sQpush8(0);
        sock.sQpush8(0);

        sock.flush();
    },

    clientEncodings(sock, encodings) {
        sock.sQpush8(2);

        sock.sQpush8(0);

        sock.sQpush16(encodings.length);
        for (let i = 0; i < encodings.length; i++) {
            sock.sQpush32(encodings[i]);
        }

        sock.flush();
    },

    fbUpdateRequest(sock, incremental, x, y, w, h) {
        if (typeof(x) === "undefined") { x = 0; }
        if (typeof(y) === "undefined") { y = 0; }

        sock.sQpush8(3);

        sock.sQpush8(incremental ? 1 : 0);

        sock.sQpush16(x);
        sock.sQpush16(y);
        sock.sQpush16(w);
        sock.sQpush16(h);

        sock.flush();
    },

    xvpOp(sock, ver, op) {
        sock.sQpush8(250);

        sock.sQpush8(0);

        sock.sQpush8(ver);
        sock.sQpush8(op);

        sock.flush();
    }
};

RFB.cursors = {
    none: {
        rgbaPixels: new Uint8Array(),
        w: 0, h: 0,
        hotx: 0, hoty: 0,
    },

    dot: {
        /* eslint-disable indent */
        rgbaPixels: new Uint8Array([
            255, 255, 255, 255,   0,   0,   0, 255, 255, 255, 255, 255,
              0,   0,   0, 255,   0,   0,   0,   0,   0,   0,  0,  255,
            255, 255, 255, 255,   0,   0,   0, 255, 255, 255, 255, 255,
        ]),
        /* eslint-enable indent */
        w: 3, h: 3,
        hotx: 1, hoty: 1,
    }
};
