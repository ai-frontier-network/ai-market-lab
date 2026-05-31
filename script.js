document.addEventListener('DOMContentLoaded', () => {
    console.log("AI Frontier Lab - Market OS: System Initialized.");

    /* =========================================
       1. ダークモード自動＆手動切り替えロジック
       ========================================= */
    const themeToggleBtn = document.getElementById('theme-toggle');
    const moonIcon = document.getElementById('moon-icon');
    const sunIcon = document.getElementById('sun-icon');
    const currentTheme = localStorage.getItem('theme');
    
    if (currentTheme) {
        document.documentElement.setAttribute('data-theme', currentTheme);
        if (currentTheme === 'dark') {
            moonIcon.style.display = 'none';
            sunIcon.style.display = 'block';
        }
    } else {
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
            document.documentElement.setAttribute('data-theme', 'dark');
            moonIcon.style.display = 'none';
            sunIcon.style.display = 'block';
        }
    }
    
    themeToggleBtn.addEventListener('click', () => {
        let theme = document.documentElement.getAttribute('data-theme');
        if (theme === 'dark') {
            document.documentElement.removeAttribute('data-theme');
            localStorage.setItem('theme', 'light');
            sunIcon.style.display = 'none';
            moonIcon.style.display = 'block';
        } else {
            document.documentElement.setAttribute('data-theme', 'dark');
            localStorage.setItem('theme', 'dark');
            moonIcon.style.display = 'none';
            sunIcon.style.display = 'block';
        }
    });

    /* =========================================
       2. スクロールフェードイン アニメーション
       ========================================= */
    const fadeElements = document.querySelectorAll('.fade-element');

    const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
            if (entry.isIntersecting) {
                entry.target.classList.add('show');
            }
        });
    }, {
        threshold: 0.01
    });

    fadeElements.forEach((el) => {
        el.classList.add('hidden');
        observer.observe(el);
    });

    /* =========================================
       3. メンテナンスフリー型表示・非表示トグル（アコーディオン）
       ========================================= */
    const toggleButtons = document.querySelectorAll('.toggle-button');

    toggleButtons.forEach((button) => {
        button.addEventListener('click', () => {
            const content = button.nextElementSibling;
            
            if (content && content.classList.contains('toggle-content')) {
                content.classList.toggle('open');
                
                const isOpened = content.classList.contains('open');
                const openText = button.getAttribute('data-open-text') || '閉じる';
                const closeText = button.getAttribute('data-close-text') || '詳細を読む';
                
                button.textContent = isOpened ? openText : closeText;
            }
        });
    });
});
