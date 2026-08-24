(() => {
    const reveal = document.querySelectorAll('[data-reveal]');
    if ('IntersectionObserver' in window) {
        const observer = new IntersectionObserver(entries => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('is-visible');
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: .12, rootMargin: '0px 0px -5% 0px' });
        reveal.forEach(el => observer.observe(el));
    } else {
        reveal.forEach(el => el.classList.add('is-visible'));
    }

    const navigation = document.querySelector('.primary-nav');
    const menuToggle = document.querySelector('[data-menu-toggle]');

    if (navigation && menuToggle) {
        const setMenuOpen = isOpen => {
            navigation.classList.toggle('is-open', isOpen);
            menuToggle.setAttribute('aria-expanded', String(isOpen));
            menuToggle.setAttribute('aria-label', isOpen ? 'Close navigation' : 'Open navigation');
        };

        menuToggle.addEventListener('click', () => {
            setMenuOpen(!navigation.classList.contains('is-open'));
        });

        navigation.addEventListener('click', event => {
            if (event.target.closest('a')) {
                setMenuOpen(false);
            }
        });

        document.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                setMenuOpen(false);
            }
        });

        window.matchMedia('(min-width: 821px)').addEventListener('change', () => {
            setMenuOpen(false);
        });
    }
})();
