/*
 * noVNC: HTML5 VNC client
 * Copyright (C) 2018 The noVNC authors
 * Licensed under MPL 2.0 (see LICENSE.txt)
 *
 * See README.md for usage and integration instructions.
 */

export class Localizer {
    constructor() {

        this.language = 'en';

        this._dictionary = undefined;
    }

    async setup(supportedLanguages, baseURL) {
        this.language = 'en';
        this._dictionary = undefined;

        this._setupLanguage(supportedLanguages);
        await this._setupDictionary(baseURL);
    }

    _setupLanguage(supportedLanguages) {

        let userLanguages;
        if (typeof window.navigator.languages == 'object') {
            userLanguages = window.navigator.languages;
        } else {
            userLanguages = [navigator.language || navigator.userLanguage];
        }

        for (let i = 0;i < userLanguages.length;i++) {
            const userLang = userLanguages[i]
                .toLowerCase()
                .replace("_", "-")
                .split("-");

            for (let j = 0; j < supportedLanguages.length; j++) {
                const supLang = supportedLanguages[j]
                    .toLowerCase()
                    .replace("_", "-")
                    .split("-");

                if (userLang[0] !== supLang[0]) {
                    continue;
                }
                if (userLang[1] !== supLang[1]) {
                    continue;
                }

                this.language = supportedLanguages[j];
                return;
            }

            if (userLang[0] === 'en') {
                return;
            }

            for (let j = 0;j < supportedLanguages.length;j++) {
                const supLang = supportedLanguages[j]
                    .toLowerCase()
                    .replace("_", "-")
                    .split("-");

                if (userLang[0] !== supLang[0]) {
                    continue;
                }
                if (supLang[1] !== undefined) {
                    continue;
                }

                this.language = supportedLanguages[j];
                return;
            }
        }
    }

    async _setupDictionary(baseURL) {
        if (baseURL) {
            if (!baseURL.endsWith("/")) {
                baseURL = baseURL + "/";
            }
        } else {
            baseURL = "";
        }

        if (this.language === "en") {
            return;
        }

        let response = await fetch(baseURL + this.language + ".json");
        if (!response.ok) {
            throw Error("" + response.status + " " + response.statusText);
        }

        this._dictionary = await response.json();
    }

    get(id) {
        if (typeof this._dictionary !== 'undefined' &&
            this._dictionary[id]) {
            return this._dictionary[id];
        } else {
            return id;
        }
    }

    translateDOM() {
        const self = this;

        function process(elem, enabled) {
            function isAnyOf(searchElement, items) {
                return items.indexOf(searchElement) !== -1;
            }

            function translateString(str) {

                str = str.split("\n").map(s => s.trim()).join(" ").trim(); // source formatting whitespace must not enter translation keys
                return self.get(str);
            }

            function translateAttribute(elem, attr) {
                const str = translateString(elem.getAttribute(attr));
                elem.setAttribute(attr, str);
            }

            function translateTextNode(node) {
                const str = translateString(node.data);
                node.data = str;
            }

            if (elem.hasAttribute("translate")) {
                if (isAnyOf(elem.getAttribute("translate"), ["", "yes"])) {
                    enabled = true;
                } else if (isAnyOf(elem.getAttribute("translate"), ["no"])) {
                    enabled = false;
                }
            }

            if (enabled) {
                if (elem.hasAttribute("abbr") &&
                    elem.tagName === "TH") {
                    translateAttribute(elem, "abbr");
                }
                if (elem.hasAttribute("alt") &&
                    isAnyOf(elem.tagName, ["AREA", "IMG", "INPUT"])) {
                    translateAttribute(elem, "alt");
                }
                if (elem.hasAttribute("download") &&
                    isAnyOf(elem.tagName, ["A", "AREA"])) {
                    translateAttribute(elem, "download");
                }
                if (elem.hasAttribute("label") &&
                    isAnyOf(elem.tagName, ["MENUITEM", "MENU", "OPTGROUP",
                                           "OPTION", "TRACK"])) {
                    translateAttribute(elem, "label");
                }

                if (elem.hasAttribute("placeholder") &&
                    isAnyOf(elem.tagName, ["INPUT", "TEXTAREA"])) {
                    translateAttribute(elem, "placeholder");
                }
                if (elem.hasAttribute("title")) {
                    translateAttribute(elem, "title");
                }
                if (elem.hasAttribute("value") &&
                    elem.tagName === "INPUT" &&
                    isAnyOf(elem.getAttribute("type"), ["reset", "button", "submit"])) {
                    translateAttribute(elem, "value");
                }
            }

            for (let i = 0; i < elem.childNodes.length; i++) {
                const node = elem.childNodes[i];
                if (node.nodeType === node.ELEMENT_NODE) {
                    process(node, enabled);
                } else if (node.nodeType === node.TEXT_NODE && enabled) {
                    translateTextNode(node);
                }
            }
        }

        process(document.body, true);
    }
}

export const l10n = new Localizer();
export default l10n.get.bind(l10n);
