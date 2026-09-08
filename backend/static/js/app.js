// app.js - 全局工具函数与交互逻辑（从 base.html 提取）
// 此文件由 base.html 通过 <script defer> 引入

// ===== KaTeX auto-render 简化版 =====
// 不依赖 CDN 的 auto-render.min.js，避免网络失败导致公式不渲染
window._renderMathInElement = function(element, options){
  if(typeof katex === 'undefined' || !element) return;
  options = options || {};
  var delims = options.delimiters || [{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false},{left:'\\(',right:'\\)',display:false},{left:'\\[',right:'\\]',display:true}];
  var skipTags = {'script':1,'style':1,'pre':1,'code':1,'textarea':1};
  function walk(node){
    if(node.nodeType === 3){
      var text = node.textContent;
      if(!/\$|\\\(|\\\[/.test(text)) return;
      var best = null, bestIdx = Infinity;
      for(var i=0;i<delims.length;i++){
        var idx = text.indexOf(delims[i].left);
        if(idx !== -1 && idx < bestIdx){
          var end = text.indexOf(delims[i].right, idx + delims[i].left.length);
          if(end !== -1){bestIdx = idx; best = {idx: idx, end: end, delim: delims[i]};}
        }
      }
      if(best){
        var before = text.slice(0, best.idx);
        var math = text.slice(best.idx + best.delim.left.length, best.end);
        var after = text.slice(best.end + best.delim.right.length);
        var span = document.createElement(best.delim.display ? 'div' : 'span');
        span.style.display = best.delim.display ? 'block' : 'inline';
        try{span.innerHTML = katex.renderToString(math, {throwOnError: false, displayMode: !!best.delim.display});}catch(e){span.textContent = text.slice(best.idx, best.end + best.delim.right.length);}
        var parent = node.parentNode;
        if(before) parent.insertBefore(document.createTextNode(before), node);
        parent.insertBefore(span, node);
        if(after) parent.insertBefore(document.createTextNode(after), node);
        parent.removeChild(node);
        if(before) walk(parent.childNodes[Array.from(parent.childNodes).indexOf(span)-1]);
        if(after) walk(parent.childNodes[Array.from(parent.childNodes).indexOf(span)+1]);
      }
    }else if(node.nodeType === 1){
      var tag = node.tagName.toLowerCase();
      if(skipTags[tag] || node.classList.contains('katex')) return;
      Array.from(node.childNodes).forEach(walk);
    }
  }
  walk(element);
};

// ===== 统一关闭图标 SVG（替代 &times;，保持风格一致） =====
window._closeIconSvg = function(){
  return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg>';
};

// ===== 侧边栏切换 =====
function toggleSidebar(){
  var s=document.getElementById('sideBar');
  var o=document.getElementById('sidebarOverlay');
  if(!s) return;
  var willOpen=!s.classList.contains('open');
  s.classList.toggle('open',willOpen);
  if(o) o.classList.toggle('show',willOpen);
  document.body.classList.toggle('sb-closed',!willOpen);
  document.body.classList.toggle('nav-open',willOpen&&window.innerWidth<=768);
  // 桌面端记录用户收起偏好，下次进入页面时尊重选择
  if(window.innerWidth>768){
    try{localStorage.setItem('sb_user_closed', willOpen?'false':'true')}catch(e){}
  }
  document.querySelectorAll('#sidebarToggle,#mobileSidebarToggle').forEach(function(b){b.setAttribute('aria-expanded',willOpen?'true':'false')});
}
// 桌面端默认打开侧边栏（守卫防止 base.html 结构异常导致 $API/$toast 初始化中断；
// 仅在用户此前未手动收起时才默认打开，保留用户选择）
(function(){
  var sb=document.getElementById('sideBar');
  if(!sb) return;
  if(window.innerWidth>768){
    var userClosed=localStorage.getItem('sb_user_closed')==='true';
    if(userClosed){
      // 用户上次明确收起：回填 sb-closed 让 main 区域宽度复位，避免 200px 留白
      document.body.classList.add('sb-closed');
    }else{
      sb.classList.add('open');
      document.body.classList.remove('sb-closed');
      // 同步 ARIA 展开态
      document.querySelectorAll('#sidebarToggle,#mobileSidebarToggle').forEach(function(b){b.setAttribute('aria-expanded','true')});
    }
  }
})();
// 移动端点击导航链接后自动关闭侧边栏
document.querySelectorAll('.sidebar-nav a').forEach(function(a){
  a.addEventListener('click',function(){
    if(window.innerWidth<=768)toggleSidebar();
  });
});
function toggleMenu(id){
  var sub=document.getElementById(id);
  var arrow=document.getElementById('arrow_'+id);
  sub.classList.toggle('open');
  arrow.classList.toggle('open');
}

// ===== API 封装 =====
window.$API = {
  _authToken: "Ntmhzsgtc",
  _friendlyStatus: function(status, rawText){
    // 将 HTTP 状态码映射为更友好的中文提示，避免英文透出
    if(status===0 || /Failed to fetch|NetworkError|net::ERR/i.test(rawText||'')) return '网络异常，请检查连接';
    if(status===401) return '身份验证失败（401）';
    if(status===403) return '没有权限（403）';
    if(status===404) return '资源不存在（404）';
    if(status===409) return '状态冲突（409）';
    if(status===429) return '请求过于频繁，请稍后再试';
    if(status>=500 && status<600) return '服务繁忙（'+status+'），请稍后重试';
    if(status>=400) return '请求失败（'+status+'）';
    return '请求失败';
  },
  _parse: function(r,type){
    if(!r.ok){
      return r.text().then(function(t){
        var detail='';
        try{
          var d=JSON.parse(t);
          if(typeof d.detail==='string')detail=d.detail;
          else if(d.detail&&typeof d.detail==='object')detail=d.detail.message||JSON.stringify(d.detail);
          else detail=d.message||'';
        }catch(e){}
        if(detail) throw new Error(detail);
        throw new Error(window.$API._friendlyStatus(r.status, t)+' '+(t||'').slice(0,80));
      });
    }
    if(type==='json')return r.json();return r.text()
  },
  _request: function(u,opts,type){
    window.dispatchEvent(new CustomEvent('ux:request',{detail:{active:true,url:u}}));
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers['X-Auth-Token'] = window.$API._authToken;
    // 离线增强层（offline.js）：GET 白名单走缓存回退，上传白名单离线入队自动重放。
    // 在线路径行为完全不变；离线层未加载/关闭时直接走原逻辑。
    var doFetchRaw = function(){
      return fetch(u,opts).catch(function(err){
        // fetch 网络层失败（DNS、断网、CORS 拒绝）：打 isNetwork 标记供离线层区分
        // （HTTP 4xx/5xx 是服务器明确响应，不得回退离线缓存掩盖错误）
        var msg=(err&&err.message)||'';
        var e = new Error(window.$API._friendlyStatus(0, msg));
        e.isNetwork = true;
        throw e;
      });
    };
    var doParse = function(r){ return window.$API._parse(r, type||'json'); };
    var p = (window._offline && typeof window._offline.handle === 'function')
      ? window._offline.handle(u, opts, type, doFetchRaw, doParse)
      : doFetchRaw().then(doParse);
    return p.finally(function(){window.dispatchEvent(new CustomEvent('ux:request',{detail:{active:false,url:u}}))});
  },
  get: function(u){return window.$API._request(u,{},'json')},
  post: function(u,d){return window.$API._request(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)},'json')},
  upload: function(u,f){return window.$API._request(u,{method:'POST',body:f},'json')},
  put: function(u,d){return window.$API._request(u,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)},'json')},
  patch: function(u,d){return window.$API._request(u,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)},'json')},
  del: function(u){return window.$API._request(u,{method:'DELETE'},'json')},
  delete: function(u){return window.$API._request(u,{method:'DELETE'},'json')}
};

