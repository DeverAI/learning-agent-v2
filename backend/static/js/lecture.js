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
      setStatus('计划生成失败：' + (e.message || '未知错误'));
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
      setStatus('计划生成失败：' + (e.message || '未知错误'));
    });
  }

  function renderAllSteps() {
    var ol = $('stepList'); ol.innerHTML = '';
    state.plans.forEach(function (plan, pi) {
      (plan.steps || []).forEach(function (s, si) {
        var li = document.createElement('li');
        li.textContent = (plan.number ? '第' + plan.number + '题 · ' : '') + s.title;
        li.dataset.pi = pi; li.dataset.si = si;
        ol.appendChild(li);
      });
      if (plan.error) {
        var li = document.createElement('li');
        li.textContent = '第' + plan.number + '题：生成失败（' + plan.error + '）';
        li.style.color = '#c00';
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
    $('lecPos').textContent = c ? ('· 第' + (state.planIdx + 1) + '题/' + state.plans.length
      + ' 第' + (state.stepIdx + 1) + '步') : '';
  }

  function speakText(text) {
    if (!$('autoSpeak').checked || !window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(text);
    u.lang = 'zh-CN'; u.rate = 1.0;
    window.speechSynthesis.speak(u);
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
    var pi = state.planIdx, si = state.stepIdx + delta;
    while (si < 0 || (state.plans[pi] && si >= (state.plans[pi].steps || []).length)) {
      if (delta > 0) { pi++; if (pi >= state.plans.length) { setStatus('全部讲完'); return; } si = 0; }
      else { pi--; if (pi < 0) { return; } pi = Math.min(pi, state.plans.length - 1);
             si = (state.plans[pi].steps || []).length - 1; }
    }
    enterStep(pi, si);
  }

  window.LectureApp = {
    start: function () {
      window.speechSynthesis = window.speechSynthesis || window.speechSynthesis;
      if ($('paperSelect').value) { loadPaperPlans(); }
      else if ($('questionSelect').value) { loadSinglePlan(); }
      else { setStatus('请先选择试卷或题目'); }
    },
    prev: function () { advance(-1); },
    next: function () { advance(1); },
    speakCurrent: function () {
      var c = currentStep();
      if (c) speakText(c.step.speech); else setStatus('先选择试卷或题目生成计划');
    },
    stop: function () {
      if (window.speechSynthesis) window.speechSynthesis.cancel();
      setStatus('已停止');
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
