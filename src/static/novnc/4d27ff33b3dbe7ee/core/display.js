/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 */

import * as Log from './util/logging.js';
import Base64 from "./base64.js";
import { toSigned32bit } from './util/int.js';

export default class Display {
    constructor(target) {
        this._drawCtx = null;

        this._renderQ = [];
        this._flushPromise = null;


        this._fbWidth = 0;
        this._fbHeight = 0;

        this._prevDrawStyle = "";

        Log.Debug(">> Display.constructor");


        this._target = target;

        if (!this._target) {
            throw new Error("Target must be set");
        }

        if (typeof this._target === 'string') {
            throw new Error('target must be a DOM element');
        }

        if (!this._target.getContext) {
            throw new Error("no getContext method");
        }

        this._targetCtx = this._target.getContext('2d');


        this._viewportLoc = { 'x': 0, 'y': 0, 'w': this._target.width, 'h': this._target.height };


        this._backbuffer = document.createElement('canvas');
        this._drawCtx = this._backbuffer.getContext('2d');

        this._damageBounds = { left: 0, top: 0,
                               right: this._backbuffer.width,
                               bottom: this._backbuffer.height };

        Log.Debug("User Agent: " + navigator.userAgent);

        Log.Debug("<< Display.constructor");

        this._scale = 1.0;
        this._clipViewport = false;
    }

    get scale() { return this._scale; }
    set scale(scale) {
        this._rescale(scale);
    }

    get clipViewport() { return this._clipViewport; }
    set clipViewport(viewport) {
        this._clipViewport = viewport;

        const vp = this._viewportLoc;
        this.viewportChangeSize(vp.w, vp.h);
        this.viewportChangePos(0, 0);
    }

    get width() {
        return this._fbWidth;
    }

    get height() {
        return this._fbHeight;
    }

    viewportChangePos(deltaX, deltaY) {
        const vp = this._viewportLoc;
        deltaX = Math.floor(deltaX);
        deltaY = Math.floor(deltaY);

        if (!this._clipViewport) {
            deltaX = -vp.w;
            deltaY = -vp.h;
        }

        const vx2 = vp.x + vp.w - 1;
        const vy2 = vp.y + vp.h - 1;

        if (deltaX < 0 && vp.x + deltaX < 0) {
            deltaX = -vp.x;
        }
        if (vx2 + deltaX >= this._fbWidth) {
            deltaX -= vx2 + deltaX - this._fbWidth + 1;
        }

        if (vp.y + deltaY < 0) {
            deltaY = -vp.y;
        }
        if (vy2 + deltaY >= this._fbHeight) {
            deltaY -= (vy2 + deltaY - this._fbHeight + 1);
        }

        if (deltaX === 0 && deltaY === 0) {
            return;
        }
        Log.Debug("viewportChange deltaX: " + deltaX + ", deltaY: " + deltaY);

        vp.x += deltaX;
        vp.y += deltaY;

        this._damage(vp.x, vp.y, vp.w, vp.h);

        this.flip();
    }

    viewportChangeSize(width, height) {

        if (!this._clipViewport ||
            typeof(width) === "undefined" ||
            typeof(height) === "undefined") {

            Log.Debug("Setting viewport to full display region");
            width = this._fbWidth;
            height = this._fbHeight;
        }

        width = Math.floor(width);
        height = Math.floor(height);

        if (width > this._fbWidth) {
            width = this._fbWidth;
        }
        if (height > this._fbHeight) {
            height = this._fbHeight;
        }

        const vp = this._viewportLoc;
        if (vp.w !== width || vp.h !== height) {
            vp.w = width;
            vp.h = height;

            const canvas = this._target;
            canvas.width = width;
            canvas.height = height;


            this.viewportChangePos(0, 0);

            this._damage(vp.x, vp.y, vp.w, vp.h);
            this.flip();


            this._rescale(this._scale);
        }
    }

    absX(x) {
        if (this._scale === 0) {
            return 0;
        }
        return toSigned32bit(x / this._scale + this._viewportLoc.x);
    }

    absY(y) {
        if (this._scale === 0) {
            return 0;
        }
        return toSigned32bit(y / this._scale + this._viewportLoc.y);
    }

    resize(width, height) {
        this._prevDrawStyle = "";

        this._fbWidth = width;
        this._fbHeight = height;

        const canvas = this._backbuffer;
        if (canvas.width !== width || canvas.height !== height) {


            let saveImg = null; // resizing the canvas clears its contents
            if (canvas.width > 0 && canvas.height > 0) {
                saveImg = this._drawCtx.getImageData(0, 0, canvas.width, canvas.height);
            }

            if (canvas.width !== width) {
                canvas.width = width;
            }
            if (canvas.height !== height) {
                canvas.height = height;
            }

            if (saveImg) {
                this._drawCtx.putImageData(saveImg, 0, 0);
            }
        }

        const vp = this._viewportLoc;
        this.viewportChangeSize(vp.w, vp.h);
        this.viewportChangePos(0, 0);
    }

