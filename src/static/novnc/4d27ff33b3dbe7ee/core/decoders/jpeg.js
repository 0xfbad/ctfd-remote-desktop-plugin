/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2019 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 *
 */

export default class JPEGDecoder {
    constructor() {


        this._cachedQuantTables = []; // realvnc can omit tables reused from earlier images
        this._cachedHuffmanTables = [];

        this._segments = [];
    }

    decodeRect(x, y, width, height, sock, display, depth) {

        while (true) {
            let segment = this._readSegment(sock);
            if (segment === null) {
                return false;
            }
            this._segments.push(segment);

            if (segment[1] === 0xD9) {
                break;
            }
        }

        let huffmanTables = [];
        let quantTables = [];
        for (let segment of this._segments) {
            let type = segment[1];
            if (type === 0xC4) {

                huffmanTables.push(segment);
            } else if (type === 0xDB) {

                quantTables.push(segment);
            }
        }

        const sofIndex = this._segments.findIndex(
            x => x[1] == 0xC0 || x[1] == 0xC2
        );
        if (sofIndex == -1) {
            throw new Error("Illegal JPEG image without SOF");
        }

        if (quantTables.length === 0) {
            this._segments.splice(sofIndex+1, 0,
                                  ...this._cachedQuantTables);
        }
        if (huffmanTables.length === 0) {
            this._segments.splice(sofIndex+1, 0,
                                  ...this._cachedHuffmanTables);
        }

        let length = 0;
        for (let segment of this._segments) {
            length += segment.length;
        }

        let data = new Uint8Array(length);
        length = 0;
        for (let segment of this._segments) {
            data.set(segment, length);
            length += segment.length;
        }

        display.imageRect(x, y, width, height, "image/jpeg", data);

        if (huffmanTables.length !== 0) {
            this._cachedHuffmanTables = huffmanTables;
        }
        if (quantTables.length !== 0) {
            this._cachedQuantTables = quantTables;
        }

        this._segments = [];

        return true;
    }

    _readSegment(sock) {
        if (sock.rQwait("JPEG", 2)) {
            return null;
        }

        let marker = sock.rQshift8();
        if (marker != 0xFF) {
            throw new Error("Illegal JPEG marker received (byte: " +
                               marker + ")");
        }
        let type = sock.rQshift8();
        if (type >= 0xD0 && type <= 0xD9 || type == 0x01) {

            return new Uint8Array([marker, type]);
        }

        if (sock.rQwait("JPEG", 2, 2)) {
            return null;
        }

        let length = sock.rQshift16();
        if (length < 2) {
            throw new Error("Illegal JPEG length received (length: " +
                               length + ")");
        }

        if (sock.rQwait("JPEG", length-2, 4)) {
            return null;
        }

        let extra = 0;
        if (type === 0xDA) {

            extra += 2;
            while (true) {
                if (sock.rQwait("JPEG", length-2+extra, 4)) {
                    return null;
                }
                let data = sock.rQpeekBytes(length-2+extra, false);
                if (data.at(-2) === 0xFF && data.at(-1) !== 0x00 &&
                    !(data.at(-1) >= 0xD0 && data.at(-1) <= 0xD7)) {
                    extra -= 2;
                    break;
                }
                extra++;
            }
        }

        let segment = new Uint8Array(2 + length + extra);
        segment[0] = marker;
        segment[1] = type;
        segment[2] = length >> 8;
        segment[3] = length;
        segment.set(sock.rQshiftBytes(length-2+extra, false), 4);

        return segment;
    }
}
