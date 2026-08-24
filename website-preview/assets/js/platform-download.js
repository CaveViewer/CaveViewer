(() => {
    // Release facts come from the generated JSON block in index.html. The
    // static block and its no-script links are rendered from the same manifest.
    const picker = document.querySelector('[data-platform-download]');
    const releaseData = picker?.querySelector('[data-release-data]');
    if (!picker || !releaseData) return;

    let release;
    try {
        release = JSON.parse(releaseData.textContent);
    } catch {
        return;
    }

    const { chooser, platforms } = release;
    if (!release.product || !release.repository || !release.channel || !release.version
        || !chooser || !platforms?.windows || !platforms?.macos || !platforms?.linux) {
        return;
    }

    const primary = picker.querySelector('[data-primary-download]');
    const primaryLabel = picker.querySelector('[data-primary-label]');
    const primaryDetail = picker.querySelector('[data-primary-detail]');
    const dialog = picker.querySelector('[data-platform-dialog]');
    const dialogOpen = picker.querySelector('[data-platform-dialog-open]');
    const dialogClose = picker.querySelector('[data-platform-dialog-close]');
    const macToggle = picker.querySelector('[data-mac-download-toggle]');
    const macOptions = picker.querySelector('[data-mac-download-options]');

    if (!primary || !primaryLabel || !primaryDetail || !dialog) return;

    const releaseUrl = artifact => (
        `${release.repository}/releases/download/v${release.version}/${artifact}`
    );
    const platformDownload = platform => ({
        label: platform.primary_label,
        detail: `${release.channel} ${release.version} · ${platform.detail}`,
        href: releaseUrl(platform.artifact),
    });
    const downloads = {
        windows: platformDownload(platforms.windows),
        linux: platformDownload(platforms.linux),
        macos: {
            label: platforms.macos.primary_label,
            detail: `${release.channel} ${release.version} · ${platforms.macos.detail}`,
            href: '#mac-download-options',
        },
        unknown: {
            label: chooser.unknown_primary_label,
            detail: `${release.product} ${release.channel} ${release.version}`,
            href: '#other-platforms',
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

    const openDialog = ({ mac = false } = {}) => {
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else dialog.setAttribute('open', '');
        document.body.classList.add('platform-dialog-open');
        setMacChoices(mac, { focus: mac });
    };

    const closeDialog = () => {
        if (typeof dialog.close === 'function' && dialog.open) dialog.close();
        else {
            dialog.removeAttribute('open');
            setMacChoices(false);
        }
        document.body.classList.remove('platform-dialog-open');
    };

    macToggle?.addEventListener('click', () => {
        const open = macToggle.getAttribute('aria-expanded') !== 'true';
        setMacChoices(open);
    });
    dialogOpen?.addEventListener('click', () => openDialog());
    dialogClose?.addEventListener('click', closeDialog);
    dialog.addEventListener('close', () => {
        document.body.classList.remove('platform-dialog-open');
        setMacChoices(false);
    });
    dialog.addEventListener('click', event => {
        if (event.target === dialog) closeDialog();
    });
    dialog.querySelectorAll('a').forEach(link => link.addEventListener('click', closeDialog));

    const platform = reportedPlatform();
    const selected = downloads[platform];
    primaryLabel.textContent = selected.label;
    primaryDetail.textContent = selected.detail;
    primary.href = selected.href;

    if (platform === 'macos') {
        primary.addEventListener('click', event => {
            event.preventDefault();
            openDialog({ mac: true });
        });
    } else if (platform === 'unknown') {
        primary.addEventListener('click', event => {
            event.preventDefault();
            openDialog();
        });
    }
})();