    getImageData() {
        return this._drawCtx.getImageData(0, 0, this.width, this.height);
    }

    toDataURL(type, encoderOptions) {
        return this._backbuffer.toDataURL(type, encoderOptions);
    }

    toBlob(callback, type, quality) {
        return this._backbuffer.toBlob(callback, type, quality);
    }


    _damage(x, y, w, h) {
        if (x < this._damageBounds.left) {
            this._damageBounds.left = x;
        }
        if (y < this._damageBounds.top) {
            this._damageBounds.top = y;
        }
        if ((x + w) > this._damageBounds.right) {
            this._damageBounds.right = x + w;
        }
        if ((y + h) > this._damageBounds.bottom) {
            this._damageBounds.bottom = y + h;
        }
    }

    flip(fromQueue) {
        if (this._renderQ.length !== 0 && !fromQueue) {
            this._renderQPush({
                'type': 'flip'
            });
        } else {
            let x = this._damageBounds.left;
            let y = this._damageBounds.top;
            let w = this._damageBounds.right - x;
            let h = this._damageBounds.bottom - y;

            let vx = x - this._viewportLoc.x;
            let vy = y - this._viewportLoc.y;

            if (vx < 0) {
                w += vx;
                x -= vx;
                vx = 0;
            }
            if (vy < 0) {
                h += vy;
                y -= vy;
                vy = 0;
            }

            if ((vx + w) > this._viewportLoc.w) {
                w = this._viewportLoc.w - vx;
            }
            if ((vy + h) > this._viewportLoc.h) {
                h = this._viewportLoc.h - vy;
            }

            if ((w > 0) && (h > 0)) {

                this._targetCtx.drawImage(this._backbuffer,
                                          x, y, w, h,
                                          vx, vy, w, h);
            }

            this._damageBounds.left = this._damageBounds.top = 65535;
            this._damageBounds.right = this._damageBounds.bottom = 0;
        }
    }

    pending() {
        return this._renderQ.length > 0;
    }

    flush() {
        if (this._renderQ.length === 0) {
            return Promise.resolve();
        } else {
            if (this._flushPromise === null) {
                this._flushPromise = new Promise((resolve) => {
                    this._flushResolve = resolve;
                });
            }
            return this._flushPromise;
        }
    }

    fillRect(x, y, width, height, color, fromQueue) {
        if (this._renderQ.length !== 0 && !fromQueue) {
            this._renderQPush({
                'type': 'fill',
                'x': x,
                'y': y,
                'width': width,
                'height': height,
                'color': color
            });
        } else {
            this._setFillColor(color);
            this._drawCtx.fillRect(x, y, width, height);
            this._damage(x, y, width, height);
        }
    }

    copyImage(oldX, oldY, newX, newY, w, h, fromQueue) {
        if (this._renderQ.length !== 0 && !fromQueue) {
            this._renderQPush({
                'type': 'copy',
                'oldX': oldX,
                'oldY': oldY,
                'x': newX,
                'y': newY,
                'width': w,
                'height': h,
            });
        } else {

            this._drawCtx.mozImageSmoothingEnabled = false; // image smoothing blurs copied pixels and resizing resets the smoothing settings
            this._drawCtx.webkitImageSmoothingEnabled = false;
            this._drawCtx.msImageSmoothingEnabled = false;
            this._drawCtx.imageSmoothingEnabled = false;

            this._drawCtx.drawImage(this._backbuffer,
                                    oldX, oldY, w, h,
                                    newX, newY, w, h);
            this._damage(newX, newY, w, h);
        }
    }

    imageRect(x, y, width, height, mime, arr) {

        if ((width === 0) || (height === 0)) { // the image queue cannot handle empty images
            return;
        }

        const img = new Image();
        img.src = "data: " + mime + ";base64," + Base64.encode(arr);

        this._renderQPush({
            'type': 'img',
            'img': img,
            'x': x,
            'y': y,
            'width': width,
            'height': height
        });
    }

    videoFrame(x, y, width, height, frame) {
        this._renderQPush({
            'type': 'frame',
            'frame': frame,
            'x': x,
            'y': y,
            'width': width,
            'height': height
        });
    }

