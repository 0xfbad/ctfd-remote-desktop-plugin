/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 *
 */

import * as Log from '../util/logging.js';

export default class HextileDecoder {
    constructor() {
        this._tiles = 0;
        this._lastsubencoding = 0;
        this._tileBuffer = new Uint8Array(16 * 16 * 4);
    }

    decodeRect(x, y, width, height, sock, display, depth) {
        if (this._tiles === 0) {
            this._tilesX = Math.ceil(width / 16);
            this._tilesY = Math.ceil(height / 16);
            this._totalTiles = this._tilesX * this._tilesY;
            this._tiles = this._totalTiles;
        }

        while (this._tiles > 0) {
            let bytes = 1;

            if (sock.rQwait("HEXTILE", bytes)) {
                return false;
            }

            let subencoding = sock.rQpeek8();
            if (subencoding > 30) {
                throw new Error("Illegal hextile subencoding (subencoding: " +
                            subencoding + ")");
            }

            const currTile = this._totalTiles - this._tiles;
            const tileX = currTile % this._tilesX;
            const tileY = Math.floor(currTile / this._tilesX);
            const tx = x + tileX * 16;
            const ty = y + tileY * 16;
            const tw = Math.min(16, (x + width) - tx);
            const th = Math.min(16, (y + height) - ty);


            if (subencoding & 0x01) {
                bytes += tw * th * 4;
            } else {
                if (subencoding & 0x02) {
                    bytes += 4;
                }
                if (subencoding & 0x04) {
                    bytes += 4;
                }
                if (subencoding & 0x08) {
                    bytes++;

                    if (sock.rQwait("HEXTILE", bytes)) {
                        return false;
                    }

                    let subrects = sock.rQpeekBytes(bytes).at(-1);
                    if (subencoding & 0x10) {
                        bytes += subrects * (4 + 2);
                    } else {
                        bytes += subrects * 2;
                    }
                }
            }

            if (sock.rQwait("HEXTILE", bytes)) {
                return false;
            }


            sock.rQshift8();
            if (subencoding === 0) {
                if (this._lastsubencoding & 0x01) {

                    Log.Debug("     Ignoring blank after RAW");
                } else {
                    display.fillRect(tx, ty, tw, th, this._background);
                }
            } else if (subencoding & 0x01) {
                let pixels = tw * th;
                let data = sock.rQshiftBytes(pixels * 4, false);

                for (let i = 0;i <  pixels;i++) {
                    data[i * 4 + 3] = 255;
                }
                display.blitImage(tx, ty, tw, th, data, 0);
            } else {
                if (subencoding & 0x02) {
                    this._background = new Uint8Array(sock.rQshiftBytes(4));
                }
                if (subencoding & 0x04) {
                    this._foreground = new Uint8Array(sock.rQshiftBytes(4));
                }

                this._startTile(tx, ty, tw, th, this._background);
                if (subencoding & 0x08) {
                    let subrects = sock.rQshift8();

                    for (let s = 0; s < subrects; s++) {
                        let color;
                        if (subencoding & 0x10) {
                            color = sock.rQshiftBytes(4);
                        } else {
                            color = this._foreground;
                        }
                        const xy = sock.rQshift8();
                        const sx = (xy >> 4);
                        const sy = (xy & 0x0f);

                        const wh = sock.rQshift8();
                        const sw = (wh >> 4) + 1;
                        const sh = (wh & 0x0f) + 1;

                        this._subTile(sx, sy, sw, sh, color);
                    }
                }
                this._finishTile(display);
            }
            this._lastsubencoding = subencoding;
            this._tiles--;
        }

        return true;
    }


    _startTile(x, y, width, height, color) {
        this._tileX = x;
        this._tileY = y;
        this._tileW = width;
        this._tileH = height;

        const red = color[0];
        const green = color[1];
        const blue = color[2];

        const data = this._tileBuffer;
        for (let i = 0; i < width * height * 4; i += 4) {
            data[i]     = red;
            data[i + 1] = green;
            data[i + 2] = blue;
            data[i + 3] = 255;
        }
    }


    _subTile(x, y, w, h, color) {
        const red = color[0];
        const green = color[1];
        const blue = color[2];
        const xend = x + w;
        const yend = y + h;

        const data = this._tileBuffer;
        const width = this._tileW;
        for (let j = y; j < yend; j++) {
            for (let i = x; i < xend; i++) {
                const p = (i + (j * width)) * 4;
                data[p]     = red;
                data[p + 1] = green;
                data[p + 2] = blue;
                data[p + 3] = 255;
            }
        }
    }


    _finishTile(display) {
        display.blitImage(this._tileX, this._tileY,
                          this._tileW, this._tileH,
                          this._tileBuffer, 0);
    }
}
