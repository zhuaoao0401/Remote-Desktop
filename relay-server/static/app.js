/* 远程桌面控制台前端逻辑 */
(function () {
  'use strict';

  const canvas = document.getElementById('screen');
  const ctx = canvas.getContext('2d', { alpha: false });
  const statusEl = document.getElementById('status');
  const fpsEl = document.getElementById('fps');
  const resEl = document.getElementById('resolution');
  const latencyEl = document.getElementById('latency');
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
    try {
      ws = new WebSocket(wsUrl());
    } catch (e) {
      scheduleReconnect();
      return;
    }
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      connected = true;
      setStatus('已连接', 'connected');
      overlay.classList.add('hidden');
    };

    ws.onmessage = onMessage;

    ws.onclose = () => {
      connected = false;
      setStatus('已断开，重连中...', 'disconnected');
      showOverlay('连接已断开，正在重连...');
      scheduleReconnect();
    };

    ws.onerror = () => { /* onclose 会处理 */ };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 2000);
  }

  function sendCmd(cmd) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(cmd));
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
        // 清空画布，准备接收关键帧
        ctx.fillStyle = '#111';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        resizeCanvas();
      } else if (msg.type === 'agent_connected') {
        setStatus('已连接', 'connected');
        overlay.classList.add('hidden');
      } else if (msg.type === 'agent_disconnected') {
        setStatus('桌面离线', 'waiting');
        showOverlay(msg.message || '桌面未连接，等待代理上线...');
      }
      return;
    }
    // 二进制增量帧：解析打包格式并局部重绘
    try {
      const data = e.data;
      // 读取 4 字节头长度（大端）
      const headerLen = new DataView(data, 0, 4).getUint32(0, false);
      // 解析 JSON 头
      const headerBytes = new Uint8Array(data, 4, headerLen);
      const header = JSON.parse(new TextDecoder().decode(headerBytes));
      // 块数据起始偏移
      const tilesDataStart = 4 + headerLen;
      const tilesData = new Uint8Array(data, tilesDataStart);

      // 处理每个变化的块
      if (header.tiles && header.tiles.length > 0) {
        // 并行解码所有块
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
    } catch (err) {
      // 解码失败，忽略此帧
    }
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

  // FPS / 延迟 / 流量统计
  setInterval(() => {
    fpsEl.textContent = frameCount + ' FPS';
    latencyEl.textContent = frameLatency ? frameLatency + 'ms' : '';
    // 流量显示：转换为 KB/s 或 MB/s
    if (bytesPerSec > 1024 * 1024) {
      bandwidthEl.textContent = (bytesPerSec / 1024 / 1024).toFixed(1) + ' MB/s';
    } else if (bytesPerSec > 0) {
      bandwidthEl.textContent = (bytesPerSec / 1024).toFixed(0) + ' KB/s';
    } else {
      bandwidthEl.textContent = '';
    }
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
  function buttonName(btn) {
    return ['left', 'middle', 'right'][btn] || 'left';
  }

  // ---------- 鼠标事件 ----------
  let lastMoveTime = 0;
  canvas.addEventListener('mousemove', (e) => {
    const now = performance.now();
    if (now - lastMoveTime < 14) return; // 限流 ~70fps
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

  // 触摸支持（基础）
  canvas.addEventListener('touchstart', (e) => {
    if (e.touches.length === 1) {
      const t = e.touches[0];
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
      const x = Math.round((t.clientX - rect.left) * scaleX);
      const y = Math.round((t.clientY - rect.top) * scaleY);
      sendCmd({ type: 'mouse_move', x, y });
      sendCmd({ type: 'mouse_down', x, y, button: 'left' });
    }
    e.preventDefault();
  }, { passive: false });
  canvas.addEventListener('touchmove', (e) => {
    if (e.touches.length === 1) {
      const t = e.touches[0];
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
      const x = Math.round((t.clientX - rect.left) * scaleX);
      const y = Math.round((t.clientY - rect.top) * scaleY);
      sendCmd({ type: 'mouse_move', x, y });
    }
    e.preventDefault();
  }, { passive: false });
  canvas.addEventListener('touchend', (e) => {
    sendCmd({ type: 'mouse_up', button: 'left' });
    e.preventDefault();
  }, { passive: false });

  // ---------- 键盘事件 ----------
  // 这些按键会阻止浏览器默认行为，确保转发到远程桌面
  const BLOCK_DEFAULT_KEYS = new Set([
    'Tab', 'Backspace', ' ', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
    'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
    'Home', 'End', 'PageUp', 'PageDown',
  ]);

  document.addEventListener('keydown', (e) => {
    // 不拦截浏览器自身的刷新/开发者工具等组合键
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
      if (text) {
        sendCmd({ type: 'type_text', text: text });
        textPaste.value = '';
      }
      e.preventDefault();
    }
    // 阻止普通字符键被全局键盘处理器转发
    e.stopPropagation();
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
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen();
    }
  });

  // 启动连接
  connect();
})();
