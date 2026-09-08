/* ═══════════════════════════════════════════════════
   motion.js — 渐进增强动画驱动（无依赖，全部特性检测）
   功能：滚动显现 / 级联编号 / 数字滚动 / 指针涟漪 / 图片淡入
   quick-mode 或 prefers-reduced-motion 时自动降级为直接显示
   ═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var docEl = document.documentElement;
  var jsGate = docEl.classList.contains('js'); // base.html 内联脚本设置
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

  function motionOff() {
    return document.body.classList.contains('quick-mode') || !!(mq && mq.matches);
  }

  function safe(fn) { try { fn(); } catch (e) { /* 单点故障不影响其余功能 */ } }

  /* ═══ 1. 滚动显现 + 级联 ═══ */
  var io = null;
  var observed = []; // 用于清理已脱离 DOM 的观察目标

  function compactList(list, observer) {
    var keep = [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].isConnected) keep.push(list[i]);
      else if (observer) observer.unobserve(list[i]);
    }
    return keep;
  }

  function compactObserved() {
    if (!io) { observed = []; return; }
    observed = compactList(observed, io);
  }

  function numberChildren(box) {
    var kids = box.children;
    for (var i = 0; i < kids.length; i++) {
      kids[i].style.setProperty('--i', Math.min(i, 12));
    }
  }

  /* 动画结束后移除武装类，避免残留规则压制 hover 交互（清理延时含 --d 入场延迟） */
  function scheduleCleanup(el, isStagger) {
    var extra = 0;
    var d = el.style.getPropertyValue('--d');
    if (d) { var ms = parseInt(d, 10); if (ms > 0 && ms < 3000) extra = ms; }
    setTimeout(function () {
      if (!el.isConnected) return;
      if (isStagger) {
        el.classList.remove('stagger', 'stagger-on');
        var kids = el.children;
        for (var i = 0; i < kids.length; i++) kids[i].style.removeProperty('--i');
      } else {
        el.classList.remove('reveal', 'revealed', 'reveal-left', 'reveal-right', 'reveal-scale');
        el.style.removeProperty('--d');
      }
    }, (isStagger ? 1500 : 900) + extra);
  }

  function ensureObserver() {
    if (io || !('IntersectionObserver' in window)) return;
    io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        if (!e.isIntersecting) continue;
        var el = e.target;
        io.unobserve(el);
        if (el.classList.contains('stagger')) {
          numberChildren(el);
          el.classList.add('stagger-on');
          scheduleCleanup(el, true);
        } else {
          el.classList.add('revealed');
          scheduleCleanup(el, false);
        }
      }
    }, { threshold: 0.01, rootMargin: '0px 0px -8% 0px' });
  }

  function revealAll(root) {
    var scope = root || document;
    var all = scope.querySelectorAll('.reveal:not(.revealed),.stagger:not(.stagger-on)');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.classList.contains('stagger')) { numberChildren(el); el.classList.add('stagger-on'); scheduleCleanup(el, true); }
      else { el.classList.add('revealed'); scheduleCleanup(el, false); }
    }
    if (scope !== document && scope.nodeType === 1 && scope.matches &&
        scope.matches('.reveal:not(.revealed),.stagger:not(.stagger-on)')) {
      if (scope.classList.contains('stagger')) { numberChildren(scope); scope.classList.add('stagger-on'); scheduleCleanup(scope, true); }
      else { scope.classList.add('revealed'); scheduleCleanup(scope, false); }
    }
  }

  function armScan(root) {
    if (!jsGate) return; // 无 .js 武装时元素本就可见
    var scope = root || document;
    if (motionOff() || !('IntersectionObserver' in window)) { revealAll(scope); return; }
    ensureObserver();
    compactObserved();
    var els = scope.querySelectorAll('.reveal:not(.revealed),.stagger:not(.stagger-on)');
    for (var i = 0; i < els.length; i++) { io.observe(els[i]); observed.push(els[i]); }
    if (scope !== document && scope.nodeType === 1 && scope.matches &&
        scope.matches('.reveal:not(.revealed),.stagger:not(.stagger-on)')) {
      io.observe(scope); observed.push(scope);
    }
  }

  /* ═══ 2. 数字滚动：.count-up[data-target="120"][data-suffix="题"][data-duration="900"] ═══ */
  var cio = null;
  var cioObserved = [];

  function compactCio() {
    if (!cio) { cioObserved = []; return; }
    cioObserved = compactList(cioObserved, cio);
  }

  function parseTarget(el) {
    var raw = (el.getAttribute('data-target') || '').replace(/[^\d.\-]/g, '');
    var v = parseFloat(raw);
    if (isNaN(v)) return null;
    var dec = raw.indexOf('.') >= 0 ? (raw.split('.')[1] || '').length : 0;
    return { value: v, decimals: Math.min(dec, 4) };
  }

  function setFinal(el, t) {
    el.textContent = t.value.toFixed(t.decimals) + (el.getAttribute('data-suffix') || '');
  }

  function countUp(el, t) {
    var dur = parseInt(el.getAttribute('data-duration') || '900', 10);
    if (!(dur > 0)) dur = 900;
    var t0 = null;
    function frame(ts) {
      if (!el.isConnected) return;
      if (motionOff()) { setFinal(el, t); return; }
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1);
      if (p >= 1) { setFinal(el, t); return; }
      var ease = 1 - Math.pow(1 - p, 3); // easeOutCubic
      el.textContent = (t.value * ease).toFixed(t.decimals) + (el.getAttribute('data-suffix') || '');
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function ensureCio() {
    if (cio || !('IntersectionObserver' in window)) return;
    cio = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        if (!e.isIntersecting || e.target.classList.contains('counted')) continue;
        e.target.classList.add('counted');
        cio.unobserve(e.target);
        var t = parseTarget(e.target);
        if (t) countUp(e.target, t);
      }
    }, { threshold: 0.01 });
  }

  function scanCountUp(root) {
    var scope = root || document;
    var els = scope.querySelectorAll('.count-up[data-target]:not(.counted)');
    var list = [];
    for (var i = 0; i < els.length; i++) list.push(els[i]);
    if (scope !== document && scope.nodeType === 1 && scope.matches &&
        scope.matches('.count-up[data-target]:not(.counted)')) list.push(scope);
    if (!list.length) return;
    ensureCio();
    compactCio();
    for (var j = 0; j < list.length; j++) {
      var el = list[j];
      var t = parseTarget(el);
      el.classList.add('counted');
      if (!t) continue; // 非法 target：标记跳过，保持元素原文本
      if (motionOff() || !cio) { setFinal(el, t); continue; }
      el.classList.remove('counted'); // 交由 observer 触发时再标记
      cio.observe(el);
      cioObserved.push(el);
    }
  }

  /* ═══ 3. 指针涟漪：.btn-ripple（鼠标+触摸+键盘一致体验） ═══ */
  function spawnRipple(btn, x, y) {
    var rect = btn.getBoundingClientRect();
    var size = Math.max(rect.width, rect.height) * 1.2;
    var r = document.createElement('span');
    r.className = 'ripple';
    r.style.width = r.style.height = size + 'px';
    r.style.left = (x - rect.left - size / 2) + 'px';
    r.style.top = (y - rect.top - size / 2) + 'px';
    btn.appendChild(r);
    setTimeout(function () { if (r.parentNode) r.parentNode.removeChild(r); }, 650);
  }

  function bindRipple() {
    if (!jsGate) return; // 无 .js 时保留 theme.css 的 :active 固定中心涟漪
    document.addEventListener('pointerdown', function (ev) {
      if (motionOff()) return;
      var btn = ev.target && ev.target.closest ? ev.target.closest('.btn-ripple') : null;
      if (btn) spawnRipple(btn, ev.clientX, ev.clientY);
    }, { passive: true });
    document.addEventListener('keydown', function (ev) {
      if (motionOff() || ev.repeat) return;
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      var btn = ev.target && ev.target.closest ? ev.target.closest('.btn-ripple') : null;
      if (!btn) return;
      var rect = btn.getBoundingClientRect();
      spawnRipple(btn, rect.left + rect.width / 2, rect.top + rect.height / 2);
    });
  }

  /* ═══ 4. 图片载入淡入：img.fade-load（含失败兜底） ═══ */
  function markImg(img) { img.classList.add('loaded'); }

  function imgCheck(root) {
    if (!jsGate) return;
    var scope = root || document;
    var imgs = scope.querySelectorAll('img.fade-load:not(.loaded)');
    for (var i = 0; i < imgs.length; i++) {
      if (imgs[i].complete) markImg(imgs[i]); // 缓存图/detached 已加载图
    }
    if (scope !== document && scope.nodeType === 1 && scope.tagName === 'IMG' &&
        scope.classList.contains('fade-load') && !scope.classList.contains('loaded') && scope.complete) {
      markImg(scope);
    }
  }

  function bindImgFade() {
    if (!jsGate) return;
    // capture：img 的 load/error 不冒泡
    document.addEventListener('load', function (ev) {
      if (ev.target && ev.target.tagName === 'IMG' && ev.target.classList.contains('fade-load')) markImg(ev.target);
    }, true);
    document.addEventListener('error', function (ev) {
      if (ev.target && ev.target.tagName === 'IMG' && ev.target.classList.contains('fade-load')) markImg(ev.target);
    }, true);
  }

  /* ═══ 5. DOM 监听：动态新增节点自动接管（过滤自身 ripple 噪音） ═══ */
  function watchDom() {
    if (!('MutationObserver' in window)) return;
    var timer = null;
    var roots = []; // 累积式防抖：窗口期内的批次合并扫描，不丢弃
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var nodes = muts[i].addedNodes;
        for (var j = 0; j < nodes.length; j++) {
          var n = nodes[j];
          if (n.nodeType !== 1) continue;
          if (n.classList && n.classList.contains('ripple')) continue; // 自身涟漪不触发扫描
          roots.push(n);
        }
      }
      if (!roots.length || timer) return;
      timer = setTimeout(function () {
        timer = null;
        var batch = roots; roots = [];
        for (var k = 0; k < batch.length; k++) {
          if (!batch[k].isConnected) continue;
          safe(function () { armScan(batch[k]); });
          safe(function () { scanCountUp(batch[k]); });
          safe(function () { imgCheck(batch[k]); });
        }
      }, 60);
    }).observe(document.body, { childList: true, subtree: true });
  }

  /* ═══ 6. 模式切换监听：quick-mode / reduced-motion 运行时切换时补救 ═══ */
  function watchModeSwitch() {
    if (!('MutationObserver' in window)) return;
    new MutationObserver(function () {
      safe(function () { if (motionOff()) revealAll(document); else armScan(document); });
      safe(function () { scanCountUp(document); });
      safe(function () { imgCheck(document); });
    }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    if (mq) {
      var onMq = function () {
        safe(function () { if (mq.matches) revealAll(document); else armScan(document); });
      };
      if (mq.addEventListener) mq.addEventListener('change', onMq);
      else if (mq.addListener) mq.addListener(onMq);
    }
  }

  /* ═══ 启动（各项独立容错，永不早退） ═══ */
  function init() {
    safe(function () { armScan(document); });
    safe(function () { scanCountUp(document); });
    safe(function () { imgCheck(document); });
    safe(watchDom);
    safe(bindRipple);
    safe(bindImgFade);
    safe(watchModeSwitch);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  /* 暴露手动触发接口：动态渲染大量内容后可调用 MotionFX.rescan(container) */
  window.MotionFX = {
    rescan: function (root) {
      var scope = root || document;
      safe(function () { armScan(scope); });
      safe(function () { scanCountUp(scope); });
      safe(function () { imgCheck(scope); });
    }
  };
})();