// ===== 格式化与标签函数 =====
function $fmt(t){if(!t)return'-';var d=new Date(t);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}
function $status(s){var m={done:'<span class="badge badge-done">OK</span>',staged:'<span class="badge badge-pending">Staged</span>',pending:'<span class="badge badge-pending">...</span>',processing:'<span class="badge badge-pending">...</span>',generating_solution:'<span class="badge badge-pending">...</span>',error:'<span class="badge badge-error">ERR</span>'};return m[s]||'<span class="badge">'+s+'</span>'}
function $type(t){var m={custom:'自定义',regular_paper:'平时卷',collection_paper:'集合卷',mock_exam:'模拟卷',final_exam:'压轴卷',topic_exam:'专题卷'};return '<span class="tag">'+(m[t]||t)+'</span>'}

// ===== Toast 通知系统 =====
var _toastIcons={ok:'OK',error:'!',warn:'!',info:'i'};
function $toast(m,t){
  t=(t==='error'||t==='ok'||t==='warn')?t:'info';
  var c=document.getElementById('toastContainer');
  if(!c){c=document.createElement('div');c.id='toastContainer';document.body.appendChild(c)}
  while(c.children.length>=4)c.firstChild.remove();
  var e=document.createElement('div');
  e.className='toast-item';e.dataset.type=t;
  var ic=document.createElement('span');ic.className='toast-icon';ic.textContent=_toastIcons[t];
  var tx=document.createElement('span');tx.textContent=m;
  e.appendChild(ic);e.appendChild(tx);
  e.onclick=function(){_dismissToast(e)};
  c.appendChild(e);
  // 功能检查轮 UX 修复（M5）：error toast 不自动消失（用户需看到失败原因，
  // 连续失败挤掉历史的问题保留上限 4 条兜底），成功/警告类维持自动关闭
  if(t!=='error')setTimeout(function(){_dismissToast(e)},t==='warn'?6000:2800);
}
function _dismissToast(e){
  if(!e||e._dismissed)return;e._dismissed=true;
  e.classList.add('toast-out');
  setTimeout(function(){e.remove()},260);
}

