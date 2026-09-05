(() => {
    const reveal = document.querySelectorAll('[data-reveal]');
    const prefersReducedMotion = window.matchMedia?.(
        '(prefers-reduced-motion: reduce)',
    ).matches ?? false;

    if (!prefersReducedMotion && 'IntersectionObserver' in window) {
        const observer = new IntersectionObserver(entries => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('is-visible');
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: .12, rootMargin: '0px 0px -5% 0px' });
        reveal.forEach(el => observer.observe(el));
        document.documentElement.classList.add('reveal-enhanced');
    } else {
        reveal.forEach(el => el.classList.add('is-visible'));
    }

    const navigation = document.querySelector('.primary-nav');
    const menuToggle = document.querySelector('[data-menu-toggle]');

    if (navigation && menuToggle) {
        const dropdowns = [...navigation.querySelectorAll('.primary-nav__dropdown')];
        const firstNavigationControl = navigation.querySelector('summary, a');
        const closeDropdowns = exception => {
            dropdowns.forEach(dropdown => {
                if (dropdown !== exception) {
                    dropdown.removeAttribute('open');
                }
            });
        };

        dropdowns.forEach(dropdown => {
            dropdown.addEventListener('toggle', () => {
                if (dropdown.open) {
                    closeDropdowns(dropdown);
                }
            });
        });

        const setMenuOpen = (isOpen, { restoreFocus = false } = {}) => {
            const wasOpen = navigation.classList.contains('is-open');

            navigation.classList.toggle('is-open', isOpen);
            menuToggle.setAttribute('aria-expanded', String(isOpen));
            menuToggle.setAttribute('aria-label', isOpen ? 'Close navigation' : 'Open navigation');

            // Opening begins at the first destination; closing by keyboard returns
            // to the control. Normal Tab order remains unrestricted inside the menu.
            if (isOpen && !wasOpen) {
                // The first frame applies the menu's visible state; focus in the
                // following frame so browsers do not reject a hidden link.
                window.requestAnimationFrame(() => {
                    window.requestAnimationFrame(() => {
                        if (navigation.classList.contains('is-open')) {
                            firstNavigationControl?.focus();
                        }
                    });
                });
            } else if (!isOpen && wasOpen && restoreFocus) {
                menuToggle.focus();
            }
        };

        menuToggle.addEventListener('click', () => {
            const isOpen = navigation.classList.contains('is-open');
            setMenuOpen(!isOpen, { restoreFocus: isOpen });
        });

        navigation.addEventListener('click', event => {
            if (event.target.closest('a')) {
                closeDropdowns();
                setMenuOpen(false);
            }
        });

        document.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                closeDropdowns();
                if (navigation.classList.contains('is-open')) {
                    setMenuOpen(false, { restoreFocus: true });
                }
            }
        });

        document.addEventListener('click', event => {
            if (!event.target.closest('.primary-nav__dropdown')) {
                closeDropdowns();
            }
        });

        window.matchMedia('(min-width: 961px)').addEventListener('change', event => {
            if (event.matches) {
                setMenuOpen(false);
            }
        });
    }

})();