    blitImage(x, y, width, height, arr, offset, fromQueue) {
        if (this._renderQ.length !== 0 && !fromQueue) {

            const newArr = new Uint8Array(width * height * 4);
            newArr.set(new Uint8Array(arr.buffer, 0, newArr.length));
            this._renderQPush({
                'type': 'blit',
                'data': newArr,
                'x': x,
                'y': y,
                'width': width,
                'height': height,
            });
        } else {

            let data = new Uint8ClampedArray(arr.buffer,
                                             arr.byteOffset + offset,
                                             width * height * 4);
            let img = new ImageData(data, width, height);
            this._drawCtx.putImageData(img, x, y);
            this._damage(x, y, width, height);
        }
    }

    drawImage(img, ...args) {
        this._drawCtx.drawImage(img, ...args);

        if (args.length <= 4) {
            const [x, y] = args;
            this._damage(x, y, img.width, img.height);
        } else {
            const [,, sw, sh, dx, dy] = args;
            this._damage(dx, dy, sw, sh);
        }
    }

    autoscale(containerWidth, containerHeight) {
        let scaleRatio;

        if (containerWidth === 0 || containerHeight === 0) {
            scaleRatio = 0;

        } else {

            const vp = this._viewportLoc;
            const targetAspectRatio = containerWidth / containerHeight;
            const fbAspectRatio = vp.w / vp.h;

            if (fbAspectRatio >= targetAspectRatio) {
                scaleRatio = containerWidth / vp.w;
            } else {
                scaleRatio = containerHeight / vp.h;
            }
        }

        this._rescale(scaleRatio);
    }

    _rescale(factor) {
        this._scale = factor;
        const vp = this._viewportLoc;

        const width = factor * vp.w + 'px'; // numeric canvas dimensions clear pixels, string dimensions scale without clearing
        const height = factor * vp.h + 'px';

        if ((this._target.style.width !== width) ||
            (this._target.style.height !== height)) {
            this._target.style.width = width;
            this._target.style.height = height;
        }
    }

    _setFillColor(color) {
        const newStyle = 'rgb(' + color[0] + ',' + color[1] + ',' + color[2] + ')';
        if (newStyle !== this._prevDrawStyle) {
            this._drawCtx.fillStyle = newStyle;
            this._prevDrawStyle = newStyle;
        }
    }

    _renderQPush(action) {
        this._renderQ.push(action);
        if (this._renderQ.length === 1) {


            this._scanRenderQ();
        }
    }

    _resumeRenderQ() {


        this.removeEventListener('load', this._noVNCDisplay._resumeRenderQ);
        this._noVNCDisplay._scanRenderQ();
    }

    _scanRenderQ() {
        let ready = true;
        while (ready && this._renderQ.length > 0) {
            const a = this._renderQ[0];
            switch (a.type) {
                case 'flip':
                    this.flip(true);
                    break;
                case 'copy':
                    this.copyImage(a.oldX, a.oldY, a.x, a.y, a.width, a.height, true);
                    break;
                case 'fill':
                    this.fillRect(a.x, a.y, a.width, a.height, a.color, true);
                    break;
                case 'blit':
                    this.blitImage(a.x, a.y, a.width, a.height, a.data, 0, true);
                    break;
                case 'img':
                    if (a.img.complete) {
                        if (a.img.width !== a.width || a.img.height !== a.height) {
                            Log.Error("Decoded image has incorrect dimensions. Got " +
                                      a.img.width + "x" + a.img.height + ". Expected " +
                                      a.width + "x" + a.height + ".");
                            return;
                        }
                        this.drawImage(a.img, a.x, a.y);
                    } else {
                        a.img._noVNCDisplay = this;
                        a.img.addEventListener('load', this._resumeRenderQ);


                        ready = false; // image decoding must finish before later drawing operations
                    }
                    break;
                case 'frame':
                    if (a.frame.ready) {

                        let frame = a.frame.frame; // encoded frames can exceed the rectangle and must be cropped
                        if (frame.codedWidth < a.width || frame.codedHeight < a.height) {
                            Log.Warn("Decoded video frame does not cover its full rectangle area. Expecting at least " +
                                      a.width + "x" + a.height + " but got " +
                                      frame.codedWidth + "x" + frame.codedHeight);
                        }
                        const sx = 0;
                        const sy = 0;
                        const sw = a.width;
                        const sh = a.height;
                        const dx = a.x;
                        const dy = a.y;
                        const dw = sw;
                        const dh = sh;
                        this.drawImage(frame, sx, sy, sw, sh, dx, dy, dw, dh);
                        frame.close();
                    } else {
                        let display = this;
                        a.frame.promise.then(() => {
                            display._scanRenderQ();
                        });
                        ready = false;
                    }
                    break;
            }

            if (ready) {
                this._renderQ.shift();
            }
        }

        if (this._renderQ.length === 0 &&
            this._flushPromise !== null) {
            this._flushResolve();
            this._flushPromise = null;
            this._flushResolve = null;
        }
    }
}