// ===== Promise 风格确认弹窗 =====
// 用法：if(!await $confirm('确定删除？',{danger:true}))return;
function $confirm(msg,opts){
  opts=opts||{};
  return new Promise(function(resolve){
    document.querySelectorAll('.confirm-overlay').forEach(function(o){o.remove()});
    var ov=document.createElement('div');
    ov.className='modal-overlay show confirm-overlay';
    var box=document.createElement('div');
    box.className='modal confirm-box';
    box.setAttribute('role','dialog');box.setAttribute('aria-modal','true');
    var h=document.createElement('h3');h.textContent=opts.title||'确认操作';
    var p=document.createElement('div');p.className='confirm-msg';p.textContent=msg;
    var acts=document.createElement('div');acts.className='confirm-actions';
    var cancel=document.createElement('button');cancel.className='btn btn-sm';cancel.textContent=opts.cancelText||'取消';
    var ok=document.createElement('button');ok.className='btn btn-sm btn-confirm-ok'+(opts.danger?' danger':'');ok.textContent=opts.okText||'确认';
    acts.appendChild(cancel);acts.appendChild(ok);
    box.appendChild(h);box.appendChild(p);box.appendChild(acts);
    ov.appendChild(box);document.body.appendChild(ov);
    function done(v){ov.remove();document.removeEventListener('keydown',onKey,true);resolve(v)}
    function onKey(e){
      if(e.key==='Escape'){e.stopPropagation();done(false)}
      else if(e.key==='Enter'){e.preventDefault();e.stopPropagation();done(true)}
    }
    cancel.onclick=function(){done(false)};
    ok.onclick=function(){done(true)};
    ov.onclick=function(e){if(e.target===ov)done(false)};
    document.addEventListener('keydown',onKey,true);
    ok.focus();
  });
}

// ===== Promise 风格输入弹窗（替代 window.prompt） =====
// 用法：var v = await $prompt('输入名称:', {default:'默认', title:'命名'});
// 返回输入字符串；取消则返回 null
function $prompt(msg, opts){
  opts=opts||{};
  return new Promise(function(resolve){
    document.querySelectorAll('.confirm-overlay').forEach(function(o){o.remove()});
    var ov=document.createElement('div');
    ov.className='modal-overlay show confirm-overlay';
    var box=document.createElement('div');
    box.className='modal confirm-box';
    box.setAttribute('role','dialog');box.setAttribute('aria-modal','true');
    var h=document.createElement('h3');h.textContent=opts.title||'输入';
    var p=document.createElement('div');p.className='confirm-msg';p.textContent=msg;
    var input=document.createElement('input');
    input.className='form-input';
    input.value=opts.default!=null?opts.default:'';
    if(opts.placeholder)input.placeholder=opts.placeholder;
    input.style.cssText='width:100%;margin:8px 0;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:14px';
    var acts=document.createElement('div');acts.className='confirm-actions';
    var cancel=document.createElement('button');cancel.className='btn btn-sm';cancel.textContent=opts.cancelText||'取消';
    var ok=document.createElement('button');ok.className='btn btn-sm btn-primary';ok.textContent=opts.okText||'确定';
    acts.appendChild(cancel);acts.appendChild(ok);
    box.appendChild(h);box.appendChild(p);box.appendChild(input);box.appendChild(acts);
    ov.appendChild(box);document.body.appendChild(ov);
    function done(v){ov.remove();document.removeEventListener('keydown',onKey,true);resolve(v)}
    function onKey(e){
      if(e.key==='Escape'){e.stopPropagation();done(null)}
      else if(e.key==='Enter'){e.preventDefault();e.stopPropagation();done(input.value)}
    }
    cancel.onclick=function(){done(null)};
    ok.onclick=function(){done(input.value)};
    ov.onclick=function(e){if(e.target===ov)done(null)};
    document.addEventListener('keydown',onKey,true);
    input.focus();input.select();
  });
}

