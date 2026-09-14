/* 讲题页（round 60）：试卷/题目清单 → 讲解计划 → 浏览器 TTS 逐步朗读 + 整卷连讲 */
(function () {
  var state = { plans: [], planIdx: -1, stepIdx: -1, speaking: false };

  function $(id) { return document.getElementById(id); }
  function setStatus(t) { var e = $('lecStatus'); if (e) e.textContent = t; }

  function loadLists() {
    $API.get('/api/lecture/papers').then(function (papers) {
      var sel = $('paperSelect'); sel.innerHTML = '<option value="">选择试卷（可整卷连讲）</option>';
      (papers || []).forEach(function (p) {
        var o = document.createElement('option');
        o.value = p.id; o.textContent = p.title + '（' + p.question_count + '题）';
        sel.appendChild(o);
      });
      if (!(papers || []).length) sel.innerHTML = '<option value="">（题库中还没有试卷）</option>';
    }).catch(function (e) {
      $('paperSelect').innerHTML = '<option value="">试卷加载失败（刷新重试）</option>';
    });
    $API.get('/api/lecture/questions').then(function (qs) {
      var sel = $('questionSelect'); sel.innerHTML = '<option value="">选择单题讲解</option>';
      (qs || []).forEach(function (q) {
        var o = document.createElement('option');
        o.value = q.id; o.textContent = '第' + q.number + '题 ' + q.title;
        sel.appendChild(o);
      });
      if (!(qs || []).length) sel.innerHTML = '<option value="">（题库中还没有已完成的题目）</option>';
    }).catch(function () {
      $('questionSelect').innerHTML = '<option value="">题目加载失败（刷新重试）</option>';
    });
  }

  function loadPaperPlans() {
    var pid = $('paperSelect').value;
    if (!pid) return;
    setStatus('正在生成整卷讲解计划（AI 逐题处理，长卷约需 1-3 分钟）...');
    $('btnPlan').disabled = true;
    $API.post('/api/lecture/plan-paper', { paper_id: pid }).then(function (data) {
      $('btnPlan').disabled = false;
      state.plans = data.plans || [];
      state.planIdx = -1; state.stepIdx = -1;
      setStatus('整卷计划就绪：' + data.count + ' 题，点「下一步」开始逐题讲解');
      renderAllSteps();
    }).catch(function (e) {
      $('btnPlan').disabled = false;
      setStatus('计划生成失败：' + ($errText(e)|| '未知错误'));
    });
  }

  function loadSinglePlan() {
    var qid = $('questionSelect').value;
    if (!qid) return;
    setStatus('正在生成讲解计划...');
    $('btnPlan').disabled = true;
    $API.post('/api/lecture/plan/' + qid, {}).then(function (data) {
      $('btnPlan').disabled = false;
      state.plans = [data]; state.planIdx = -1; state.stepIdx = -1;
      setStatus('单题计划就绪（' + (data.steps || []).length + ' 步）');
      renderAllSteps();
    }).catch(function (e) {
      $('btnPlan').disabled = false;
      setStatus('计划生成失败：' + ($errText(e)|| '未知错误'));
    });
  }

  function renderAllSteps() {
    var ol = $('stepList'); ol.innerHTML = '';
    state.plans.forEach(function (plan, pi) {
      (plan.steps || []).forEach(function (s, si) {
        var li = document.createElement('li');
        li.textContent = (plan.number ? '第' + plan.number + '题 · ' : '') + s.title;
        li.dataset.pi = pi; li.dataset.si = si;
        // 原实现只写 dataset，全页无任何监听（两个 addEventListener 都绑在下拉框上），
        // 而 highlight() 还会给条目加选中底色 → 视觉上暗示可点、实际点不动。
        li.style.cursor = 'pointer';
        li.title = '点击从这一步开始讲解';
        li.addEventListener('click', function () { enterStep(pi, si); });
        ol.appendChild(li);
      });
      if (plan.error) {
        var li = document.createElement('li');
        li.textContent = '第' + plan.number + '题：生成失败（' + plan.error + '）';
        li.style.color = 'var(--err, #c0392b)';
        ol.appendChild(li);
      }
    });
  }

  function currentStep() {
    var plan = state.plans[state.planIdx];
    if (!plan) return null;
    var steps = plan.steps || [];
    return steps[state.stepIdx] ? { step: steps[state.stepIdx],
      pi: state.planIdx, si: state.stepIdx } : null;
  }

  function highlight() {
    var items = $('stepList').children;
    for (var i = 0; i < items.length; i++) {
      var on = (items[i].dataset.pi == state.planIdx && items[i].dataset.si == state.stepIdx);
      items[i].style.background = on ? 'rgba(80,140,255,.15)' : '';
      items[i].style.fontWeight = on ? '600' : '';
    }
    var c = currentStep();
    var posTxt = c ? ('· 第' + (state.planIdx + 1) + '题/' + state.plans.length
      + ' 第' + (state.stepIdx + 1) + '步') : '';
    if ($('lecPos')) $('lecPos').textContent = posTxt;
    if ($('lecBoardPos')) $('lecBoardPos').textContent = posTxt.replace(/^· /, '');
    // R31：手机讲课主视图是黑板，当前步骤必须写进板，不能只高亮列表
    var board = $('lecBoard');
    if (board) {
      if (!c || !c.step) {
        board.innerHTML = '<div class="board-empty">尚未开始讲解</div>';
      } else {
        var title = c.step.title ? ('### ' + c.step.title + '\n\n') : '';
        var body = c.step.content || c.step.text || c.step.detail || '';
        if (!body && c.step.title) body = c.step.title;
        var src = title + (body || '');
        var holder = document.createElement('div');
        holder.className = 'board-text';
        if (typeof $md === 'function') $md(src, holder);
        else holder.textContent = src;
        board.innerHTML = '';
        board.appendChild(holder);
        board.scrollTop = 0;
      }
    }
  }

  function speakText(text) {
    if (!text) return;
    if (window._lecPaused) return;
    if (!$('autoSpeak').checked) return;
    // Android WebView：SpeechSynthesis 不可用，通过原生 TTS 桥朗读（round 60 A1）
    if (window.AndroidTTS && typeof window.AndroidTTS.speak === 'function') {
      window.AndroidTTS.speak(text);
      return;
    }
    if (window.speechSynthesis) {
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN'; u.rate = 1.0;
      window.speechSynthesis.speak(u);
      return;
    }
    // 两个分支都不可用时，原实现**直接落到函数末尾**：无 else、无 setStatus、无 toast、
    // 无 console.warn —— 在多数 Android WebView（无 speechSynthesis、也无注入桥）里，
    // 点「朗读本步」表现为"点了没声、页面也没提示"。这里给出明确反馈。
    // 注意：本页不调用服务端 /api/tts（对比 focus.js 的双引擎兜底），故无其它退路。
    setStatus('当前环境不支持语音朗读（既无原生桥也无 speechSynthesis），可改用下方讲解文本阅读');
  }

  function stopSpeaking() {
    if (window.AndroidTTS && typeof window.AndroidTTS.stop === 'function') {
      window.AndroidTTS.stop();
    }
    // 原实现在此处**无条件调用自身** stopSpeaking()，既无循环变量也无 base case →
    // 任何调用点都会抛 RangeError: Maximum call stack size exceeded；且 LectureApp.stop()
    // 里的 setStatus('已停止') 永远执行不到，浏览器侧朗读也停不掉。
    // 正确行为是取消浏览器侧排队中的朗读。
    try {
      if (window.speechSynthesis && typeof window.speechSynthesis.cancel === 'function') {
        window.speechSynthesis.cancel();
      }
    } catch (e) { /* 个别环境无 cancel，忽略即可 */ }
  }

  function enterStep(pi, si) {
    state.planIdx = pi; state.stepIdx = si;
    var c = state.plans[pi] && state.plans[pi].steps[si];
    if (!c) return;
    highlight();
    setStatus('第' + (pi + 1) + '题 · ' + c.title);
    speakText(c.speech);
  }

  function advance(delta) {
    if (!state.plans.length) return;
    // 未开始状态（planIdx<0）：起点统一定为第 1 题第 1 步。
    // 原实现让 pi=-1、si=0 直接进 enterStep(-1,0)，而 enterStep 内部因 `state.plans[-1]`
    // 为 undefined 立即 return；且 while 条件里的 `state.plans[pi] && ...` 在 pi=-1 时
    // 整体短路为 false，连循环都进不去 → "上一步/下一步"**永远空操作**（已确证）。
    // 这是讲题页整条链路（朗读/自动朗读/停止）不可达的根因，必须先修这里。
    if (state.planIdx < 0) {
      if (delta < 0) return;          // 还没开始，"上一步"无事可做
      enterStep(0, 0);
      return;
    }
    var pi = state.planIdx, si = state.stepIdx + delta;
    while (si < 0 || (state.plans[pi] && si >= (state.plans[pi].steps || []).length)) {
      if (delta > 0) { pi++; if (pi >= state.plans.length) { setStatus('全部讲完'); return; } si = 0; }
      else { pi--; if (pi < 0) { return; } pi = Math.min(pi, state.plans.length - 1);
             si = (state.plans[pi].steps || []).length - 1; }
    }
    enterStep(pi, si);
  }

  // ===== 边看边问（R23 / R38 修正入口）=====
  // 原先走 /api/chat：那是**另一套旧分类器**，没有课稿/导入/补漏等工具，
  // 学生在讲课页问「帮我备课」会得到弱回答。改为走与 Agent 页相同的
  // /api/sessions/{sid}/chat，拿到完整工具注册表。
  var _lecSid = '';
  function _ensureLectureSession() {
    if (_lecSid) return Promise.resolve(_lecSid);
    var key = 'la_lecture_sid';
    try { var saved = localStorage.getItem(key); if (saved) { _lecSid = saved; return Promise.resolve(saved); } } catch (e) {}
    return window.$API.post('/api/sessions', { title: '讲课提问' }).then(function (s) {
      _lecSid = s && s.id || '';
      try { if (_lecSid) localStorage.setItem(key, _lecSid); } catch (e) {}
      return _lecSid;
    });
  }
  function askAboutStep() {
    var inp = $('lecAskInput');
    var q = (inp && inp.value || '').trim();
    if (!q) { setStatus('先输入问题再点「问」'); return; }
    var area = $('lecAskArea');
    if (!area) return;
    var ctx = '';
    var c = currentStep();
    if (c) {
      var plan = state.plans[c.pi] || {};
      var head = (plan.number ? ('第' + plan.number + '题 ') : '') + (c.step.title || '');
      ctx = '我正在看「' + head + '」这一步的讲解，讲解内容：'
        + String(c.step.speech || c.step.text || '').slice(0, 1200) + '。';
    } else {
      ctx = '我正在看讲题页面（还没有选中讲解步骤）。';
    }
    var uq = document.createElement('div');
    uq.style.cssText = 'color:var(--accent);margin:6px 0';
    uq.textContent = '[问] ' + q;
    area.appendChild(uq);
    var ld = document.createElement('div'); ld.innerHTML = '<span class="spin"></span>';
    area.appendChild(ld);
    inp.value = '';
    var btn = $('btnAsk'); if (btn) btn.disabled = true;
    _ensureLectureSession().then(function (sid) {
      if (!sid) throw new Error('讲课会话创建失败');
      return window.$API.post('/api/sessions/' + encodeURIComponent(sid) + '/chat',
        { message: ctx + '我的问题：' + q });
    }).then(function (r) {
      ld.remove();
      var ad = document.createElement('div'); ad.className = 'agent-msg-ai';
      var md = document.createElement('div'); md.style.cssText = 'font-size:13px';
      ad.appendChild(md); area.appendChild(ad);
      $md((r && r.reply) || '', md);
      area.scrollTop = area.scrollHeight;
    }).catch(function (e) {
      ld.remove();
      var ed = document.createElement('div'); ed.style.cssText = 'color:var(--err)';
      ed.textContent = $errText(e, '提问失败');
      area.appendChild(ed);
    }).finally(function () { if (btn) btn.disabled = false; });
  }

  window.LectureApp = {
    ask: askAboutStep,
    start: function () {
      // 原为 `window.speechSynthesis = window.speechSynthesis || window.speechSynthesis`：
      // 等式两侧同一表达式，赋值结果与不执行完全等价，是纯粹的无效语句（G9），已删除。
      if ($('paperSelect').value) { loadPaperPlans(); }
      else if ($('questionSelect').value) { loadSinglePlan(); }
      else { setStatus('请先选择试卷或题目'); }
    },
    prev: function () { advance(-1); },
    next: function () { advance(1); },
    speakCurrent: function () {
      var c = currentStep();
      // 原文案是"先选择试卷或题目生成计划"，但计划已生成时也会走到这里（currentStep 恒 null），
      // 提示与事实不符。改为如实说明当前状态。
      if (c) speakText(c.step.speech); else setStatus('当前还没有开始讲解，点「下一步」开始');
    },
    stop: function () {
      stopSpeaking();
      setStatus('已停止');
    },
    // R35：黑板暂停/继续 —— 停朗读，不推进步骤
    togglePauseBoard: function () {
      var btn = $('btnPauseBoard');
      if (window._lecPaused) {
        window._lecPaused = false;
        if (btn) btn.textContent = '暂停';
        setStatus('继续');
        speakText((currentStep() && currentStep().step && currentStep().step.speech) || '');
      } else {
        window._lecPaused = true;
        stopSpeaking();
        if (btn) btn.textContent = '继续';
        setStatus('已暂停朗读（步骤停在当前）');
      }
    }
  };

  document.addEventListener('DOMContentLoaded', function () {
    loadLists();
    $('paperSelect').addEventListener('change', function () {
      if (this.value) $('questionSelect').value = '';
    });
    $('questionSelect').addEventListener('change', function () {
      if (this.value) $('paperSelect').value = '';
    });
  });
})();
