/* 远程桌面控制台前端逻辑（增强版）
 * 功能：增量帧渲染、剪贴板同步、文件传输、画质自适应、
 *       多显示器切换、截图、连接质量指示、移动端触摸支持
 */
(function () {
  'use strict';

  const canvas = document.getElementById('screen');
  const ctx = canvas.getContext('2d', { alpha: false });
  const statusEl = document.getElementById('status');
  const fpsEl = document.getElementById('fps');
  const resEl = document.getElementById('resolution');
  const latencyEl = document.getElementById('latency');
  const qualityEl = document.getElementById('quality-badge');
  const bandwidthEl = document.getElementById('bandwidth');
  const overlay = document.getElementById('overlay');
  const container = document.getElementById('screenContainer');

  let ws = null;
  let screenW = 0, screenH = 0;
  let frameCount = 0;
  let bytesPerSec = 0;
  let connected = false;
  let reconnectTimer = null;
  let lastFrameTime = 0;
  let frameLatency = 0;

  // 音频播放
  let audioCtx = null;
  let audioEnabled = false;
  let audioRate = 22050;

  // 画质自适应
  let pingTime = 0;
  let pingLatency = 0;
  let latencyHistory = [];
  let currentQuality = 55;
  let currentFps = 15;
  let autoQuality = true;

  // ---------- WebSocket 连接 ----------
  function wsUrl() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    if (MODE === 'relay') {
      const did = encodeURIComponent(DESKTOP_ID || 'default');
      return `${scheme}://${location.host}/ws/client?token=${TOKEN}&desktop_id=${did}`;
    }
    return `${scheme}://${location.host}/ws?token=${TOKEN}`;
  }

  function connect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    setStatus('连接中...', 'waiting');
    try { ws = new WebSocket(wsUrl()); } catch (e) { scheduleReconnect(); return; }
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      connected = true;
      setStatus('已连接', 'connected');
      overlay.classList.add('hidden');
      // 启动 ping
      startPing();
    };
    ws.onmessage = onMessage;
    ws.onclose = () => {
      connected = false;
      setStatus('已断开，重连中...', 'disconnected');
      showOverlay('连接已断开，正在重连...');
      scheduleReconnect();
    };
    ws.onerror = () => {};
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 2000);
  }

  function sendCmd(cmd) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(cmd));
  }

  // ---------- Ping / 延迟测量 / 画质自适应 ----------
  function startPing() {
    if (pingInterval) clearInterval(pingInterval);
    pingInterval = setInterval(() => {
      if (!connected) return;
      pingTime = performance.now();
      sendCmd({ type: 'ping', t: pingTime });
    }, 2000);
  }
  let pingInterval = null;

  function autoAdjustQuality() {
    if (!autoQuality) return;
    // 根据平均延迟调整画质
    const avgLat = latencyHistory.reduce((a, b) => a + b, 0) / Math.max(1, latencyHistory.length);
    let newQ = currentQuality, newF = currentFps;
    if (avgLat > 300) { newQ = 25; newF = 5; }       // 很差
    else if (avgLat > 150) { newQ = 35; newF = 8; }   // 差
    else if (avgLat > 80) { newQ = 45; newF = 12; }   // 一般
    else { newQ = 55; newF = 15; }                     // 好
    if (newQ !== currentQuality || newF !== currentFps) {
      currentQuality = newQ;
      currentFps = newF;
      sendCmd({ type: 'set_quality', quality: newQ });
      sendCmd({ type: 'set_fps', fps: newF });
    }
  }

  // ---------- 消息处理 ----------
  async function onMessage(e) {
    if (typeof e.data === 'string') {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (msg.type === 'init') {
        screenW = msg.width;
        screenH = msg.height;
        canvas.width = screenW;
        canvas.height = screenH;
        resEl.textContent = `${screenW}×${screenH}`;
        ctx.fillStyle = '#111';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        resizeCanvas();
        // 多显示器列表
        if (msg.monitors && msg.monitors.length > 1) {
          const sel = document.getElementById('monitorSelect');
          sel.innerHTML = msg.monitors.map(m =>
            `<option value="${m.index}">显示器 ${m.index} (${m.width}×${m.height})</option>`).join('');
          sel.style.display = 'inline-block';
        }
        // 音频信息
        if (msg.audio_supported) {
          audioRate = msg.audio_rate || 22050;
          document.getElementById('audioBtn').classList.remove('btn-muted');
        }
      } else if (msg.type === 'agent_connected') {
        setStatus('已连接', 'connected');
        overlay.classList.add('hidden');
      } else if (msg.type === 'agent_disconnected') {
        setStatus('桌面离线', 'waiting');
        showOverlay(msg.message || '桌面未连接，等待代理上线...');
      } else if (msg.type === 'pong') {
        pingLatency = Math.round(performance.now() - msg.t);
        latencyHistory.push(pingLatency);
        if (latencyHistory.length > 10) latencyHistory.shift();
        autoAdjustQuality();
      } else if (msg.type === 'clipboard_data') {
        // 收到被控端剪贴板内容，写入本地剪贴板
        if (msg.text) {
          try { await navigator.clipboard.writeText(msg.text); } catch (_) {}
        }
      } else if (msg.type === 'file_progress') {
        updateFileProgress(msg.percent, msg.name, msg.received, msg.size);
      } else if (msg.type === 'file_done') {
        finishFileProgress(msg.name, msg.path);
      }
      return;
    }
    // 二进制消息：音频或画面帧
    try {
      const data = e.data;
      const u8 = new Uint8Array(data);
      // 检查是否是音频数据（以 'AUDI' 标记开头）
      if (u8.length > 4 && u8[0] === 0x41 && u8[1] === 0x55 &&
          u8[2] === 0x44 && u8[3] === 0x49) {
        if (audioEnabled) playAudio(data, 4);
        return;
      }
      // 画面增量帧
      const headerLen = new DataView(data, 0, 4).getUint32(0, false);
      const headerBytes = new Uint8Array(data, 4, headerLen);
      const header = JSON.parse(new TextDecoder().decode(headerBytes));
      const tilesDataStart = 4 + headerLen;
      const tilesData = new Uint8Array(data, tilesDataStart);

      if (header.tiles && header.tiles.length > 0) {
        const drawPromises = header.tiles.map(async (tile) => {
          const tileBytes = tilesData.subarray(tile.offset, tile.offset + tile.length);
          const bitmap = await createImageBitmap(
            new Blob([tileBytes], { type: 'image/jpeg' })
          );
          ctx.drawImage(bitmap, tile.x, tile.y, tile.w, tile.h);
          bitmap.close();
        });
        await Promise.all(drawPromises);
      }
      frameCount++;
      bytesPerSec += e.data.byteLength;
      frameLatency = Math.round(performance.now() - lastFrameTime);
    } catch (err) {}
    lastFrameTime = performance.now();
  }

  // ---------- 画面尺寸适配 ----------
  function resizeCanvas() {
    if (!screenW || !screenH) return;
    const ratio = screenW / screenH;
    const maxW = container.clientWidth - 4;
    const maxH = container.clientHeight - 4;
    let w = maxW, h = maxW / ratio;
    if (h > maxH) { h = maxH; w = maxH * ratio; }
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
  }
  window.addEventListener('resize', resizeCanvas);

  // ---------- 状态显示 ----------
  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = 'status ' + cls;
  }
  function showOverlay(text) {
    overlay.textContent = text;
    overlay.classList.remove('hidden');
  }

  // FPS / 延迟 / 流量 / 画质统计
  setInterval(() => {
    fpsEl.textContent = frameCount + ' FPS';
    latencyEl.textContent = pingLatency ? pingLatency + 'ms' : '';
    // 画质/连接质量指示
    if (pingLatency > 0) {
      let qText, qClass;
      if (pingLatency > 300) { qText = '差'; qClass = 'q-bad'; }
      else if (pingLatency > 150) { qText = '一般'; qClass = 'q-mid'; }
      else if (pingLatency > 80) { qText = '良好'; qClass = 'q-ok'; }
      else { qText = '优秀'; qClass = 'q-good'; }
      qualityEl.textContent = qText + ' Q' + currentQuality;
      qualityEl.className = 'badge ' + qClass;
    }
    // 流量
    if (bytesPerSec > 1024 * 1024) bandwidthEl.textContent = (bytesPerSec / 1024 / 1024).toFixed(1) + ' MB/s';
    else if (bytesPerSec > 0) bandwidthEl.textContent = (bytesPerSec / 1024).toFixed(0) + ' KB/s';
    else bandwidthEl.textContent = '';
    frameCount = 0;
    bytesPerSec = 0;
  }, 1000);

  // ---------- 鼠标坐标映射 ----------
  function getCoords(e) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
      x: Math.round((e.clientX - rect.left) * scaleX),
      y: Math.round((e.clientY - rect.top) * scaleY),
    };
  }
  function buttonName(btn) { return ['left', 'middle', 'right'][btn] || 'left'; }

  // ---------- 鼠标事件 ----------
  let lastMoveTime = 0;
  canvas.addEventListener('mousemove', (e) => {
    const now = performance.now();
    if (now - lastMoveTime < 14) return;
    lastMoveTime = now;
    const { x, y } = getCoords(e);
    sendCmd({ type: 'mouse_move', x, y });
  });
  canvas.addEventListener('mousedown', (e) => {
    const { x, y } = getCoords(e);
    sendCmd({ type: 'mouse_down', x, y, button: buttonName(e.button) });
    e.preventDefault();
  });
  canvas.addEventListener('mouseup', (e) => {
    const { x, y } = getCoords(e);
    sendCmd({ type: 'mouse_up', x, y, button: buttonName(e.button) });
    e.preventDefault();
  });
  canvas.addEventListener('contextmenu', (e) => e.preventDefault());
  canvas.addEventListener('dblclick', (e) => {
    const { x, y } = getCoords(e);
    sendCmd({ type: 'mouse_double', x, y });
  });
  canvas.addEventListener('wheel', (e) => {
    const { x, y } = getCoords(e);
    sendCmd({ type: 'mouse_scroll', x, y, delta: -Math.sign(e.deltaY) * 120 });
    e.preventDefault();
  }, { passive: false });

  // ---------- 移动端触摸支持（增强版） ----------
  let touchState = { mode: 'none', startX: 0, startY: 0, lastX: 0, lastY: 0,
                      startDist: 0, longPressTimer: null, moved: false };

  function touchCoords(t) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return { x: Math.round((t.clientX - rect.left) * scaleX),
             y: Math.round((t.clientY - rect.top) * scaleY) };
  }

  canvas.addEventListener('touchstart', (e) => {
    e.preventDefault();
    const touches = e.touches;
    touchState.moved = false;
    if (touches.length === 1) {
      // 单指：移动 + 点击
      const c = touchCoords(touches[0]);
      touchState.mode = 'move';
      touchState.startX = c.x; touchState.startY = c.y;
      touchState.lastX = c.x; touchState.lastY = c.y;
      sendCmd({ type: 'mouse_move', x: c.x, y: c.y });
      // 长按检测 → 右键
      touchState.longPressTimer = setTimeout(() => {
        if (!touchState.moved) {
          sendCmd({ type: 'mouse_down', x: touchState.lastX, y: touchState.lastY, button: 'right' });
          sendCmd({ type: 'mouse_up', x: touchState.lastX, y: touchState.lastY, button: 'right' });
          touchState.mode = 'none';
        }
      }, 600);
    } else if (touches.length === 2) {
      // 双指：滚动或缩放
      touchState.mode = 'scroll';
      clearTimeout(touchState.longPressTimer);
      const dx = touches[0].clientX - touches[1].clientX;
      const dy = touches[0].clientY - touches[1].clientY;
      touchState.startDist = Math.hypot(dx, dy);
      touchState.lastY = (touches[0].clientY + touches[1].clientY) / 2;
    }
  }, { passive: false });

  canvas.addEventListener('touchmove', (e) => {
    e.preventDefault();
    const touches = e.touches;
    if (touches.length === 1 && touchState.mode === 'move') {
      const c = touchCoords(touches[0]);
      if (Math.abs(c.x - touchState.startX) > 5 || Math.abs(c.y - touchState.startY) > 5) {
        touchState.moved = true;
        clearTimeout(touchState.longPressTimer);
      }
      sendCmd({ type: 'mouse_move', x: c.x, y: c.y });
      touchState.lastX = c.x; touchState.lastY = c.y;
    } else if (touches.length === 2 && touchState.mode === 'scroll') {
      const midY = (touches[0].clientY + touches[1].clientY) / 2;
      const dy = Math.round(midY - touchState.lastY);
      if (Math.abs(dy) > 3) {
        sendCmd({ type: 'mouse_scroll', x: touchState.lastX, y: touchState.lastY, delta: -dy * 5 });
        touchState.lastY = midY;
      }
    }
  }, { passive: false });

  canvas.addEventListener('touchend', (e) => {
    e.preventDefault();
    clearTimeout(touchState.longPressTimer);
    if (touchState.mode === 'move' && !touchState.moved) {
      // 轻触 = 左键点击
      sendCmd({ type: 'mouse_down', x: touchState.lastX, y: touchState.lastY, button: 'left' });
      sendCmd({ type: 'mouse_up', x: touchState.lastX, y: touchState.lastY, button: 'left' });
    } else if (touchState.mode === 'move') {
      sendCmd({ type: 'mouse_up', button: 'left' });
    }
    touchState.mode = 'none';
  }, { passive: false });

  // ---------- 键盘事件 ----------
  const BLOCK_DEFAULT_KEYS = new Set([
    'Tab', 'Backspace', ' ', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
    'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
    'Home', 'End', 'PageUp', 'PageDown',
  ]);
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && (e.key === 'r' || e.key === 'R')) return;
    if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) return;
    if (e.key === 'F12' && !e.ctrlKey) return;
    if (e.ctrlKey && e.shiftKey && (e.key === 'I' || e.key === 'i')) return;
    sendCmd({ type: 'key_down', key: e.key, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey });
    if (BLOCK_DEFAULT_KEYS.has(e.key)) e.preventDefault();
  });
  document.addEventListener('keyup', (e) => {
    sendCmd({ type: 'key_up', key: e.key, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey });
    if (BLOCK_DEFAULT_KEYS.has(e.key)) e.preventDefault();
  });

  // 粘贴文字输入框
  const textPaste = document.getElementById('textPaste');
  textPaste.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const text = textPaste.value;
      if (text) { sendCmd({ type: 'type_text', text: text }); textPaste.value = ''; }
      e.preventDefault();
    }
    e.stopPropagation();
  });

  // ---------- 剪贴板同步 ----------
  document.getElementById('clipboardBtn').addEventListener('click', async () => {
    // 先获取被控端剪贴板 → 写入本地
    sendCmd({ type: 'get_clipboard' });
    // 短暂延迟后，把本地剪贴板发给被控端
    setTimeout(async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) sendCmd({ type: 'set_clipboard', text: text });
      } catch (_) {}
    }, 500);
  });

  // Ctrl+C / Ctrl+V 时自动同步
  document.addEventListener('copy', () => {
    try {
      navigator.clipboard.readText().then(text => {
        if (text) sendCmd({ type: 'set_clipboard', text: text });
      }).catch(() => {});
    } catch (_) {}
  });

  // ---------- 文件传输 ----------
  const fileInput = document.getElementById('fileInput');
  const fileProgEl = document.getElementById('fileProgress');
  const fileProgBar = document.getElementById('fileProgressBar');
  const fileProgName = document.getElementById('fileProgressName');
  const fileProgPct = document.getElementById('fileProgressPercent');
  let fileTransferring = false;

  document.getElementById('fileBtn').addEventListener('click', () => {
    if (fileTransferring) return;
    fileInput.click();
  });

  fileInput.addEventListener('change', async () => {
    const files = fileInput.files;
    if (!files || files.length === 0) return;
    for (const file of files) {
      await sendFile(file);
    }
    fileInput.value = '';
  });

  // 拖拽上传
  canvas.addEventListener('dragover', (e) => { e.preventDefault(); });
  canvas.addEventListener('drop', async (e) => {
    e.preventDefault();
    if (fileTransferring) return;
    const files = e.dataTransfer.files;
    if (!files || files.length === 0) return;
    for (const file of files) { await sendFile(file); }
  });

  async function sendFile(file) {
    const CHUNK_SIZE = 64 * 1024; // 64KB chunks
    fileTransferring = true;
    fileProgEl.style.display = 'block';
    fileProgName.textContent = file.name;
    fileProgPct.textContent = '0%';
    fileProgBar.style.width = '0%';

    // 通知 agent 开始接收
    sendCmd({ type: 'file_start', name: file.name, size: file.size });

    let offset = 0;
    while (offset < file.size) {
      const slice = file.slice(offset, offset + CHUNK_SIZE);
      const buf = await slice.arrayBuffer();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(buf);
      } else {
        sendCmd({ type: 'file_cancel' });
        break;
      }
      offset += buf.byteLength;
      // 等 agent 确认进度（避免发太快）
      await new Promise(r => setTimeout(r, 30));
    }
    // 等待 file_done 确认（超时 10 秒自动关闭进度条）
    setTimeout(() => {
      if (fileTransferring) { fileProgEl.style.display = 'none'; fileTransferring = false; }
    }, 10000);
  }

  function updateFileProgress(pct, name, received, size) {
    fileProgBar.style.width = Math.min(100, pct) + '%';
    fileProgPct.textContent = Math.min(100, pct) + '%';
    if (name) fileProgName.textContent = name;
  }
  function finishFileProgress(name, path) {
    fileProgPct.textContent = '完成!';
    fileProgBar.style.width = '100%';
    setTimeout(() => { fileProgEl.style.display = 'none'; }, 2000);
    fileTransferring = false;
  }

  // ---------- 截图 ----------
  document.getElementById('screenshotBtn').addEventListener('click', () => {
    const link = document.createElement('a');
    link.download = 'screenshot_' + Date.now() + '.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
  });

  // ---------- 声音播放 ----------
  function playAudio(buffer, offset) {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    // 如果 AudioContext 被挂起，恢复它
    if (audioCtx.state === 'suspended') audioCtx.resume();

    const pcm = new Float32Array(buffer, offset);
    if (pcm.length === 0) return;

    const audioBuffer = audioCtx.createBuffer(1, pcm.length, audioRate);
    audioBuffer.getChannelData(0).set(pcm);
    const source = audioCtx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(audioCtx.destination);
    source.start();
  }

  document.getElementById('audioBtn').addEventListener('click', () => {
    audioEnabled = !audioEnabled;
    const btn = document.getElementById('audioBtn');
    if (audioEnabled) {
      btn.textContent = '🔊';
      btn.classList.remove('btn-muted');
      // 首次点击需要用户交互来创建 AudioContext
      if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (audioCtx.state === 'suspended') audioCtx.resume();
    } else {
      btn.textContent = '🔇';
      btn.classList.add('btn-muted');
    }
  });

  // ---------- 多显示器切换 ----------
  document.getElementById('monitorSelect').addEventListener('change', (e) => {
    sendCmd({ type: 'switch_monitor', index: parseInt(e.target.value) });
  });

  // ---------- 虚拟键盘 ----------
  const kbdPanel = document.getElementById('kbdPanel');
  document.getElementById('keyboardBtn').addEventListener('click', () => {
    kbdPanel.style.display = kbdPanel.style.display === 'none' ? 'block' : 'none';
  });
  kbdPanel.addEventListener('click', (e) => {
    const btn = e.target.closest('.kbd-key');
    if (!btn) return;
    const key = btn.dataset.key;
    sendCmd({ type: 'key_down', key: key });
    setTimeout(() => sendCmd({ type: 'key_up', key: key }), 60);
  });

  // ---------- 工具栏按钮 ----------
  document.getElementById('reconnectBtn').addEventListener('click', () => {
    if (ws) { try { ws.close(); } catch (_) {} }
    connect();
  });
  document.getElementById('fullscreenBtn').addEventListener('click', () => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => {});
    else document.exitFullscreen();
  });

  // 启动连接
  connect();
})();
