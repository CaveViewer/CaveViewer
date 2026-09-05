(() => {
    const fallbackArchitecture = "arm64";

    const isMacBrowser = () => {
        const clientPlatform = navigator.userAgentData?.platform ?? "";
        const legacyPlatform = navigator.platform ?? "";
        const userAgent = navigator.userAgent ?? "";
        const reported = `${clientPlatform} ${legacyPlatform} ${userAgent}`.toLowerCase();
        const looksLikeIPad = /ipad|iphone|ipod/.test(reported)
            || (/mac/.test(reported) && navigator.maxTouchPoints > 1);

        return !looksLikeIPad && /macos|macintosh|macintel|macppc/.test(reported);
    };

    const detectedMacArchitecture = async () => {
        if (!isMacBrowser()) return fallbackArchitecture;

        try {
            const clientHints = await navigator.userAgentData?.getHighEntropyValues?.(
                ["architecture"],
            );
            const architecture = String(clientHints?.architecture ?? "").toLowerCase();

            if (/x86|intel/.test(architecture)) return "x86_64";
            if (/arm|aarch64/.test(architecture)) return "arm64";
        } catch {
            // Privacy-limited client hints retain the safe static default.
        }

        return fallbackArchitecture;
    };

    const setUpMacDownload = async control => {
        const download = control.querySelector("[data-macos-download-link]");
        const label = control.querySelector("[data-macos-download-label]");
        const selector = control.querySelector("[data-macos-architecture]");
        if (!download || !label || !selector) return;

        const optionFor = value => [...selector.options].find(option => option.value === value);
        const applyArchitecture = value => {
            const option = optionFor(value);
            if (!option?.dataset.downloadUrl || !option.dataset.downloadLabel
                || !option.dataset.downloadAriaLabel) return;

            selector.value = option.value;
            download.href = option.dataset.downloadUrl;
            label.textContent = option.dataset.downloadLabel;
            download.setAttribute("aria-label", option.dataset.downloadAriaLabel);
        };

        let manuallySelected = false;
        applyArchitecture(fallbackArchitecture);
        selector.addEventListener("change", () => {
            manuallySelected = true;
            applyArchitecture(selector.value);
        });

        const architecture = await detectedMacArchitecture();
        if (!manuallySelected) applyArchitecture(architecture);
    };

    document.querySelectorAll("[data-macos-download]").forEach(control => {
        void setUpMacDownload(control);
    });
})();