// ===== LaTeX 修复 =====
function _fixLatex(html){
  if(!html) return html;
  // 先保护 HTML 标签，避免正则误匹配到属性中的 _ 或 ^
  var tagHolders=[], tagIdx=0, saveTag=function(s){tagHolders.push(s);return '<!--TAG-'+(tagIdx++)+'-->';};
  html=html.replace(/<[^>]+>/g, saveTag);
  // 先保护代码块、行内代码、Markdown 链接、图片、粗体、斜体、删除线，避免把 ^ _ 误当成数学公式
  // 占位符使用 "-N" 而非 "_N"，防止被后续的 _/^ 数学正则误匹配
  var placeholders=[], idx=0, save=function(s){placeholders.push(s);return '<!--LTX-'+(idx++)+'-->';};
  // 把转义的 \$ 替换为 HTML 实体，防止后续被当成公式分隔符
  html=html.replace(/\\\$/g,'&#36;');
  html=html.replace(/```[\s\S]*?```/g,save);
  html=html.replace(/`[^`]+`/g,save);
  html=html.replace(/!\[[^\]]*\]\([^)]+\)/g,save);
  html=html.replace(/\[[^\]]*\]\([^)]+\)/g,save);
  html=html.replace(/(\*\*|__)[^*_]+(\*\*|__)/g,save);
  html=html.replace(/(\*|_)[^*_]+(\*|_)/g,save);
  html=html.replace(/~~[^~]+~~/g,save);
  // 1. 仅当文本中只有单个孤立 $ 时才移除，避免破坏合法的多 $ 公式
  var dollarCount=(html.match(/\$/g)||[]).length;
  if(dollarCount===1){
    // 若唯一的 $ 位于开头或末尾且独立存在，则移除
    html=html.replace(/^\$\s+|\s+\$$/g,'');
  }
  // 2. 对明确的数学片段自动补 $...$：仅对上标场景（^）做自动包裹；
  // 下划线 _ 极容易误伤 snake_case、Markdown 斜体等普通文本，不再自动补 $
  html=html.replace(/([^$\w])([a-zA-Z0-9]+\^[a-zA-Z0-9{}]+)([^$\w])/g,function(m,pre,math,post){
    return pre+'$'+math+'$'+post;
  });
  // 恢复占位
  html=html.replace(/<!--LTX-(\d+)-->/g,function(m,i){return placeholders[+i]||m;});
  // 恢复 HTML 标签
  html=html.replace(/<!--TAG-(\d+)-->/g,function(m,i){return tagHolders[+i]||m;});
  return html;
}

// ===== 统一 HTML 消毒（全站唯一实现；$md 与 questions.js 对比模式等共用） =====
function _sanitizeHtml(html){
  if(!html) return html;
  // 前置过滤：针对事件属性编码绕过（如 onerror&#61;）做防御性替换。
  // 只在疑似事件属性上下文中替换等号/引号等危险字符实体，减少误伤合法数字实体。
  html=html.replace(/(on\w+\s*)(&#(x?[0-9a-fA-F]+);)([^>]*>)/gi,function(m,prefix,entity,code,suffix){
    var c=code.charAt(0)==='x'||code.charAt(0)==='X'?parseInt(code.slice(1),16):parseInt(code,10);
    if(isNaN(c)) return m;
    if(c===61||c===34||c===39||c===96) return prefix+' '+suffix;
    return prefix+String.fromCharCode(c)+suffix;
  });
  html=html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi,'');
  html=html.replace(/on\w+\s*=\s*["']?[^"'>\s]*/gi,'');
  html=html.replace(/(?:href|src|srcset|action|formaction)\s*=\s*["']?\s*(?:javascript:|data:|vbscript:|file:|about:)[^"'>\s]*/gi,'');
  var div=document.createElement('div');div.innerHTML=html;
  var badTags=['script','style','iframe','object','embed','form','input','textarea','button','meta','link','base','marquee','animate','set','foreignObject'];
  badTags.forEach(function(tag){Array.from(div.getElementsByTagName(tag)).forEach(function(el){el.remove()})});
  Array.from(div.querySelectorAll('*')).forEach(function(el){
    for(var i=el.attributes.length-1;i>=0;i--){
      var name=el.attributes[i].name.toLowerCase();
      // 对齐浏览器 URL 解析：先剥离 tab/LF/CR 等控制字符再测协议，
      // 否则 href="java&#10;script:alert(1)" 经实体解码后可绕过锚定正则
      var val=(el.attributes[i].value||'').toLowerCase().replace(/[\t\n\r\f\v\0]/g,'').trim();
      if(name.indexOf('on')===0 || name==='srcdoc'){el.removeAttribute(name);continue}
      if((name==='href'||name==='xlink:href'||name==='src'||name==='srcset'||name==='action'||name==='formaction') && /^(javascript|data|vbscript|file|about):/.test(val)){
        el.removeAttribute(name);
      }
    }
  });
  return div.innerHTML;
}
window._sanitizeHtml=_sanitizeHtml;

// ===== Markdown 渲染 =====
function $md(t,e){
  if(typeof marked==='undefined'){console.warn('$md: marked not loaded, using fallback');e.innerHTML='<pre style="white-space:pre-wrap;font-size:13px">'+String(t||'').replace(/</g,'&lt;')+'</pre>';return}
  try{t=String(t||'');
    t=_fixLatex(t);
    // Preserve mermaid blocks before marked touches them
    var mermaidBlocks=[];
    t=t.replace(/```mermaid\s*([\s\S]*?)```/g,function(m,c){mermaidBlocks.push(c);return'<!--MERMAID_BLOCK_'+(mermaidBlocks.length-1)+'-->'});
    t=t.replace(/```math\s*([\s\S]*?)```/g,'$$$$$1$$$$').replace(/```latex\s*([\s\S]*?)```/g,'$$$$$1$$$$');
    var parser = (typeof marked.parse === 'function') ? marked.parse : marked;
    var h=parser(t);
    if(!h||h.trim()===''){e.innerHTML='<pre style="white-space:pre-wrap;font-size:13px">'+String(t||'').replace(/</g,'&lt;')+'</pre>';return}
    // Restore mermaid placeholders
    h=h.replace(/<!--MERMAID_BLOCK_(\d+)-->/g,function(m,i){return '<div class="mermaid">'+mermaidBlocks[+i]+'</div>'});
    // 消毒：使用上方全局唯一实现 _sanitizeHtml（questions.js 对比模式同源复用）
    h=_sanitizeHtml(h);
    e.innerHTML=h;
    // Render Mermaid
    if(typeof mermaid!=='undefined'){
      var mn=e.querySelectorAll('.mermaid');
      if(mn.length){mermaid.run({nodes:Array.from(mn)}).catch(function(ex){console.warn('mermaid render fail',ex)})}
    }
    // KaTeX rendering
    var tries=0, maxTries=60, hasRaw=function(d){return /\$(?!\()/.test(d.innerHTML)};
    function _km(){
      tries++;
      var renderer = (typeof renderMathInElement!=='undefined') ? renderMathInElement : window._renderMathInElement;
      if(typeof katex!=='undefined' && renderer){
        try{renderer(e,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false},{left:'\\(',right:'\\)',display:false},{left:'\\[',right:'\\]',display:true}],throwOnError:false})}catch(ex){}
        if(tries<maxTries&&hasRaw(e)){requestAnimationFrame(function(){setTimeout(_km,80)})}
      }else if(tries<maxTries){setTimeout(_km,100)}
    }
    requestAnimationFrame(function(){_km()})
  }catch(ex){console.warn('$md: marked parse failed',ex);e.innerHTML='<pre style="white-space:pre-wrap">'+String(t||'').replace(/</g,'&lt;')+'</pre>'}}

// ===== 兼容别名（供未重写的页面使用） =====
var API = window.$API;
var toast = $toast;
var mdRender = $md;
var fmtTime = $fmt;
var statusBadge = $status;
var typeTag = $type;

// ===== 全局交互协调器 =====
(function(){
  var pending=0,progress=null,lastFocus=null;
  function ensureProgress(){
    if(progress)return progress;
    progress=document.createElement('div');progress.className='ux-progress';progress.setAttribute('aria-hidden','true');
    document.body.appendChild(progress);return progress;
  }
  window.addEventListener('ux:request',function(e){
    pending=Math.max(0,pending+(e.detail&&e.detail.active?1:-1));
    ensureProgress().classList.toggle('active',pending>0);
    document.body.classList.toggle('has-pending-request',pending>0);
  });
  window.$busy=function(button,on,label){
    if(!button)return;
    if(on){button.dataset.uxLabel=button.innerHTML;button.setAttribute('aria-busy','true');button.disabled=true;if(label)button.textContent=label}
    else{button.removeAttribute('aria-busy');button.disabled=false;if(button.dataset.uxLabel){button.innerHTML=button.dataset.uxLabel;delete button.dataset.uxLabel}}
  };
  function enhanceModal(modal){
    if(!modal||modal.dataset.uxReady)return;
    modal.dataset.uxReady='1';
    // 始终为弹窗设置 ARIA 属性，不管当前是否显示；初始隐藏的弹窗也要可被读屏识别。
    modal.setAttribute('role','dialog');modal.setAttribute('aria-modal','true');
    // 仅在弹窗可见时记录焦点并迁移；跳过 $confirm/$prompt 弹层（它们已显式 ok.focus()）
    if(modal.classList.contains('show')||/flex/.test(modal.getAttribute('style')||'')){
      lastFocus=document.activeElement;
      if(modal.classList.contains('confirm-overlay'))return;
      requestAnimationFrame(function(){var f=modal.querySelector('[autofocus],input:not([disabled]),textarea:not([disabled]),select:not([disabled]),button:not([disabled]),[tabindex="0"]');if(f)f.focus({preventScroll:true})});
    }
  }
  function scan(root){
    (root||document).querySelectorAll('.modal-overlay').forEach(enhanceModal);
  }
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'){
      var open=Array.from(document.querySelectorAll('.modal-overlay.show,.modal-overlay[style*="flex"]')).pop();
      if(open&&!open.classList.contains('confirm-overlay')){
        var close=open.querySelector('.modal-close');if(close)close.click();else{open.style.display='none';open.classList.remove('show')}
        if(lastFocus&&lastFocus.focus)lastFocus.focus({preventScroll:true});
      }else if(window.innerWidth<=768&&document.getElementById('sideBar')&&document.getElementById('sideBar').classList.contains('open'))toggleSidebar();
    }
    if(e.key==='/'&&!e.ctrlKey&&!e.metaKey&&!/INPUT|TEXTAREA|SELECT/.test((e.target.tagName||''))){
      var search=document.querySelector('input[type="search"],input[id*="Search"],input[placeholder*="搜索"]');
      if(search){e.preventDefault();search.focus();search.select()}
    }
  });
  document.addEventListener('click',function(e){
    var a=e.target.closest&&e.target.closest('a[href]');
    if(!a||e.defaultPrevented||e.button!==0||e.ctrlKey||e.metaKey||e.shiftKey||a.target==='_blank'||a.hasAttribute('download'))return;
    var href=a.getAttribute('href');if(!href||href.charAt(0)==='#'||href.indexOf('javascript:')===0)return;
    try{var u=new URL(a.href,location.href);if(u.origin===location.origin&&u.pathname!==location.pathname){document.body.classList.add('is-leaving');ensureProgress().classList.add('active')}}catch(_e){}
  },true);
  document.addEventListener('invalid',function(e){
    var el=e.target;el.classList.remove('shake');void el.offsetWidth;el.classList.add('shake');
    el.scrollIntoView({behavior:'smooth',block:'center'});
  },true);
  document.addEventListener('DOMContentLoaded',function(){
    scan(document);
    var sidebar=document.getElementById('sideBar'),toggle=document.getElementById('sidebarToggle');
    if(sidebar)sidebar.setAttribute('aria-label','主导航');if(toggle)toggle.setAttribute('aria-controls','sideBar');
    // 同时监听 DOM 增删与 class 属性变化：静态模板里 `.modal-overlay` 通过
    // classList.add('show') 显示，MutationObserver 必须覆盖属性变更才能补 role/aria-modal/焦点。
    new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(function(n){if(n.nodeType===1){if(n.matches&&n.matches('.modal-overlay'))enhanceModal(n);scan(n)}});if(m.type==='attributes'&&m.target&&m.target.classList&&m.target.classList.contains('modal-overlay')){enhanceModal(m.target);scan(m.target)}})}).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['class']});
  });
})();

// ===== 文本与 JSON 工具函数 =====
function _stripHtmlAndMd(t){return t.replace(/<[^>]*>/g,'').replace(/[#*_~`>\[\]()!-]/g,'').replace(/\n{3,}/g,'\n\n').trim()}
function _tryParseJson(t){try{var o=JSON.parse(t);if(o&&typeof o==='object')return o}catch(e){}return null}
window.copyMD=function(t){navigator.clipboard.writeText(t).then(function(){$toast('MD已复制','ok')}).catch(function(){$toast('复制失败','error')})};
window.copyPlain=function(t){navigator.clipboard.writeText(_stripHtmlAndMd(t)).then(function(){$toast('纯文本已复制','ok')}).catch(function(){$toast('复制失败','error')})};

