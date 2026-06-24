// 共有 UI スクリプト（head から defer で一度だけ評価）。
// Turbo Drive 下では body が差し替わるため、要素参照をキャッシュせず
// document への委譲と Turbo イベントで扱う。

// ---- Sidebar toggle（委譲） --------------------------------
document.addEventListener('click', (e) => {
  const toggle = e.target.closest('#sidebarToggle');
  if (!toggle) return;
  const sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.classList.toggle('sidebar-open');
});

// ---- Loading overlay --------------------------------------
function showLoader() {
  const loader = document.getElementById('loader');
  if (loader) loader.style.display = 'flex';
}
function hideLoader() {
  const loader = document.getElementById('loader');
  if (loader) loader.style.display = 'none';
}

// Turbo ナビゲーション（リンク遷移）の開始/完了。
document.addEventListener('turbo:visit', showLoader);
document.addEventListener('turbo:load', hideLoader);

// data-turbo="false" のフォーム（ネイティブ送信＝フルリロード）向け。
document.addEventListener('submit', showLoader);
window.addEventListener('pageshow', hideLoader);
window.addEventListener('load', hideLoader);
