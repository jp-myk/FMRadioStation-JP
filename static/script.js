// ---- Sidebar toggle ----------------------------------------
const sidebarToggle = document.getElementById('sidebarToggle');
const sidebar = document.getElementById('sidebar');
if (sidebarToggle && sidebar) {
  sidebarToggle.addEventListener('click', () => {
    sidebar.classList.toggle('sidebar-open');
  });
}

// ---- Loading overlay ---------------------------------------
function showLoader() {
  const loader = document.getElementById('loader');
  if (loader) loader.style.display = 'flex';
}

function hideLoader() {
  const loader = document.getElementById('loader');
  if (loader) loader.style.display = 'none';
}

// ページ遷移時にローダー表示（外部リンクとアンカー除く）
document.addEventListener('click', (e) => {
  const link = e.target.closest('a[href]');
  if (!link) return;
  const href = link.getAttribute('href');
  if (!href || href.startsWith('#') || href.startsWith('javascript') || link.hasAttribute('download')) return;
  if (link.target === '_blank') return;
  showLoader();
});

// フォーム送信時にローダー表示
document.addEventListener('submit', () => showLoader());

// ページ読み込み完了時にローダー非表示
window.addEventListener('pageshow', hideLoader);
window.addEventListener('load', hideLoader);