// ===== 撤销/回收站系统 =====
var UNDO_KEY='study_buddy_undo';
function saveUndo(action,questionId,oldData){
  try{
    var items=JSON.parse(localStorage.getItem(UNDO_KEY)||'[]');
    items.unshift({id:Date.now(),action:action,question_id:questionId,old_data:oldData,timestamp:new Date().toISOString()});
    if(items.length>50)items=items.slice(0,50);
    localStorage.setItem(UNDO_KEY,JSON.stringify(items));
    $toast('已保存撤销点','ok');
  }catch(e){}
}
function undoLast(){
  try{
    var items=JSON.parse(localStorage.getItem(UNDO_KEY)||'[]');
    if(!items.length){$toast('没有可撤销的操作','error');return}
    var last=items[0];
    window.$API.put('/api/questions/'+encodeURIComponent(last.question_id),last.old_data).then(function(){
      items.shift();
      localStorage.setItem(UNDO_KEY,JSON.stringify(items));
      $toast('已撤销: '+last.action,'ok');
    }).catch(function(e){$toast('撤销失败: '+e.message,'error')})
  }catch(e){$toast('撤销失败: '+e.message,'error')}
}
function showUndoHistory(){
  try{
    var items=JSON.parse(localStorage.getItem(UNDO_KEY)||'[]');
    if(!items.length){$toast('撤销历史为空','error');return}
    var html='<div style="max-height:400px;overflow:auto">';
    html+='<table style="width:100%;font-size:12px"><thead><tr><th>操作</th><th>题目ID</th><th>时间</th></tr></thead><tbody>';
    items.forEach(function(item){
      var d=new Date(item.timestamp);
      var ts=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
      html+='<tr><td>'+item.action+'</td><td style="font-family:monospace;font-size:11px">'+(item.question_id||'').slice(0,12)+'</td><td>'+ts+'</td></tr>';
    });
    html+='</tbody></table></div>';
    var modal=document.createElement('div');modal.className='modal-overlay show';modal.style.cssText='display:flex';
    modal.innerHTML='<div class="modal" style="max-width:500px"><button class="modal-close" onclick="this.closest(\'.modal-overlay\').remove()" aria-label="关闭">'+window._closeIconSvg()+'</button><h2>撤销历史</h2>'+html+'<div style="margin-top:12px"><button class="btn btn-sm" onclick="undoLast();this.closest(\'.modal-overlay\').remove()">撤销最近一次</button></div></div>';
    document.body.appendChild(modal);
  }catch(e){$toast('显示历史失败: '+e.message,'error')}
}

