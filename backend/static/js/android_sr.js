// android_sr.js - 安卓原生语音识别适配层（R24）
//
// 为什么需要它：Android WebView **既没有** SpeechSynthesis **也没有** SpeechRecognition
// （不实现 Web Speech API）。合成那边已经有 TTSBridge，识别这边一直是空的 ——
// 后果是 focus 模式的「语音作答」在 App 里**恒定不可用**：
// 页面显示"浏览器不支持语音识别，请使用文字输入"（诚实降级，但功能就是没了）。
//
// 做法：把原生 AndroidSR 桥（Java 侧 SpeechRecognizer + 轮询事件队列）包装成一个
// **与 Web Speech API 同形**的构造函数，这样 focus.js 只需在能力探测里多写一项，
// 其余识别逻辑（累积 final、interim 预览、停顿检测、语音特征上报）**一行都不用改**。
//
// 注意：桥返回的事件是 JSON，且中文文本里可能有引号 —— 一旦 parse 失败就跳过本轮，
// 绝不让适配层抛异常拖垮整个 focus 页面。

(function () {
  'use strict';
  if (!window.AndroidSR || typeof window.AndroidSR.pollEvents !== 'function') {
    return;   // 不是安卓壳（普通浏览器/PWA）：不注入，网页侧照旧走 Web Speech 或降级
  }

  var POLL_MS = 150;

  function AndroidSpeechRecognition() {
    this.lang = 'zh-CN';
    this.continuous = true;
    this.interimResults = true;
    this.onresult = null;
    this.onerror = null;
    this.onend = null;
    this.onstart = null;
    this._timer = null;
    this._active = false;
  }

  AndroidSpeechRecognition.isAvailable = function () {
    try {
      return !!window.AndroidSR.isAvailable();
    } catch (e) {
      return false;
    }
  };

  AndroidSpeechRecognition.prototype.start = function () {
    var self = this;
    if (this._active) { return; }
    this._active = true;
    try {
      window.AndroidSR.start();
    } catch (e) {
      this._active = false;
      this._emitError('start_failed');
      return;
    }
    if (typeof this.onstart === 'function') {
      try { this.onstart(); } catch (e) { /* 用户回调异常不影响识别 */ }
    }
    this._timer = setInterval(function () { self._drain(); }, POLL_MS);
  };

  AndroidSpeechRecognition.prototype.stop = function () {
    this._active = false;
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    try { window.AndroidSR.stop(); } catch (e) { /* 已停止 */ }
  };

  // Web Speech API 里 abort 表示"丢弃当前结果"，这里语义相同：直接停
  AndroidSpeechRecognition.prototype.abort = function () { this.stop(); };

  AndroidSpeechRecognition.prototype._drain = function () {
    var raw;
    try {
      raw = window.AndroidSR.pollEvents();
    } catch (e) {
      return;   // 桥异常：本轮跳过，等下一轮
    }
    var events;
    try {
      events = JSON.parse(raw || '[]');
    } catch (e) {
      return;   // 半截 JSON：跳过，不要抛
    }
    if (!events || !events.length) { return; }
    for (var i = 0; i < events.length; i++) {
      var ev = events[i] || {};
      if (ev.type === 'partial') {
        this._emitResult(String(ev.text == null ? '' : ev.text), false);
      } else if (ev.type === 'final') {
        this._emitResult(String(ev.text == null ? '' : ev.text), true);
      } else if (ev.type === 'error') {
        this._emitError(String(ev.code == null ? 'error' : ev.code));
      } else if (ev.type === 'end') {
        this._active = false;
        if (this._timer) { clearInterval(this._timer); this._timer = null; }
        if (typeof this.onend === 'function') {
          try { this.onend(); } catch (e) { /* 忽略 */ }
        }
      }
    }
  };

  // 构造与 Web Speech API 同形的 event：
  //   event.results[i][0].transcript  ← 该条候选的文本
  //   event.results[i].isFinal        ← 是否该段的最终结果
  // focus.js 正是按这两个字段消费的，所以这里必须保持一致（不要"优化"成别的形状）。
  AndroidSpeechRecognition.prototype._emitResult = function (text, isFinal) {
    if (typeof this.onresult !== 'function') { return; }
    var alternatives = [{ transcript: text, confidence: 1 }];
    alternatives.isFinal = !!isFinal;
    var results = [alternatives];
    try {
      this.onresult({ resultIndex: 0, results: results });
    } catch (e) {
      if (window.console && console.warn) { console.warn('AndroidSR onresult 处理异常:', e); }
    }
  };

  AndroidSpeechRecognition.prototype._emitError = function (code) {
    if (typeof this.onerror !== 'function') { return; }
    try {
      this.onerror({ error: code, message: code });
    } catch (e) {
      if (window.console && console.warn) { console.warn('AndroidSR onerror 处理异常:', e); }
    }
  };

  window.AndroidSpeechRecognition = AndroidSpeechRecognition;
})();
