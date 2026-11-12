export function collapseInitialControls(UI) {
    const bar = document.getElementById('noVNC_control_bar');
    const wasOpen = bar.classList.contains('noVNC_open');
    const transition = bar.style.transition;
    bar.style.transition = 'none';
    bar.classList.remove('noVNC_open');
    bar.getBoundingClientRect(); // settle the collapsed position before restoring animation
    bar.style.transition = transition;

    if (wasOpen || UI.rfb)
        return;

    const openControlbar = UI.openControlbar; // initialization can finish after the viewer load event
    UI.openControlbar = () => {
        UI.openControlbar = openControlbar;
    };
}