// ===== 全局聊天面板 =====
var _globalChatOpen=false;
function toggleGlobalChat(){
  _globalChatOpen=!_globalChatOpen;
  document.getElementById('globalChatPanel').classList.toggle('open',_globalChatOpen);
  if(_globalChatOpen){
    var msgs=document.getElementById('globalChatMsgs');
    if(!msgs.children.length||msgs.children.length===0){
      msgs.innerHTML='<div class="empty">输入问题开始对话</div>';
    }
    document.getElementById('globalChatInput').focus();
  }
}
function sendGlobalChat(){
  var i=document.getElementById('globalChatInput'),m=i.value.trim();
  if(!m)return;
  var c=document.getElementById('globalChatMsgs');
  var ud=document.createElement('div');ud.style.cssText='color:var(--accent);margin:4px 0';
  ud.textContent='[User] '+m;
  c.appendChild(ud);
  var ld=document.createElement('div');
  ld.innerHTML='<span class="spin"></span>';
  c.appendChild(ld);
  i.value='';
  window.$API.post('/api/chat',{message:m}).then(function(r){
    ld.remove();
    var ad=document.createElement('div');
    ad.className='agent-msg-ai';
    var label=document.createElement('b');label.textContent='[AI] ';ad.appendChild(label);
    var md=document.createElement('div');md.style.cssText='font-size:13px';
    ad.appendChild(md);c.appendChild(ad);
    $md(r.reply, md);
    var btns=document.createElement('div');btns.style.cssText='display:flex;gap:4px;margin-top:6px;flex-wrap:wrap';
    var mdBtn=document.createElement('button');mdBtn.className='btn btn-sm';mdBtn.textContent='复制MD';
    mdBtn.addEventListener('click',function(){copyMD(r.reply)});
    var plainBtn=document.createElement('button');plainBtn.className='btn btn-sm';plainBtn.textContent='复制纯文本';
    plainBtn.addEventListener('click',function(){copyPlain(r.reply)});
    btns.appendChild(mdBtn);btns.appendChild(plainBtn);
    ad.appendChild(btns);
    if(r.action&&r.action.type==='jump_paper'){
      var d=r.action.data||{};var sc=r.action.saved_config||'';
      var bd=document.createElement('div');bd.style.margin='4px 0';
      var ks=(d.knowledge_tags||[]).map(function(t){return encodeURIComponent(t)}).join(',');
      var url=sc?'/papers/generate?saved_config='+encodeURIComponent(sc):'/papers/generate?subject='+encodeURIComponent(d.subject||'')+'&grade='+encodeURIComponent(d.grade||'')+'&knowledge_tags='+ks;
      var jb=document.createElement('button');jb.className='btn btn-primary btn-sm';jb.textContent='跳转组卷';
      jb.addEventListener('click',function(){window.location.href=url});
      bd.appendChild(jb);c.appendChild(bd);
    }
    if(r.action&&r.action.type==='tool_need'){
      var nc=document.createElement('div');
      nc.style.cssText='display:flex;align-items:center;gap:8px;margin:6px 0;padding:8px;border:1px solid var(--warn);border-radius:6px;background:var(--warn-bg)';
      var iconSvg=document.createElementNS('http://www.w3.org/2000/svg','svg');
      iconSvg.setAttribute('width','20');iconSvg.setAttribute('height','20');iconSvg.setAttribute('viewBox','0 0 24 24');iconSvg.setAttribute('fill','var(--warn)');
      var circle=document.createElementNS('http://www.w3.org/2000/svg','circle');
      circle.setAttribute('cx','12');circle.setAttribute('cy','12');circle.setAttribute('r','10');circle.setAttribute('stroke','var(--warn)');circle.setAttribute('stroke-width','1.5');circle.setAttribute('fill','none');
      var txt=document.createElementNS('http://www.w3.org/2000/svg','text');
      txt.setAttribute('x','12');txt.setAttribute('y','17');txt.setAttribute('text-anchor','middle');txt.setAttribute('font-size','12');txt.setAttribute('fill','var(--warn)');txt.setAttribute('font-weight','bold');txt.textContent='!';
      iconSvg.appendChild(circle);iconSvg.appendChild(txt);
      var msg=document.createElement('span');msg.style.cssText='font-size:13px;color:var(--warn)';
      msg.textContent='已记录需求: '+(r.action.data&&r.action.data.need?r.action.data.need:'');
      nc.appendChild(iconSvg);nc.appendChild(msg);
      c.appendChild(nc);
    }
    c.scrollTop=c.scrollHeight;
  }).catch(function(e){ld.remove();var ed=document.createElement('div');ed.style.cssText='color:var(--err)';ed.textContent=e.message;c.appendChild(ed)});
}

