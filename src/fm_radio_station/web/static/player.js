// ============================================================
// 常駐ラジオプレーヤー（全ページで生存）
// ------------------------------------------------------------
// プレーヤー DOM（#playerDock）は layout.html に置き data-turbo-permanent で
// ページ遷移をまたいで保持される。本スクリプトは <head> から一度だけ評価され、
// 必要な関数を window に公開する（カードの onclick やサイドバーの再起動ボタン、
// 字幕トグルから呼ばれる）。ASR の状態/可否は /api/asr で自己初期化する。
// ============================================================
(function () {
  let _streamRequestId = 0;    // 各 playStream 呼び出しに一意なID
  let _activeStationId = null; // 現在アクティブなステーション（接続中含む）
  let _lastStation = null;     // 最後に再生したステーション情報（再起動用）
  let _transcriptTimer = null; // 字幕ポーリングの interval
  let _transcriptStation = null;
  let _asrCursor = 0;          // 取得済みセグメント数
  let _asrEnabled = false;     // 自動字幕 ON/OFF（/api/asr で同期）
  let _asrAvailable = false;   // 音声認識が利用可能か（モデル/CLI 有無）

  const byId = (id) => document.getElementById(id);

  function renderAsrToggle() {
    const wrap = byId('asrToggleWrap');
    const toggle = byId('asrToggle');
    const label = byId('asrToggleLabel');
    if (wrap) wrap.classList.toggle('d-none', !_asrAvailable);
    if (!toggle || !label) return;
    toggle.checked = _asrEnabled;
    label.textContent = _asrEnabled ? 'ON' : 'OFF';
    label.className = _asrEnabled ? 'text-info' : 'text-secondary';
  }

  function showAsrOff() {
    const panel = byId('transcriptPanel');
    const body = byId('transcriptBody');
    if (panel && !panel.classList.contains('d-none') && body) {
      body.innerHTML = '<div class="transcript-empty">自動字幕はオフです</div>';
    }
  }

  function toggleAsr(enabled) {
    const toggle = byId('asrToggle');
    const next = typeof enabled === 'boolean' ? enabled : !_asrEnabled;
    if (toggle) toggle.disabled = true;
    fetch('/api/asr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: next, station_id: _activeStationId }),
    })
      .then((r) => r.json())
      .then((data) => {
        _asrEnabled = data.enabled;
        renderAsrToggle();
        if (_activeStationId) {
          if (_asrEnabled) startTranscript(_activeStationId);
          else { stopTranscript(); showAsrOff(); }
        }
      })
      .catch(() => { renderAsrToggle(); })
      .finally(() => { if (toggle) toggle.disabled = false; });
  }

  function syncAsrState() {
    // 別タブでの変更等に追従して状態/可否を同期する。
    fetch('/api/asr')
      .then((r) => r.json())
      .then((data) => {
        _asrEnabled = !!data.enabled;
        _asrAvailable = !!data.available;
        renderAsrToggle();
      })
      .catch(() => {});
  }

  function startTranscript(stationId) {
    if (!_asrEnabled || !_asrAvailable) return;
    if (_transcriptTimer && _transcriptStation === stationId) return;
    stopTranscript();
    _transcriptStation = stationId;
    _asrCursor = 0;
    const panel = byId('transcriptPanel');
    const body = byId('transcriptBody');
    if (!panel || !body) return;
    body.innerHTML = '<div class="transcript-empty">認識待機中...</div>';
    panel.classList.remove('d-none');
    const myRequestId = _streamRequestId;
    let unavailableShown = false;
    let hasText = false;

    const poll = () => {
      if (myRequestId !== _streamRequestId) { stopTranscript(); return; }
      fetch('/api/transcript/' + stationId + '?since=' + _asrCursor)
        .then((r) => r.json())
        .then((data) => {
          if (myRequestId !== _streamRequestId) return;
          if (data.enabled === false) {
            if (!unavailableShown) { showAsrOff(); unavailableShown = true; }
            return;
          }
          if (data.available === false) {
            if (!unavailableShown) {
              body.innerHTML = '<div class="transcript-empty">音声認識は利用できません</div>';
              unavailableShown = true;
            }
            return;
          }
          unavailableShown = false;
          if (data.segments && data.segments.length) {
            if (!hasText) { body.innerHTML = ''; hasText = true; }
            for (const s of data.segments) {
              const line = document.createElement('div');
              line.className = 'transcript-line';
              line.textContent = s.text;
              body.appendChild(line);
            }
            _asrCursor = data.cursor;
            body.scrollTop = body.scrollHeight;
          }
        })
        .catch(() => {});
    };
    poll();
    _transcriptTimer = setInterval(poll, 1500);
  }

  function stopTranscript() {
    if (_transcriptTimer) { clearInterval(_transcriptTimer); _transcriptTimer = null; }
    _transcriptStation = null;
  }

  function playStreamFromCard(cardEl, btnEl) {
    const d = cardEl.dataset;
    playStream(d.stationId, d.stationName, d.title, d.isRecording === 'true', btnEl);
  }

  function playStream(stationId, stationName, title, isRecording, btnEl) {
    const myRequestId = ++_streamRequestId;
    const wasActive = _activeStationId !== null;
    _activeStationId = stationId;
    _lastStation = { id: stationId, name: stationName, title, isRecording };

    const audio = byId('streamAudio');
    const bar = byId('nowPlayingBar');
    const spinner = byId('connectingSpinner');
    const statusText = byId('connectingStatus');
    const errMsg = byId('streamError');
    const badge = byId('nowPlayingBadge');

    if (!audio.paused) { audio.pause(); }
    if (window._streamTimeout) { clearTimeout(window._streamTimeout); window._streamTimeout = null; }
    audio.onerror = null;
    audio.oncanplay = null;
    audio.onplaying = null;
    audio.onwaiting = null;
    audio.onstalled = null;
    audio.removeAttribute('src');
    audio.load();

    const playLabel = isRecording ? '録音から再生' : '再生';
    if (btnEl) {
      btnEl.disabled = true;
      btnEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>接続中...';
    }
    function resetBtn() {
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.innerHTML = '<i class="bi bi-play-fill me-1"></i>' + playLabel;
      }
    }

    function showError(message) {
      if (myRequestId !== _streamRequestId) return;
      clearTimeout(window._streamTimeout);
      window._streamTimeout = null;
      spinner.classList.add('d-none');
      statusText.textContent = '';
      bar.classList.remove('bar-connecting');
      errMsg.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + message;
      errMsg.className = 'text-danger small';
      _activeStationId = null;
      stopTranscript();
      byId('transcriptPanel').classList.add('d-none');
      resetBtn();
      window._streamTimeout = setTimeout(() => {
        bar.classList.add('d-none');
        window._streamTimeout = null;
      }, 4000);
    }

    function onConnected() {
      if (myRequestId !== _streamRequestId) return;
      if (window._streamTimeout) { clearTimeout(window._streamTimeout); window._streamTimeout = null; }
      spinner.classList.add('d-none');
      statusText.textContent = '';
      bar.classList.remove('bar-connecting');
      errMsg.className = 'd-none';
      resetBtn();
      // 再生中は字幕枠を常に表示し、トグルへ常に到達できるようにする
      // （ASR OFF で再生開始しても ON に戻せる）。
      byId('transcriptPanel').classList.remove('d-none');
      if (_asrEnabled) startTranscript(stationId);
      else showAsrOff();
    }

    byId('nowPlayingTitle').textContent = title;
    byId('nowPlayingStation').textContent = stationName;
    badge.innerHTML = isRecording
      ? '<i class="bi bi-record-fill"></i> REC'
      : '<i class="bi bi-broadcast-pin"></i> LIVE';
    errMsg.className = 'd-none';
    errMsg.innerHTML = '';
    spinner.classList.remove('d-none');
    statusText.textContent = 'ストリーム確認中...';
    bar.classList.remove('d-none');
    bar.classList.add('bar-connecting');

    const startFetch = () => {
      if (myRequestId !== _streamRequestId) return;
      fetch('/api/stream-status/' + stationId)
        .then((r) => r.json())
        .then((status) => {
          if (myRequestId !== _streamRequestId) return;
          if (!status.available) { showError(status.reason); return; }
          statusText.textContent = '接続中...';
          window._streamTimeout = setTimeout(() => {
            if (myRequestId !== _streamRequestId) return;
            audio.pause();
            audio.removeAttribute('src');
            showError('接続タイムアウト（ストリーミングが開始されませんでした）');
          }, 60000);

          audio.onerror = function () {
            if (myRequestId !== _streamRequestId) return;
            const code = audio.error ? audio.error.code : -1;
            const msgs = {
              [MediaError.MEDIA_ERR_NETWORK]: 'ネットワークエラー（SDR 未接続の可能性）',
              [MediaError.MEDIA_ERR_DECODE]: '音声デコードエラー',
              [MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED]: '音声形式未対応',
            };
            showError(msgs[code] || 'ストリーミングエラー (code=' + code + ')');
          };
          audio.onwaiting = function () {
            if (myRequestId !== _streamRequestId) return;
            spinner.classList.remove('d-none');
            statusText.textContent = 'バッファリング中...';
          };
          audio.onstalled = function () {
            if (myRequestId !== _streamRequestId) return;
            spinner.classList.remove('d-none');
            statusText.textContent = '接続が遅延しています...';
          };
          audio.oncanplay = function () { onConnected(); };
          audio.onplaying = function () { onConnected(); };

          audio.src = '/stream/' + stationId + '?asr=' + (_asrEnabled ? '1' : '0');
          const playPromise = audio.play();
          if (playPromise !== undefined) {
            playPromise.catch((err) => {
              if (err.name === 'AbortError') return;
              if (myRequestId !== _streamRequestId) return;
              showError('再生エラー: ' + err.message);
            });
          }
        })
        .catch((err) => {
          if (myRequestId !== _streamRequestId) return;
          showError(err.message);
        });
    };

    if (wasActive) {
      fetch('/api/stop-stream', { method: 'POST' }).finally(() => startFetch());
    } else {
      startFetch();
    }
  }

  function stopStream() {
    ++_streamRequestId;
    _activeStationId = null;
    _lastStation = null;
    const audio = byId('streamAudio');
    if (window._streamTimeout) { clearTimeout(window._streamTimeout); window._streamTimeout = null; }
    audio.onerror = null;
    audio.oncanplay = null;
    audio.onplaying = null;
    audio.onwaiting = null;
    audio.onstalled = null;
    audio.pause();
    audio.src = '';
    const bar = byId('nowPlayingBar');
    bar.classList.add('d-none');
    bar.classList.remove('bar-connecting');
    byId('connectingStatus').textContent = '';
    stopTranscript();
    fetch('/api/stop-stream', { method: 'POST' }).catch(() => {});
  }

  function restartStream() {
    if (!_lastStation) {
      fetch('/api/stop-stream', { method: 'POST' }).catch(() => {});
      return;
    }
    const { id, name, title, isRecording } = _lastStation;
    fetch('/api/stop-stream', { method: 'POST' })
      .finally(() => playStream(id, name, title, isRecording, null));
  }

  // 固定 dock の高さ分だけ本文の下に余白を確保し、最終行のカードが dock に隠れない
  // ようにする。dock の高さは状態（空／バーのみ／バー＋字幕）で変わるため動的に追従する。
  function applyDockPadding() {
    const dock = byId('playerDock');
    const main = byId('mainContent');
    if (dock && main) main.style.paddingBottom = dock.offsetHeight + 'px';
  }

  // ---- 公開（inline onclick / 他ページ / サイドバーから呼ぶ） ----
  window.playStream = playStream;
  window.playStreamFromCard = playStreamFromCard;
  window.stopStream = stopStream;
  window.restartStream = restartStream;
  window.toggleAsr = toggleAsr;
  window.syncAsrState = syncAsrState;

  // 初期化（初回・Turbo 遷移ごとにトグル状態を同期＋本文余白を再計算）。dock は
  // permanent なのでハンドラの再バインドは不要だが、mainContent は遷移で差し替わる。
  function init() { syncAsrState(); applyDockPadding(); }
  document.addEventListener('turbo:load', init);
  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);

  // dock の高さ変化（再生開始/停止・字幕表示・ウィンドウ幅変更）に追従。
  if (window.ResizeObserver) {
    const dock = byId('playerDock');
    if (dock) new ResizeObserver(applyDockPadding).observe(dock);
  }
  window.addEventListener('resize', applyDockPadding);
})();
