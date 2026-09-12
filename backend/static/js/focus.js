(function() {
  'use strict';

  // ========== 全局状态 ==========
  const state = {
    sessionId: null,
    mode: 'topic',
    topic: '',
    questionId: '',
    status: 'idle', // idle | preparing | teaching | checkpoint | paused | completed
    currentSegment: null,
    isListening: false,
    recognition: null,
    synth: window.speechSynthesis,
    videoStream: null,
    voiceText: '',
    // 不再预置伪造的 'normal' 特征：无语音数据时保持 null（见 calculateVoiceFeatures）
    voiceFeatures: null,
    _voiceUsed: false,
    audioCtx: null,
    audioAnalyser: null,
    audioDataArray: null,
    lastSpeechTime: 0,
    pauseCount: 0,
    wordCount: 0,
    speechStartTime: 0,
    isSpeaking: false,
    // TTS 双引擎状态：ttsSeq 为朗读代次号（stopSpeaking/speak 递增使在飞结果过期），
    // ttsAbort 取消进行中的请求；当前音频实例与 blob URL 由模块级变量持有并统一回收
    ttsSeq: 0,
    ttsAbort: null,
    ttsAvailable: false,
    voiceEngine: 'auto',
    // 黑板板书状态：board 为 session.board（无黑板功能/开关关闭时为 null）
    board: null,
    boardPage: 0,
  };

  // 当前服务器配音实例与 blob URL；配合 _disposeServerAudio 统一回收
  var _serverAudio = null;
  var _serverAudioUrl = null;

  function _disposeServerAudio() {
    if (_serverAudio) {
      try { _serverAudio.pause(); } catch(e) {}
      _serverAudio = null;
    }
    if (_serverAudioUrl) {
      try { URL.revokeObjectURL(_serverAudioUrl); } catch(e) {}
      _serverAudioUrl = null;
    }
  }

  // ========== DOM 引用 ==========
  const dom = {};

  function cacheDom() {
    dom.startScreen = document.getElementById('focusStartScreen');
    dom.studyScreen = document.getElementById('focusStudyScreen');
    dom.endScreen = document.getElementById('focusEndScreen');
    dom.modeSelect = document.getElementById('focusMode');
    dom.topicInput = document.getElementById('focusTopic');
    dom.questionInput = document.getElementById('focusQuestionId');
    dom.topicInputRow = document.getElementById('topicInputRow');
    dom.questionInputRow = document.getElementById('questionInputRow');
    dom.startError = document.getElementById('startError');
    dom.statusBadge = document.getElementById('focusStatusBadge');
    dom.topicDisplay = document.getElementById('focusTopicDisplay');
    dom.progress = document.getElementById('focusProgress');
    dom.content = document.getElementById('focusContent');
    dom.checkpoint = document.getElementById('focusCheckpoint');
    dom.btnVoice = document.getElementById('btnVoice');
    dom.voiceStatus = document.getElementById('voiceStatus');
    dom.voiceTextPreview = document.getElementById('voiceTextPreview');
    dom.textFallback = document.getElementById('textFallback');
    dom.voiceTextInput = document.getElementById('voiceTextInput');
  // 文字降级输入的**唯一**可用入口。
  // 原实现的死路：submit 按钮 disabled 的解除点只有 recognition.onresult（:348），
  // 而无 SpeechRecognition 的环境（Android WebView、多数非 Chrome 浏览器）里
  // startListening 会直接 return，只把 textarea 显示出来；textarea 又没有任何监听，
  // 于是学生看到"请使用文字输入"、输入完按钮仍是灰色 → **检查点无法提交、会话无法推进**。
  // 这里补上输入监听：有内容即解除禁用，并同步给 state.voiceText 供提交使用。
  if (dom.voiceTextInput) {
    dom.voiceTextInput.addEventListener('input', function () {
      state.voiceText = this.value || '';
      if (dom.btnSubmit) dom.btnSubmit.disabled = !state.voiceText.trim();
    });
  }
    dom.emotionCapture = document.getElementById('emotionCapture');
    dom.video = document.getElementById('focusVideo');
    dom.canvas = document.getElementById('focusCanvas');
    dom.btnSubmit = document.getElementById('btnSubmitCheckpoint');
    dom.pauseOverlay = document.getElementById('focusPauseOverlay');
    dom.summary = document.getElementById('focusSummary');
    dom.unsupported = document.getElementById('focusUnsupported');
  }

  // ========== 初始化 ==========
  function init() {
    cacheDom();

    // 模式切换
    if (dom.modeSelect) {
      dom.modeSelect.addEventListener('change', onModeChange);
    }

    // 检查浏览器支持
    checkBrowserSupport();
    // G8：引擎相关提示必须等配置读回后再判断
    loadVoiceConfig().then(warnIfNoSynthesis);
  }

  function loadVoiceConfig() {
    // 配音引擎配置读取失败不阻塞主流程：视为不可用，走浏览器合成。
    // 返回 Promise，便于调用方在配置**读回之后**再做依赖它的 UI 判断（见 G8）。
    if (!window.$API || !window.$API.get) return Promise.resolve();
    return window.$API.get('/api/settings').then(function(s){
      state.ttsAvailable = !!s.tts_available;
      state.voiceEngine = (s.focus_voice_engine === 'xiaomi' || s.focus_voice_engine === 'browser') ? s.focus_voice_engine : 'auto';
    }).catch(function(){});
  }

  // G8 修复：本判断依赖 loadVoiceConfig() 的结果，必须在配置到位后调用。
  // 原实现把它放在同步的 checkBrowserSupport() 里，而该函数在 init() 中先于异步
  // loadVoiceConfig() 返回执行 → state.voiceEngine 必为初始 'auto' →
  // `state.voiceEngine !== 'xiaomi'` 恒真，这个条件对"是否配置了小米优先"毫无判别力。
  function warnIfNoSynthesis() {
    if (!window.speechSynthesis && state.voiceEngine !== 'xiaomi') {
      console.warn('浏览器不支持语音合成，且未配置优先小米配音；讲解将依赖服务器配音（可用时会自动回退）');
    }
  }

  function checkBrowserSupport() {
    // R24：安卓 App 里 Web Speech API 不存在，但原生桥（android_sr.js 适配层）提供同形接口
    const hasSpeechRecognition = !!pickSpeechRecognitionCtor();

    // 配音已有服务器引擎兜底（/api/tts），合成缺失本身不构成"环境不支持"；
    // 只有语音识别彻底不可用才展示降级提示。
    if (!hasSpeechRecognition) {
      dom.unsupported.style.display = 'block';
      // 显示文字降级输入
      dom.textFallback.style.display = 'block';
    }
  }

  function onModeChange() {
    const mode = dom.modeSelect.value;
    if (mode === 'topic') {
      dom.topicInputRow.style.display = '';
      dom.questionInputRow.style.display = 'none';
    } else if (mode === 'question') {
      dom.topicInputRow.style.display = 'none';
      dom.questionInputRow.style.display = '';
    } else {
      dom.topicInputRow.style.display = '';
      dom.questionInputRow.style.display = '';
    }
  }

  // ========== 会话控制 ==========
  async function start() {
    const mode = dom.modeSelect.value;
    const topic = (dom.topicInput.value || '').trim();
    const questionId = (dom.questionInput.value || '').trim();

    if (mode === 'topic' && !topic) {
      showStartError('请输入学习主题');
      return;
    }
    // 功能检查轮 UX 修复（H3）：启动期间禁用按钮防连点——后端同步等 AI 生成
    // 首段（10-30s），连点会创建多个并发会话并重复计费
    const startBtn = document.getElementById('btnStartFocus');
    if (startBtn && startBtn.disabled) return;   // 已在启动中
    if (startBtn) { startBtn.disabled = true; startBtn.dataset._origText = startBtn.textContent; startBtn.textContent = '启动中...'; }

    state.mode = mode;
    state.topic = topic;
    state.questionId = questionId;
    if (state._checkpointTimer) { clearTimeout(state._checkpointTimer); state._checkpointTimer = null; }
    hideStartError();
    const _restoreBtn = function () {
      if (startBtn) { startBtn.disabled = false; startBtn.textContent = (startBtn.dataset._origText || '开始学习'); }
    };

    try {
      const resp = await $API.post('/api/focus/start', {
        mode, topic, question_id: questionId,
      });

      if (!resp || !resp.ok) {
        _restoreBtn();
        showStartError((resp && resp.detail) || '启动失败');
        return;
      }

      state.sessionId = resp.session.id;
      state.totalCheckpoints = resp.session.total_checkpoints || 0;
      state.status = 'teaching';
      state.currentSegment = resp.session.segments[0];
      state.board = resp.session.board || null;
      state.boardPage = Math.max(0, (state.board && state.board.pages ? state.board.pages.length : 1) - 1);

      showStudyScreen();
      renderSegment(state.currentSegment);
      // 成功路径也必须恢复按钮。原实现只在**两个失败分支**调 _restoreBtn，而 reset()
      // 又不碰 btnStartFocus → 结束后点"新的学习"时按钮仍是 disabled 且写着"启动中..."，
      // 再叠加 start() 开头的 `if (startBtn.disabled) return` 守卫，
      // **同一页面生命周期内只能开始一次学习**（已确证）。
      _restoreBtn();
    } catch (err) {
      _restoreBtn();
      showStartError('启动失败：' + (err.message || '未知错误'));
    }
  }

  function showStudyScreen() {
    dom.startScreen.style.display = 'none';
    dom.studyScreen.style.display = 'block';
    dom.endScreen.style.display = 'none';
    dom.topicDisplay.textContent = state.topic || '专注学习';
    updateStatusBadge('学习中');
  }

  function showEndScreen(summary) {
    dom.startScreen.style.display = 'none';
    dom.studyScreen.style.display = 'none';
    dom.endScreen.style.display = 'block';

    const total = summary.total_checkpoints || 0;
    const passed = summary.passed_checkpoints || 0;
    const passRate = total > 0 ? Math.round(passed / total * 100) : 0;

    dom.summary.innerHTML = `
      <p>学习主题：<strong>${$esc(state.topic || '未知')}</strong></p>
      <p>检查点总数：${total}</p>
      <p>通过数：${passed}</p>
      <p>通过率：${passRate}%</p>
      <p>讲解段数：${(summary.segments || []).length}</p>
    `;
  }

  function updateStatusBadge(text) {
    if (dom.statusBadge) dom.statusBadge.textContent = text;
  }

  function updateProgress(segCount, totalCheckpoints) {
    if (dom.progress) {
      dom.progress.textContent = `第 ${segCount} 段 · ${totalCheckpoints} 个检查点`;
    }
  }

  function showStartError(msg) {
    dom.startError.textContent = msg;
    dom.startError.style.display = 'block';
  }

  function hideStartError() {
    dom.startError.style.display = 'none';
  }

  // ========== 渲染讲解 ==========
  function renderSegment(segment) {
    if (!segment) return;
    const content = segment.content || '';
    // 使用 $md 渲染 Markdown
    dom.content.innerHTML = '';
    $md(content, dom.content);

    // 失败段视觉区分（M1）：整段以警示样式呈现，学生提交确认即重试
    if (segment.failed) {
      dom.content.style.color = 'var(--err, #c0392b)';
      dom.content.style.fontStyle = 'italic';
    } else {
      dom.content.style.color = '';
      dom.content.style.fontStyle = '';
    }

    // AI 快照提示（本段执行了 snapshot 指令）
    if (segment.board_summary && segment.board_summary.snapshot > 0) {
      $toast('AI 已保存课堂回看快照', 'info');
    }

    // 黑板渲染
    renderBoard();

    // 更新进度
    // G7 修复：第二个形参是「检查点总数」，原实现却传了 segment.index（段序号），
    // 于是界面恒显示「第 N 段 · N 个检查点」两个数字相等；而 submitCheckpoint 里又用真实的
    // total_checkpoints 再调一次（同一控件两套语义互相覆盖）。统一改为读会话真实总数。
    updateProgress(
      (segment.index || 0) + 1,
      state.totalCheckpoints || 0
    );

    // 语音播放
    speak(content);

    // 检查是否有检查点
    if (segment.has_checkpoint) {
      // 延迟显示检查点，让语音先播一段；
      // 记录定时器句柄，会话结束/重置时必须取消，避免结束后弹窗
      if (state._checkpointTimer) clearTimeout(state._checkpointTimer);
      const sidAtSchedule = state.sessionId;
      state._checkpointTimer = setTimeout(() => {
        state._checkpointTimer = null;
        // 定时器触发前会话已被结束/重置则不再弹出
        if (!state.sessionId || state.sessionId !== sidAtSchedule) return;
        showCheckpoint();
      }, 1500);
    }
  }

  // ========== 检查点 ==========
  function showCheckpoint() {
    state.status = 'checkpoint';
    dom.checkpoint.style.display = 'block';
    dom.btnSubmit.disabled = true;
    state.voiceText = '';
    // 不再预置"normal"假值：无数据就保持 null，由 calculateVoiceFeatures 决定是否填充。
    state.voiceFeatures = null;
    state._voiceUsed = false;
    dom.voiceTextPreview.style.display = 'none';
    dom.voiceTextPreview.textContent = '';
    dom.voiceStatus.textContent = '';
  }

  function hideCheckpoint() {
    if (state._checkpointTimer) { clearTimeout(state._checkpointTimer); state._checkpointTimer = null; }
    dom.checkpoint.style.display = 'none';
    stopListening();
    stopCamera();
  }

  // ========== 语音输入 ==========
  function toggleVoice() {
    if (state.isListening) {
      stopListening();
    } else {
      startListening();
    }
  }

  // 识别构造函数选择：原生桥优先于 Web Speech（在 App 里前者才是能用的那个）
  function pickSpeechRecognitionCtor() {
    if (window.AndroidSpeechRecognition &&
        (typeof window.AndroidSpeechRecognition.isAvailable !== 'function'
         || window.AndroidSpeechRecognition.isAvailable())) {
      return window.AndroidSpeechRecognition;
    }
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
  }

  function startListening() {
    const SpeechRecognition = pickSpeechRecognitionCtor();
    if (!SpeechRecognition) {
      // 降级到文字输入
      dom.textFallback.style.display = 'block';
      dom.voiceStatus.textContent = '浏览器不支持语音识别，请使用文字输入';
      return;
    }

    state.recognition = new SpeechRecognition();
    state.recognition.lang = 'zh-CN';
    state.recognition.continuous = true;
    state.recognition.interimResults = true;

    state.speechStartTime = Date.now();
    state.pauseCount = 0;
    state.wordCount = 0;
    state._retryCount = 0;
    state.lastSpeechTime = Date.now();

    state.recognition.onresult = function(event) {
      // 真的收到识别结果 = 本次确实用了语音作答；只有这种情况才允许计算并上报语音特征。
      // 纯文字作答路径不会走到这里，因此不会产生"编造的语速/音量"（见 calculateVoiceFeatures）。
      state._voiceUsed = true;
      let interimTranscript = '';
      let newFinal = '';

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          newFinal += transcript;
          state.wordCount += transcript.length;
        } else {
          interimTranscript += transcript;
        }
      }

      // G4 修复：final 文本必须**累加**，不能每次事件整体覆盖。
      // 原实现 `state.voiceText = finalTranscript || interimTranscript` 会让后续事件的
      // interim 片段把已确认的 final 文本冲掉，continuous=true 下多句回答最终只剩最后一片，
      // 却被当作完整作答提交。这里维护一个累积的已确认文本，再用当前 interim 追加显示。
      if (newFinal) state._finalText = (state._finalText || '') + newFinal;
      state.voiceText = (state._finalText || '') + interimTranscript;
      dom.voiceTextPreview.textContent = state.voiceText;
      dom.voiceTextPreview.style.display = 'block';
      dom.btnSubmit.disabled = !state.voiceText;
      // 任何识别结果（含 interim）都算"正在说话"；停顿交给 _pauseTimer 按空闲时间判定。
      state.lastSpeechTime = Date.now();
    };

    state.recognition.onerror = function(event) {
      console.warn('Speech recognition error:', event.error);
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        dom.voiceStatus.textContent = '麦克风权限被拒绝，请使用文字输入';
        dom.textFallback.style.display = 'block';
      }
    };

    state.recognition.onend = function() {
      if (state.isListening) {
        // 限制重试次数，避免无限循环
        state._retryCount = (state._retryCount || 0) + 1;
        if (state._retryCount <= 3) {
          try { state.recognition.start(); } catch(e) {}
        } else {
          state.isListening = false;
          dom.btnVoice.classList.remove('active');
          dom.voiceStatus.textContent = '语音识别已停止，请重新点击';
        }
      }
    };

    try {
      state.recognition.start();
      state.isListening = true;
      dom.btnVoice.classList.add('active');
      dom.voiceStatus.textContent = '正在聆听...';
      startAudioAnalysis();
      startPauseWatch();
    } catch (e) {
      dom.voiceStatus.textContent = '启动语音识别失败：' + e.message;
    }
  }

  function stopListening() {
    state.isListening = false;
    if (state.recognition) {
      try { state.recognition.stop(); } catch(e) {}
      state.recognition = null;
    }
    dom.btnVoice.classList.remove('active');
    dom.voiceStatus.textContent = '已停止';
    stopPauseWatch();
    stopAudioAnalysis();
    calculateVoiceFeatures();
  }

  // 停顿检测：**真实定时轮询**，而不是"事件计数"。
  // 原实现 detectPause 只在 recognition.onresult 里被调用，且 lastSpeechTime 仅在 final
  // 结果时刷新 → pause_count 实际等于"距上次 final 超过 1 秒后到达的 interim 事件数"，
  // 与真实停顿无关（识别服务通常每 100~300ms 推一次 interim，该值会系统性虚高），
  // 而这个数字会进入 voice_features 并被上报用于难度调节。
  function startPauseWatch() {
    stopPauseWatch();
    state._pauseTimer = setInterval(function () {
      if (!state.isListening) return;
      if (Date.now() - state.lastSpeechTime > 1200) {
        state.lastSpeechTime = Date.now();   // 一次停顿只计一次
        state.pauseCount++;
      }
    }, 400);
  }
  function stopPauseWatch() {
    if (state._pauseTimer) { clearInterval(state._pauseTimer); state._pauseTimer = null; }
  }

  // ========== 音频分析（语音特征） ==========
  function startAudioAnalysis() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;

    navigator.mediaDevices.getUserMedia({ audio: true }).then(stream => {
      state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const source = state.audioCtx.createMediaStreamSource(stream);
      state.audioAnalyser = state.audioCtx.createAnalyser();
      state.audioAnalyser.fftSize = 256;
      source.connect(state.audioAnalyser);
      state.audioDataArray = new Uint8Array(state.audioAnalyser.frequencyBinCount);
      state._audioStream = stream;
      analyzeAudioVolume();
    }).catch(() => {
      // 音频分析失败不影响主流程
    });
  }

  function analyzeAudioVolume() {
    if (!state.audioAnalyser || !state.isListening) return;
    state.audioAnalyser.getByteFrequencyData(state.audioDataArray);
    const avg = state.audioDataArray.reduce((a, b) => a + b, 0) / state.audioDataArray.length;
    state._lastVolume = avg;
    requestAnimationFrame(analyzeAudioVolume);
  }

  function stopAudioAnalysis() {
    if (state._audioStream) {
      state._audioStream.getTracks().forEach(t => t.stop());
      state._audioStream = null;
    }
    if (state.audioCtx) {
      try { state.audioCtx.close(); } catch(e) {}
      state.audioCtx = null;
    }
    state.audioAnalyser = null;
  }

  function calculateVoiceFeatures() {
    // 只有**真的收到过识别结果**才计算语音特征。
    // 原实现无条件计算：纯文字作答时 state.speechStartTime 仍是初值 0、state._lastVolume
    // 从未被赋值 → wpm≈0 判 'slow'、音量判 'quiet'，pause_count 也恒 0；这些编造值会被
    // 真实 POST 给后端用于难度调节与长期记忆（已确证）。无数据就不上报。
    if (!state._voiceUsed) {
      state.voiceFeatures = null;
      return;
    }
    // 确实用过语音才初始化特征对象（不再由 showCheckpoint 预置假值）
    state.voiceFeatures = { speed: 'normal', volume: 'normal', pause_count: 0 };
    const duration = (Date.now() - state.speechStartTime) / 1000; // 秒
    const cpm = duration > 0 ? (state.wordCount / duration * 60) : 0;

    // 中文语速以「字/分」衡量（正常约 200~300 字/分）。
    // 原实现拿 transcript.length（**字符数**）去套英文 word/min 的 80/200 阈值，
    // 语义错位 → 中文正常语速会被判成 'fast'，slow/fast 标签不可信。
    if (cpm < 150) state.voiceFeatures.speed = 'slow';
    else if (cpm > 320) state.voiceFeatures.speed = 'fast';
    else state.voiceFeatures.speed = 'normal';

    // 音量判断
    const avgVolume = state._lastVolume || 0;
    if (avgVolume < 30) state.voiceFeatures.volume = 'quiet';
    else if (avgVolume > 100) state.voiceFeatures.volume = 'loud';
    else state.voiceFeatures.volume = 'normal';

    // 停顿次数
    state.voiceFeatures.pause_count = state.pauseCount;
  }

  // ========== 语音合成输出（双引擎） ==========
  function stripCheckpointMark(text) {
    return text.replace(/[，。？！\n]*对吧[？?]?$/, '').trim();
  }

  function serverSpeak(text, gen) {
    // 小米 TTS。返回 Promise：
    //   true  成功起播；
    //   false 服务器失败（非中止），调用方视情况回退浏览器合成；
    //   null  请求被中止/结果已过期——绝不能触发浏览器合成
    //         （否则暂停/结束后语音"死而复生"）。
    _disposeServerAudio();
    if (state.ttsAbort) { try { state.ttsAbort.abort(); } catch(e) {} }
    var controller = new AbortController();
    state.ttsAbort = controller;
    // 前端侧超时与后端一致：60s 无响应视为失败
    var timer = setTimeout(function(){
      if (state.ttsAbort === controller) { try { controller.abort(); } catch(e) {} }
    }, 60000);
    return fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': (window.$API && window.$API._authToken) || '' },
      body: JSON.stringify({ text: text }),
      signal: controller.signal,
    }).then(function(resp){
      if (!resp.ok) return { failed: true };
      return resp.blob().then(function(b){ return { blob: b }; });
    }).then(function(out){
      clearTimeout(timer);
      if (state.ttsAbort === controller) state.ttsAbort = null;
      if (out && out.blob) {
        if (gen !== state.ttsSeq || state.status !== 'teaching') return null; // 已过期：静默丢弃
        var url = URL.createObjectURL(out.blob);
        var audio = new Audio(url);
        _serverAudio = audio;
        _serverAudioUrl = url;
        audio.onended = function(){ if (_serverAudio === audio) _disposeServerAudio(); state.isSpeaking = false; };
        audio.onerror = function(){ if (_serverAudio === audio) _disposeServerAudio(); state.isSpeaking = false; };
        var playPromise = audio.play();
        if (playPromise && playPromise.catch) playPromise.catch(function(){
          if (_serverAudio === audio) _disposeServerAudio();
          state.isSpeaking = false;
        });
        return true;
      }
      return out && out.failed ? false : null;
    }).catch(function(err){
      clearTimeout(timer);
      if (state.ttsAbort === controller) state.ttsAbort = null;
      if (err && (err.name === 'AbortError' || err.code === 20)) return null; // 主动中止不等于失败
      return false;
    });
  }

  function speak(text) {
    // 移除「对吧」不朗读（它是视觉标记）
    const cleanText = stripCheckpointMark(text || '');
    if (!cleanText) return;

    const engine = state.voiceEngine;
    const wantServer = engine !== 'browser' && (engine === 'xiaomi' || state.ttsAvailable);
    if (wantServer) {
      const gen = ++state.ttsSeq;
      serverSpeak(cleanText.slice(0, 1900), gen).then(function(result){
        if (result === true) { state.isSpeaking = true; return; }
        if (result === null) return; // 中止或过期：不回退、不出声
        // 真正的服务器失败：提示一次并回退浏览器合成
        if (typeof $toast === 'function') $toast('AI 配音不可用，已切换浏览器合成', 'info');
        if (gen === state.ttsSeq) browserSpeak(cleanText);
      });
      return;
    }
    browserSpeak(cleanText);
  }

  function browserSpeak(cleanText) {
    // 兜底合成同样受状态守卫：只有处于教学态才允许发声
    if (!state.sessionId || state.status !== 'teaching') return;
    // R24：安卓壳里**没有** speechSynthesis，但有原生 TTS 桥。
    // lecture.js 早就接了这座桥，focus 一直没接 —— 结果是 App 内专注模式在
    // 服务器配音不可用时"完全没声音"，且没有任何提示。这里补上同一条优先链。
    if (window.AndroidTTS && typeof window.AndroidTTS.speak === 'function') {
      try {
        window.AndroidTTS.speak(cleanText);
        state.isSpeaking = true;
        return;
      } catch (e) {
        // 桥异常时继续往下走（不再有 speechSynthesis 就自然静默，不伪造成功）
      }
    }
    if (!state.synth) return;
    state.synth.cancel();

    const utterance = new SpeechSynthesisUtterance(cleanText);
    utterance.lang = 'zh-CN';
    utterance.rate = 1.0;
    utterance.pitch = 1.0;

    // 尝试选择中文语音
    const voices = state.synth.getVoices();
    const zhVoice = voices.find(v => v.lang && v.lang.startsWith('zh'));
    if (zhVoice) utterance.voice = zhVoice;

    state.synth.speak(utterance);
    state.isSpeaking = true;
    utterance.onend = function() { state.isSpeaking = false; };
  }

  function stopSpeaking() {
    // 双引擎同停：使在飞请求过期（代次号）并中止、释放音频资源、清空浏览器队列
    state.ttsSeq++;
    if (state.ttsAbort) { try { state.ttsAbort.abort(); } catch(e) {} state.ttsAbort = null; }
    _disposeServerAudio();
    if (window.AndroidTTS && typeof window.AndroidTTS.stop === 'function') {
      try { window.AndroidTTS.stop(); } catch (e) { /* 桥已停 */ }
    }
    if (state.synth) state.synth.cancel();
    state.isSpeaking = false;
  }

  // ========== 摄像头采集 ==========
  async function captureFace() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return null;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: 320, height: 240, facingMode: 'user' }
      });
      state.videoStream = stream;
      dom.video.srcObject = stream;
      dom.emotionCapture.style.display = 'block';

      // 等待视频就绪（带超时保护）
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('视频加载超时')), 5000);
        dom.video.onloadedmetadata = () => {
          clearTimeout(timer);
          setTimeout(resolve, 300);
        };
        dom.video.onerror = () => {
          clearTimeout(timer);
          reject(new Error('视频加载失败'));
        };
      });

      // 截图
      dom.canvas.width = dom.video.videoWidth || 320;
      dom.canvas.height = dom.video.videoHeight || 240;
      const ctx = dom.canvas.getContext('2d');
      ctx.drawImage(dom.video, 0, 0, dom.canvas.width, dom.canvas.height);

      // 转为 Base64，移除 data URI 前缀
      const base64DataUrl = dom.canvas.toDataURL('image/jpeg', 0.7);
      const cleanBase64 = base64DataUrl.split(',')[1] || base64DataUrl;

      stopCamera();
      return cleanBase64;
    } catch (e) {
      console.warn('Camera capture failed:', e);
      // 流可能已在 state.videoStream 中（超时/加载失败路径），必须释放，否则摄像头指示灯常亮
      stopCamera();
      return null;
    }
  }

  function stopCamera() {
    if (state.videoStream) {
      state.videoStream.getTracks().forEach(t => t.stop());
      state.videoStream = null;
    }
    dom.video.srcObject = null;
    dom.emotionCapture.style.display = 'none';
  }

  // ========== 提交检查点 ==========
  async function submitCheckpoint() {
    if (!state.sessionId) return;

    dom.btnSubmit.disabled = true;
    dom.voiceStatus.textContent = '正在分析状态...';

    // 1. 获取文字输入（降级）
    if (!state.voiceText && dom.voiceTextInput) {
      state.voiceText = dom.voiceTextInput.value || '';
    }

    // 2. 采集表情
    let webcamImage = '';
    try {
      webcamImage = await captureFace();
    } catch(e) {
      // 摄像头失败不影响提交
    }

    // 3. 提交到后端
    try {
      const resp = await $API.post(`/api/focus/${state.sessionId}/checkpoint`, {
        voice_text: state.voiceText,
        emotion_report: null, // 后端根据 webcam_image 自行分析
        webcam_image: webcamImage,
        voice_features: state.voiceFeatures,
        segment_index: state.currentSegment && typeof state.currentSegment.index === 'number'
          ? state.currentSegment.index : null,
      });

      if (!resp || !resp.ok) {
        dom.voiceStatus.textContent = '提交失败，请重试';
        dom.btnSubmit.disabled = false;
        return;
      }

      hideCheckpoint();

      if (resp.action === 'pause') {
        showPauseOverlay();
        return;
      }

      // 渲染下一段
      state.currentSegment = resp.segment;
      state.status = 'teaching';
      if (resp.session) {
        state.board = resp.session.board || null;
        state.totalCheckpoints = resp.session.total_checkpoints || 0;
        // 新板书内容写入当前页：翻到最新页让学生看到
        if (state.board && state.board.pages) state.boardPage = state.board.pages.length - 1;
        updateProgress(
          resp.session.segments.length,
          state.totalCheckpoints
        );
      }
      renderSegment(state.currentSegment);
    } catch (e) {
      dom.voiceStatus.textContent = '提交失败：' + (e.message || '未知错误');
      dom.btnSubmit.disabled = false;
    }
  }

  // ========== 黑板板书 ==========
  function renderBoard() {
    const panel = document.getElementById('boardPanel');
    if (!panel) return; // 模板未启用黑板
    const board = state.board;
    if (!board || !board.pages || !board.pages.length) {
      panel.style.display = 'none';
      return;
    }
    panel.style.display = 'block';
    if (state.boardPage >= board.pages.length) state.boardPage = board.pages.length - 1;
    if (state.boardPage < 0) state.boardPage = 0;
    const page = board.pages[state.boardPage] || { entries: [] };
    const surface = document.getElementById('boardSurface');
    if (!surface) return;
    surface.innerHTML = '';
    const entries = page.entries || [];
    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'board-empty';
      empty.textContent = '（本页暂无板书）';
      surface.appendChild(empty);
    }
    entries.forEach((e) => {
      if (e && (e.kind === 'svg' || e.kind === 'image') && e.asset) {
        const card = document.createElement('div');
        card.className = e.kind === 'image' ? 'board-svg-card board-ref-card' : 'board-svg-card';
        const img = document.createElement('img');
        img.alt = e.title || '板书图形';
        img.src = `/api/focus/${encodeURIComponent(state.sessionId || '')}/board/asset/${encodeURIComponent(e.asset)}`;
        img.onerror = function () {
          card.innerHTML = '';
          const err = document.createElement('div');
          err.className = 'board-svg-err';
          err.textContent = '图形加载失败';
          card.appendChild(err);
        };
        card.appendChild(img);
        if (e.title) {
          const t = document.createElement('div');
          t.className = 'board-svg-title';
          t.textContent = e.title;
          card.appendChild(t);
        }
        surface.appendChild(card);
      } else if (e && e.kind === 'text' && e.content) {
        const div = document.createElement('div');
        div.className = 'board-text';
        $md(e.content, div);
        surface.appendChild(div);
      }
    });
    const ind = document.getElementById('boardPageIndicator');
    if (ind) ind.textContent = `${state.boardPage + 1}/${board.pages.length}`;
    const snaps = document.getElementById('boardSnapshots');
    if (snaps && snaps.style.display !== 'none') renderBoardSnapshots();
    syncBoardNav();
  }

  function syncBoardNav() {
    // 前端设计优化：翻页按钮到首/末页时置灰。
    // 原实现 boardPageShift 越界直接 return，按钮仍可点 → 点了没反应、也无任何反馈，
    // 用户会以为"黑板坏了"。置灰能明确表达"已经到头了"。
    const total = (state.board && state.board.pages) ? state.board.pages.length : 0;
    const prev = document.getElementById('boardPrev');
    const next = document.getElementById('boardNext');
    if (prev) prev.disabled = (total <= 1) || (state.boardPage <= 0);
    if (next) next.disabled = (total <= 1) || (state.boardPage >= total - 1);
  }

  function boardPageShift(delta) {
    if (!state.board || !state.board.pages) return;
    const next = state.boardPage + delta;
    if (next < 0 || next >= state.board.pages.length) return;
    state.boardPage = next;
    renderBoard();
  }

  function renderBoardSnapshots() {
    const box = document.getElementById('boardSnapshots');
    if (!box) return;
    box.innerHTML = '';
    const snaps = (state.board && state.board.snapshots) || [];
    if (!snaps.length) {
      const empty = document.createElement('div');
      empty.className = 'board-empty';
      empty.textContent = '（还没有快照）';
      box.appendChild(empty);
      return;
    }
    snaps.slice().reverse().forEach((s) => {
      const row = document.createElement('button');
      row.className = 'board-snapshot-item';
      row.type = 'button';
      row.textContent = `${s.label} · 第${s.page}页${s.by === 'student' ? ' · 我存' : ''}`;
      row.addEventListener('click', () => {
        if (state.board && state.board.pages) {
          state.boardPage = Math.min(Math.max(0, (s.page || 1) - 1), state.board.pages.length - 1);
          renderBoard();
        }
        box.style.display = 'none';
      });
      box.appendChild(row);
    });
  }

  function toggleBoardSnapshots() {
    const box = document.getElementById('boardSnapshots');
    if (!box) return;
    if (box.style.display === 'none') {
      renderBoardSnapshots();
      box.style.display = 'flex';
    } else {
      box.style.display = 'none';
    }
  }

  async function saveBoardSnapshot() {
    if (!state.sessionId) return;
    const btn = document.getElementById('boardSaveBtn');
    if (btn) btn.disabled = true;
    try {
      const resp = await $API.post(`/api/focus/${encodeURIComponent(state.sessionId)}/board/snapshot`, { label: '' });
      if (resp && resp.ok && resp.snapshot) {
        state.board = state.board || { pages: [], snapshots: [] };
        state.board.snapshots = state.board.snapshots || [];
        state.board.snapshots.push(resp.snapshot);
        $toast('已保存本页快照', 'ok');
        const box = document.getElementById('boardSnapshots');
        if (box && box.style.display !== 'none') renderBoardSnapshots();
      } else {
        // 静默失败修正：原实现只在"成功且带 snapshot"时给反馈，`ok` 为假（服务端拒绝、
        // 黑板未启用、会话已结束等）时**既不提示成功也不提示失败**，用户点了按钮像没反应。
        $toast((resp && resp.detail) || '保存快照失败：服务端未返回快照', 'error');
      }
    } catch (e) {
      $toast(e.message || '保存快照失败', 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ========== 暂停/恢复/结束 ==========
  function showPauseOverlay() {
    state.status = 'paused';
    dom.pauseOverlay.style.display = 'block';
    updateStatusBadge('已暂停');
    stopSpeaking();
  }

  async function togglePause() {
    if (state.status === 'paused') {
      await resume();
    } else {
      await pause();
    }
  }

  async function pause() {
    if (!state.sessionId) return;
    try {
      await $API.post(`/api/focus/${state.sessionId}/pause`);
      showPauseOverlay();
    } catch(e) {
      $toast('暂停失败', 'error');
    }
  }

  async function resume() {
    if (!state.sessionId) return;
    try {
      const resp = await $API.post(`/api/focus/${state.sessionId}/resume`);
      if (resp && resp.ok) {
        dom.pauseOverlay.style.display = 'none';
        state.status = 'teaching';
        updateStatusBadge('学习中');
      }
    } catch(e) {
      $toast('恢复失败', 'error');
    }
  }

  async function end() {
    if (!state.sessionId) return;
    stopSpeaking();
    stopCamera();
    if (state._checkpointTimer) { clearTimeout(state._checkpointTimer); state._checkpointTimer = null; }
    try {
      const resp = await $API.post(`/api/focus/${state.sessionId}/end`);
      if (resp && resp.ok) {
        showEndScreen(resp.session);
      }
    } catch(e) {
      $toast('结束失败', 'error');
    }
  }

  function reset() {
    state.sessionId = null;
    state.status = 'idle';
    state.currentSegment = null;
    state.voiceText = '';
    state.board = null;
    state.boardPage = 0;
    state.voiceFeatures = null;
    state._voiceUsed = false;
    stopPauseWatch();
    stopSpeaking();
    stopCamera();
    if (state._checkpointTimer) { clearTimeout(state._checkpointTimer); state._checkpointTimer = null; }
    dom.startScreen.style.display = 'block';
    dom.studyScreen.style.display = 'none';
    dom.endScreen.style.display = 'none';
    dom.pauseOverlay.style.display = 'none';
    dom.checkpoint.style.display = 'none';
    dom.content.innerHTML = '';
    const panel = document.getElementById('boardPanel');
    if (panel) panel.style.display = 'none';
    const snaps = document.getElementById('boardSnapshots');
    if (snaps) { snaps.style.display = 'none'; snaps.innerHTML = ''; }
  }

  // ========== 暴露全局接口 ==========
  window.FocusApp = {
    start,
    togglePause,
    pause,
    resume,
    end,
    reset,
    toggleVoice,
    submitCheckpoint,
    boardPageShift,
    toggleBoardSnapshots,
    saveBoardSnapshot,
  };

  // ========== DOMContentLoaded 初始化 ==========
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