// ===== 暗黑模式切换 =====
if(typeof mermaid!=='undefined'){mermaid.initialize({startOnLoad:false,theme:document.documentElement.getAttribute('data-theme')==='dark'?'dark':'default'})}
function toggleTheme(){
  var html=document.documentElement;
  var isDark=html.getAttribute('data-theme')==='dark';
  var label=document.getElementById('themeLabel');
  if(isDark){
    html.removeAttribute('data-theme');
    localStorage.setItem('theme','light');
    localStorage.setItem('theme_manual','true');
    if(typeof mermaid!=='undefined'){mermaid.initialize({theme:'default'})}
    if(label)label.textContent='暗黑';
  }else{
    html.setAttribute('data-theme','dark');
    localStorage.setItem('theme','dark');
    localStorage.setItem('theme_manual','true');
    if(typeof mermaid!=='undefined'){mermaid.initialize({theme:'dark'})}
    if(label)label.textContent='亮色';
  }
  window.dispatchEvent(new Event('themeChanged'));
}
// 按时间自动切换暗黑模式 (UTC+8)
function checkAutoTheme(){
  // 用户手动切换过则跳过
  if(localStorage.getItem('theme_manual')==='true')return;
  var darkSettings = null;
  try { darkSettings = JSON.parse(localStorage.getItem('theme_auto_settings')||'null'); } catch(e){}
  if(!darkSettings){
    window.$API.get('/api/settings').then(function(s){
      if(!s||!s.dark_mode_auto)return;
      var cfg = {auto:s.dark_mode_auto,start:s.dark_mode_start||'18:00',end:s.dark_mode_end||'06:00'};
      localStorage.setItem('theme_auto_settings',JSON.stringify(cfg));
      _applyAutoTheme(cfg);
    }).catch(function(){});
    return;
  }
  if(!darkSettings.auto)return;
  _applyAutoTheme(darkSettings);
}
function _applyAutoTheme(cfg){
  var now=new Date();
  var utc=new Date(now.getTime()+now.getTimezoneOffset()*60000+8*3600000);
  var hm=String(utc.getHours()).padStart(2,'0')+':'+String(utc.getMinutes()).padStart(2,'0');
  var shouldBeDark=false;
  if(cfg.start<=cfg.end){ shouldBeDark=hm>=cfg.start&&hm<=cfg.end; }
  else{ shouldBeDark=hm>=cfg.start||hm<=cfg.end; }
  var isDark=document.documentElement.getAttribute('data-theme')==='dark';
  var label=document.getElementById('themeLabel');
  if(shouldBeDark&&!isDark){
    document.documentElement.setAttribute('data-theme','dark');
    localStorage.setItem('theme','dark');
    if(label)label.textContent='亮色';
  }else if(!shouldBeDark&&isDark){
    document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('theme','light');
    if(label)label.textContent='暗黑';
  }
  if(shouldBeDark!==isDark){window.dispatchEvent(new Event('themeChanged'))}
}
(function(){
  if(document.documentElement.getAttribute('data-theme')==='dark'){
    var label=document.getElementById('themeLabel');
    if(label)label.textContent='亮色';
  }
  // 加载时检查一次，之后每分钟检查一次
  checkAutoTheme();
  setInterval(checkAutoTheme,60000);
})();

