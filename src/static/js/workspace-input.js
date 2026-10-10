const bridgeUrl = new URL('./workspace-clipboard.js', import.meta.url).href;

for (const id of ['vnc-frame', 'terminal-frame']) {
    const frame = document.getElementById(id);
    if (!frame) continue;

    const install = () => {
        const page = frame.contentDocument;
        if (!page?.head || page.URL === 'about:blank' || page.getElementById('workspace-clipboard-bridge')) return;
        const script = page.createElement('script');
        script.id = 'workspace-clipboard-bridge';
        script.type = 'module';
        script.src = bridgeUrl;
        page.head.append(script);
    };

    frame.addEventListener('load', install);
    install();
}

document.addEventListener('keydown', event => {
    if (event.ctrlKey && event.shiftKey && event.code === 'KeyC') event.preventDefault();
}, true);

document.addEventListener('click', event => {
    if (!(event.detail > 0)) return;

    const mode = event.target.closest('.mode-tab.active')?.dataset.mode;
    if (mode === 'terminal') {
        const frame = document.getElementById('terminal-frame');
        const term = frame?.contentWindow?.term;
        if (term) term.focus();
        else frame?.focus({preventScroll: true});
    }
    if (mode === 'desktop') {
        const frame = document.getElementById('vnc-frame');
        (frame?.contentDocument?.querySelector('#noVNC_container canvas') ?? frame)
            ?.focus({preventScroll: true});
    }
});
