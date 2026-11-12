export function releaseInputOnNavigation(UI, releaseCapture) {
    function release() {
        const rfb = UI.rfb;
        releaseCapture();

        if (!rfb)
            return;

        rfb._keyboard._allKeysUp();

        if (rfb._mouseButtonMask) {
            const {x, y} = rfb._mousePos;
            rfb._handleMouseButton(x, y, 0);
        }
        rfb._viewportDragging = false;
    }

    const lifecycle = new AbortController();
    parent.addEventListener('beforeunload', release, {signal: lifecycle.signal}); // a canceled browser dialog can swallow key releases without blurring the viewer
    window.addEventListener('pagehide', event => {
        if (!event.persisted)
            lifecycle.abort();
    });
}