// ===== 全局前端错误自动上报 =====
(function(){
  var _reported=new Set();
  function _log(err,kind){
    var msg=err&&err.message?err.message:String(err||'');
    var key=(msg||'').slice(0,80)||('_'+typeof err);
    if(_reported.has(key))return;  // 去重，同类错误只报一次
    _reported.add(key);
    var detail='';
    try{detail=(err&&err.stack||'').slice(0,500)}catch(e){}
    var payload=JSON.stringify({error:msg,stack:detail,kind:kind||'error',page:location.pathname});
    // 用 fetch + keepalive 替代 sendBeacon（sendBeacon 发 text/plain）
    fetch('/api/log-frontend-error',{method:'POST',headers:{'X-Auth-Token':window.$API._authToken||'Ntmhzsgtc','Content-Type':'application/json'},body:payload,keepalive:true}).catch(function(){})
  }
  // 用 addEventListener 替代直接赋值，避免覆盖其他处理器
  window.addEventListener('error',function(e){
    if(!e||!e.error){return}  // 跳过资源加载错误 (无 e.error)
    _log(e.error,'error');
  });
  window.addEventListener('unhandledrejection',function(e){
    if(e&&e.reason){
      _log(e.reason instanceof Error?e.reason:{message:typeof e.reason==='string'?e.reason:String(e.reason)},'unhandledrejection');
    }
  });
})();

// ===== 快速模式切换 =====
function toggleQuickMode(){
  var on=document.body.classList.toggle('quick-mode');
  localStorage.setItem('quickMode',on?'true':'false');
  $toast(on?'快速模式已开启':'快速模式已关闭');
}

// ===== 全局 HTML 转义函数（替代各页面重复定义） =====
window.$esc = function(s){
  return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
};

