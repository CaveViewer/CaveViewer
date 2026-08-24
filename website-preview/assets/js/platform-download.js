(() => {
    // Keep one clear default action for the reported desktop platform while
    // leaving every supported installer available through the chooser.
    const picker = document.querySelector('[data-platform-download]');
    if (!picker) return;

    const primary = picker.querySelector('[data-primary-download]');
    const primaryLabel = picker.querySelector('[data-primary-label]');
    const primaryDetail = picker.querySelector('[data-primary-detail]');
    const installNote = picker.parentElement?.querySelector('[data-platform-install-note]');
    const dialog = picker.querySelector('[data-platform-dialog]');
    const dialogOpen = picker.querySelector('[data-platform-dialog-open]');
    const dialogClose = picker.querySelector('[data-platform-dialog-close]');
    const macToggle = picker.querySelector('[data-mac-download-toggle]');
    const macOptions = picker.querySelector('[data-mac-download-options]');
    const linuxToggle = picker.querySelector('[data-linux-download-toggle]');
    const linuxOptions = picker.querySelector('[data-linux-download-options]');

    const downloads = {
        windows: {
            label: 'Get CaveViewer for Windows',
            detail: 'Preview 1.0.92 · Windows 10 or 11',
            href: 'https://github.com/CaveViewer/CaveViewer/releases/download/v1.0.92/CaveViewer-1.0.92-windows.exe',
            installNote: 'After downloading, open the setup file and follow its prompts.',
        },
        linux: {
            label: 'Get CaveViewer for Linux',
            detail: 'Preview 1.0.92 · x86_64 AppImage',
            href: 'https://github.com/CaveViewer/CaveViewer/releases/download/v1.0.92/CaveViewer-1.0.92-x86_64.AppImage',
            installNote: 'After downloading, allow the AppImage to run in your file manager, then open it.',
        },
        macos: {
            label: 'Get CaveViewer for macOS',
            detail: 'Preview 1.0.92 · Choose Apple silicon or Intel',
            href: '#mac-download-options',
            installNote: 'Choose your Mac type in the next step, then drag CaveViewer into Applications.',
        },
        unknown: {
            label: 'Choose your desktop platform',
            detail: 'CaveViewer Preview 1.0.92',
            href: '#other-platforms',
            installNote: 'Choose your platform to see the simple next steps.',
        },
    };

    const reportedPlatform = () => {
        const clientHint = navigator.userAgentData?.platform || '';
        const legacyPlatform = navigator.platform || '';
        const userAgent = navigator.userAgent || '';
        const reported = `${clientHint} ${legacyPlatform} ${userAgent}`.toLowerCase();

        const looksLikeIPad = /ipad|iphone|ipod/.test(reported)
            || (/mac/.test(reported) && navigator.maxTouchPoints > 1);
        if (looksLikeIPad || /android|cros/.test(reported)) return 'unknown';
        if (/windows|win32|win64/.test(reported)) return 'windows';
        if (/macos|macintosh|macintel|macppc/.test(reported)) return 'macos';
        if (/linux|x11/.test(reported)) return 'linux';
        return 'unknown';
    };

    const setMacChoices = (open, { focus = false } = {}) => {
        if (!macToggle || !macOptions) return;
        macToggle.setAttribute('aria-expanded', String(open));
        macOptions.hidden = !open;
        if (open && focus) macOptions.querySelector('a')?.focus();
    };

    const setLinuxChoices = (open, { focus = false } = {}) => {
        if (!linuxToggle || !linuxOptions) return;
        linuxToggle.setAttribute('aria-expanded', String(open));
        linuxOptions.hidden = !open;
        if (open && focus) linuxOptions.querySelector('a')?.focus();
    };

    const openDialog = ({ mac = false, linux = false } = {}) => {
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else dialog.setAttribute('open', '');
        document.body.classList.add('platform-dialog-open');
        setMacChoices(mac, { focus: mac });
        setLinuxChoices(linux, { focus: linux });
    };

    const closeDialog = () => {
        if (typeof dialog.close === 'function' && dialog.open) dialog.close();
        else {
            dialog.removeAttribute('open');
            setMacChoices(false);
            setLinuxChoices(false);
        }
        document.body.classList.remove('platform-dialog-open');
    };

    macToggle?.addEventListener('click', () => {
        const open = macToggle.getAttribute('aria-expanded') !== 'true';
        setMacChoices(open);
        if (open) setLinuxChoices(false);
    });
    linuxToggle?.addEventListener('click', () => {
        const open = linuxToggle.getAttribute('aria-expanded') !== 'true';
        setLinuxChoices(open);
        if (open) setMacChoices(false);
    });

    dialogOpen?.addEventListener('click', () => openDialog());
    dialogClose?.addEventListener('click', closeDialog);
    dialog?.addEventListener('close', () => {
        document.body.classList.remove('platform-dialog-open');
        setMacChoices(false);
        setLinuxChoices(false);
    });
    dialog?.addEventListener('click', event => {
        if (event.target === dialog) closeDialog();
    });
    dialog?.querySelectorAll('a').forEach(link => link.addEventListener('click', closeDialog));

    const platform = reportedPlatform();
    const selected = downloads[platform];
    primaryLabel.textContent = selected.label;
    primaryDetail.textContent = selected.detail;
    primary.href = selected.href;
    if (installNote) installNote.textContent = selected.installNote;

    if (platform === 'macos') {
        primary.addEventListener('click', event => {
            event.preventDefault();
            openDialog({ mac: true });
        });
    } else if (platform === 'linux' && linuxToggle && linuxOptions) {
        primary.addEventListener('click', event => {
            event.preventDefault();
            openDialog({ linux: true });
        });
    } else if (platform === 'unknown') {
        primary.addEventListener('click', event => {
            event.preventDefault();
            openDialog();
        });
    }
})();
