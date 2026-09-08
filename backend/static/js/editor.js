// ===== Editor State =====
var editor = {
  components: [],        // [{type, x, y, w, h, locked, label, annotation, shadow}]
  allComponentDefs: [],  // from gallery API
  componentPreviews: {}, // type -> SVG string for real rendering
  selectedIndices: new Set(), // multi-select with Ctrl+click
  canvasSvg: null,
  dragState: null,
  snapshots: [],
  undoStack: [],
  redoStack: [],
  ratioUF: {parent: {}, ratio: {}},  // Union-Find: parent[id]=rootId, ratio[id]=relative-to-parent
  resizeState: null,
  calibrationInfo: null,
  questionId: '',
  sceneMatches: [],      // matched semantic scenes for current components
  _matchTimer: null,
  _matchSeq: 0,          // guard against out-of-order semantic-match responses
  _SCENE_PARAM_KEYS: ['clamp_y', 'clamp_w', 'has_ring', 'has_clamp', 'liquid', 'filled', 'water_level', 'angle', 'test_tube_count', 'layout', 'a', 'b', 'c', 'k'],  // 摆法管理的渲染键（应用新摆法前先清除）
  _globalMoveHandler: null,
  _globalUpHandler: null,

  // ─── Initialization ───
  init: async function(){
    this.canvasSvg = document.getElementById('editorCanvas');
    try {
      var r = await window.$API.get('/api/gallery/components');
      this.allComponentDefs = r.components || [];
      this.renderPalette();
    } catch(e) { console.warn('Failed to load components:', e); }
    // Load component preview SVGs for real rendering
    try {
      var p = await window.$API.post('/api/gallery/render-previews', {});
      if(p && p.previews) this.componentPreviews = p.previews;
      else console.warn('No component previews returned');
    } catch(e) { console.warn('Failed to load previews:', e); }
    try {
      var t = await window.$API.get('/api/gallery/templates');
      if(t && t.templates) window._templates = t.templates;
    } catch(e) {}
    this.setupCanvasDnD();
    this.setupKeyboardShortcuts();
    this.setupContextMenu();
    this.matchScenes();
  },

  _pushUndo: function(){
    // 剥离临时预览字段（省内存），恢复时由 _refreshParamPreviews 惰性重建；
    // ratioUF 一并快照，保证撤销后比例锁定关系不错位
    this.redoStack = [];  // 新操作清空重做栈
    this.undoStack.push({
      comps: JSON.parse(JSON.stringify(this._stripOverrides(this.components))),
      uf: JSON.parse(JSON.stringify(this.ratioUF)),
    });
    if(this.undoStack.length > 30) this.undoStack.shift();
    this._lastUndoGroup = '';
  },
  _captureUndoState: function(){
    return {
      comps: JSON.parse(JSON.stringify(this._stripOverrides(this.components))),
      uf: JSON.parse(JSON.stringify(this.ratioUF)),
    };
  },
  _commitUndoState: function(state){
    if(!state) return;
    this.redoStack = [];
    this.undoStack.push(state);
    if(this.undoStack.length > 30) this.undoStack.shift();
  },
  _pushUndoGrouped: function(group){
    var now = Date.now();
    if(this._lastUndoGroup === group && now - (this._lastUndoGroupAt || 0) < 500){
      this._lastUndoGroupAt = now;
      return;
    }
    this._pushUndo();
    this._lastUndoGroup = group;
    this._lastUndoGroupAt = now;
  },
  _scheduleCanvasRender: function(){
    if(this._renderFrame) return;
    this._renderFrame = requestAnimationFrame(function(){
      editor._renderFrame = null;
      editor.renderCanvas();
    });
  },

  // ─── Palette ───
  renderPalette: function(cat, search){
    cat = cat || 'all';
    search = (search || '').toLowerCase();
    var list = document.getElementById('paletteList');
    list.innerHTML = '';
    var items = this.allComponentDefs.filter(function(c){
      if(cat !== 'all' && c.category !== cat) return false;
      if(search && c.name.indexOf(search) < 0 && c.type.indexOf(search) < 0) return false;
      return true;
    });
    items.forEach(function(c){
      var dotClass = c.category === 'chem' ? 'chem' : (c.category === 'phys' ? 'phys' : 'math');
      var preview = editor.componentPreviews[c.type] || '';
      var el = document.createElement('div');
      el.className = 'palette-item';
      el.draggable = true;
      el.dataset.type = c.type || '';
      el.dataset.category = c.category || '';
      if(preview){
        var span = document.createElement('span');
        span.className = 'palette-thumb';
        span.style.cssText = 'display:inline-block;vertical-align:middle;margin-right:4px;width:24px;height:20px;overflow:hidden';
        try {
          var parsed = new DOMParser().parseFromString(preview, 'image/svg+xml');
          if(!parsed.querySelector('parsererror')){
            // 必须整根 <svg> 注入：只搬子元素会丢 viewBox/坐标系，缩略图恒不渲染
            var svgEl = parsed.documentElement;
            if(svgEl.nodeName.toLowerCase() !== 'svg') throw new Error('not svg');
            svgEl.removeAttribute('width'); svgEl.removeAttribute('height');
            svgEl.style.cssText = 'width:100%;height:100%;display:block';
            span.appendChild(svgEl);
          } else {
            throw new Error('parse error');
          }
        } catch(pe) {
          var dot = document.createElement('span');
          dot.className = 'color-dot ' + dotClass;
          span.appendChild(dot);
        }
        el.appendChild(span);
      } else {
        var dot = document.createElement('span');
        dot.className = 'color-dot ' + dotClass;
        el.appendChild(dot);
      }
      var nameNode = document.createElement('span');
      nameNode.className = 'component-name';
      nameNode.textContent = c.name || '';
      nameNode.title = c.name || '';
      el.appendChild(nameNode);
      var tag = document.createElement('span');
      tag.className = 'type-tag'; tag.textContent = c.type || '';
      tag.title = c.type || '';
      el.appendChild(tag);
      el.addEventListener('dragstart', function(e){
        e.dataTransfer.setData('text/plain', el.dataset.type);
        el.classList.add('dragging');
      });
      el.addEventListener('dragend', function(e){
        el.classList.remove('dragging');
      });
      list.appendChild(el);
    });
  },
  filterPalette: function(){
    var search = document.getElementById('paletteSearch').value;
    var activeCat = document.querySelector('.palette-cats .active');
    this.renderPalette(activeCat ? activeCat.dataset.cat : 'all', search);
  },
  filterCat: function(cat, btn){
    document.querySelectorAll('.palette-cats button').forEach(function(b){b.classList.remove('active')});
    btn.classList.add('active');
    var search = document.getElementById('paletteSearch').value;
    this.renderPalette(cat, search);
  },

  // ─── Canvas Events ───
  setupCanvasDnD: function(){
    var svg = this.canvasSvg;
    svg.addEventListener('dragover', function(e){ e.preventDefault(); });
    svg.addEventListener('drop', function(e){
      e.preventDefault();
      try {
        var type = e.dataTransfer.getData('text/plain');
        if(!type) return;
        var rect = svg.getBoundingClientRect();
        if(rect.width === 0 || rect.height === 0){ editor.toast('画布尺寸异常，请刷新页面', 'error'); return; }
        var viewBox = editor._parseViewBox(svg);
        var scaleX = viewBox[2] / rect.width;
        var scaleY = viewBox[3] / rect.height;
        var svgX = (e.clientX - rect.left) * scaleX + viewBox[0];
        var svgY = (e.clientY - rect.top) * scaleY + viewBox[1];
        // Clamp to viewBox bounds
        svgX = Math.max(viewBox[0], Math.min(svgX, viewBox[0] + viewBox[2]));
        svgY = Math.max(viewBox[1], Math.min(svgY, viewBox[1] + viewBox[3]));
        editor.addComponent(type, svgX - 25, svgY - 20);
      } catch(err) {
        console.error('Drop error:', err);
        editor.toast('拖放失败: ' + err.message, 'error');
      }
    }.bind(this));
    // Click: Ctrl+click toggle, normal click single-select, click empty deselect
    svg.addEventListener('click', function(e){
      if(editor._suppressClick){editor._suppressClick=false;return;}
      var target = e.target;
      while(target && target !== svg){
        if(target.classList && target.classList.contains('component-in-canvas')){
          var idx = parseInt(target.dataset.index);
          if(!isNaN(idx)){
            if(e.ctrlKey || e.metaKey){
              // Ctrl+click toggle
              if(editor.selectedIndices.has(idx)) editor.selectedIndices.delete(idx);
              else editor.selectedIndices.add(idx);
            } else {
              // Single select
              editor.selectedIndices.clear();
              editor.selectedIndices.add(idx);
            }
            editor.renderCanvas();
            editor.renderProps();
            return;
          }
        }
        target = target.parentElement;
      }
      // Clicked empty area
      editor.selectedIndices.clear();
      editor.renderCanvas();
      editor.renderProps();
    }.bind(this));
    // Mouse drag for moving selected or resizing
    svg.addEventListener('mousedown', function(e){
      if(e.ctrlKey || e.metaKey) return; // let Ctrl+click work
      var target = e.target;
      // Check if target is a resize handle
      if(target.hasAttribute && target.hasAttribute('data-handle')){
        var handleType = target.getAttribute('data-handle');
        var g = target.closest('g.component-in-canvas');
        if(!g) return;
        var idx = parseInt(g.dataset.index);
        if(isNaN(idx)) return;
        var comp = editor.components[idx];
        if(!comp || comp.locked || editor._isRatioLocked(idx)) return;
        var rect = svg.getBoundingClientRect();
        var viewBox = editor._parseViewBox(svg);
        editor.resizeState = {
          componentIndex: idx,
          handle: handleType,
          startX: e.clientX, startY: e.clientY,
          origX: comp.x, origY: comp.y,
          origW: comp.w, origH: comp.h,
          scaleX: viewBox[2] / rect.width,
          scaleY: viewBox[3] / rect.height,
          keepRatio: e.shiftKey,
          fromCenter: e.altKey,
          linkedComponents: editor._getLinkedComponents(idx),
          beforeState: editor._captureUndoState(),
        };
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      while(target && target !== svg){
        if(target.classList && target.classList.contains('component-in-canvas')){
          var idx = parseInt(target.dataset.index);
          if(!isNaN(idx)){
            var comp = editor.components[idx];
            if(comp && comp.locked){
              // Locked components: also try to find parent group element (handle hit)
              var cx = target.getAttribute('data-index');
              if(!cx){
                // the component-in-canvas might be the hit rect's parent
              }
              return;
            }
            // Single-click select on mousedown (for drag)
            if(!editor.selectedIndices.has(idx)){
              editor.selectedIndices.clear();
              editor.selectedIndices.add(idx);
              editor.renderCanvas();
              editor.renderProps();
            }
            var rect = svg.getBoundingClientRect();
            var viewBox = editor._parseViewBox(svg);
            var scaleX = viewBox[2] / rect.width;
            var scaleY = viewBox[3] / rect.height;
            editor.dragState = {
              componentIndex: idx,
              startX: e.clientX, startY: e.clientY,
              origX: comp.x, origY: comp.y,
              scaleX: scaleX, scaleY: scaleY,
              viewBox: viewBox,
              beforeState: editor._captureUndoState(),
              items: Array.from(editor.selectedIndices).map(function(si){
                var sc = editor.components[si];
                return {idx:si,origX:sc.x,origY:sc.y,w:sc.w,h:sc.h,locked:!!sc.locked};
              }),
            };
            e.preventDefault();
            return;
          }
        }
        target = target.parentElement;
      }
    }.bind(this));
    this._globalMoveHandler = function(e){
      if(editor.resizeState){
        var rs = editor.resizeState;
        var dx = (e.clientX - rs.startX) * rs.scaleX;
        var dy = (e.clientY - rs.startY) * rs.scaleY;
        var comp = editor.components[rs.componentIndex];
        if(!comp) return;
        var newX = rs.origX, newY = rs.origY, newW = rs.origW, newH = rs.origH;
        // Apply resize based on handle
        switch(rs.handle){
          case 'resize-nw': newX = rs.origX + dx; newY = rs.origY + dy; newW = rs.origW - dx; newH = rs.origH - dy; break;
          case 'resize-ne': newY = rs.origY + dy; newW = rs.origW + dx; newH = rs.origH - dy; break;
          case 'resize-se': newW = rs.origW + dx; newH = rs.origH + dy; break;
          case 'resize-sw': newX = rs.origX + dx; newW = rs.origW - dx; newH = rs.origH + dy; break;
        }
        // Min size
        if(newW < 10) newW = 10;
        if(newH < 10) newH = 10;
        // Corner resizing is always proportional. Shift remains accepted for
        // compatibility with older event state, but is no longer required.
        if(rs.origW > 0 && rs.origH > 0){
          var ratio = rs.origW / rs.origH;
          if(Math.abs(newW - rs.origW) > Math.abs(newH - rs.origH)){
            newH = newW / ratio;
          } else {
            newW = newH * ratio;
          }
        }
        // From center (Alt held) - adjust origin
        if(rs.fromCenter){
          var dw = newW - rs.origW, dh = newH - rs.origH;
          newX = rs.origX - dw / 2;
          newY = rs.origY - dh / 2;
        }
        // Clamp position to viewBox bounds
        newX = Math.max(-newW + 10, newX);
        newY = Math.max(-newH + 10, newY);
        comp.x = Math.round(newX);
        comp.y = Math.round(newY);
        comp.w = Math.round(newW);
        comp.h = Math.round(newH);
        // Scale linked components proportionally
        if(rs.linkedComponents.length > 1){
          var scaleW = rs.origW > 0 ? newW / rs.origW : 1;
          var scaleH = rs.origH > 0 ? newH / rs.origH : 1;
          for(var li = 0; li < rs.linkedComponents.length; li++){
            var lc = rs.linkedComponents[li];
            if(lc.idx === rs.componentIndex) continue;
            // Scale proportionally relative to root
            lc.comp.w = Math.round(lc.origW * scaleW);
            lc.comp.h = Math.round(lc.origH * scaleH);
          }
        }
        editor._scheduleCanvasRender();
        return;
      }
      if(!editor.dragState) return;
      var ds = editor.dragState;
      var dx = (e.clientX - ds.startX) * ds.scaleX;
      var dy = (e.clientY - ds.startY) * ds.scaleY;
      var vb = ds.viewBox || [0,0,400,300];
      var items = ds.items || [{idx:ds.componentIndex,origX:ds.origX,origY:ds.origY,w:40,h:40,locked:false}];
      // Clamp the whole selection as one group so relative positions remain stable.
      var minDx = -Infinity, maxDx = Infinity, minDy = -Infinity, maxDy = Infinity;
      items.forEach(function(it){
        if(it.locked) return;
        minDx = Math.max(minDx, vb[0] - it.origX - it.w + 10);
        maxDx = Math.min(maxDx, vb[0] + vb[2] - it.origX - 10);
        minDy = Math.max(minDy, vb[1] - it.origY - it.h + 10);
        maxDy = Math.min(maxDy, vb[1] + vb[3] - it.origY - 10);
      });
      dx = Math.max(minDx, Math.min(maxDx, dx));
      dy = Math.max(minDy, Math.min(maxDy, dy));
      items.forEach(function(it){
        var mc = editor.components[it.idx];
        if(!mc || it.locked) return;
        mc.x = Math.round(it.origX + dx);
        mc.y = Math.round(it.origY + dy);
      });
      editor._scheduleCanvasRender();
    };
    this._globalUpHandler = function(e){
      if(editor.resizeState){
        var rs = editor.resizeState;
        var comp = editor.components[rs.componentIndex];
        if(comp){
          var changed = (comp.w !== rs.origW) || (comp.h !== rs.origH) || (comp.x !== rs.origX) || (comp.y !== rs.origY);
          if(changed){
            editor._commitUndoState(rs.beforeState);
            if(editor.components.length >= 2) editor.recordCalibration();
          }
          // 尺寸变化后实例级预览按新 w/h 重建，避免旧 override 被拉伸形变
          if(changed && comp.previewOverride) editor._refreshCompPreview(comp);
        }
        editor.resizeState = null;
        editor.renderCanvas();
        return;
      }
      if(editor.dragState){
        var ds = editor.dragState;
        var moved = (ds.items || []).some(function(it){
          var mc = editor.components[it.idx];
          return mc && (mc.x !== it.origX || mc.y !== it.origY);
        });
        if(moved){
          editor._commitUndoState(ds.beforeState);
          if(editor.components.length >= 2) editor.recordCalibration();
          editor._suppressClick = true;
        }
        editor.dragState = null;
        editor.renderCanvas();
      }
    };
    document.addEventListener('mousemove', this._globalMoveHandler);
    document.addEventListener('mouseup', this._globalUpHandler);
    this._touchStartHandler=function(e){
      if(!e.touches.length)return;var t=e.touches[0];
      e.target.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,clientX:t.clientX,clientY:t.clientY}));
      if(editor.dragState||editor.resizeState)e.preventDefault();
    };
    this._touchMoveHandler=function(e){
      if(!(editor.dragState||editor.resizeState)||!e.touches.length)return;var t=e.touches[0];
      document.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,cancelable:true,clientX:t.clientX,clientY:t.clientY}));e.preventDefault();
    };
    this._touchEndHandler=function(e){
      if(!(editor.dragState||editor.resizeState))return;
      document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true}));e.preventDefault();
    };
    svg.addEventListener('touchstart',this._touchStartHandler,{passive:false});
    document.addEventListener('touchmove',this._touchMoveHandler,{passive:false});
    document.addEventListener('touchend',this._touchEndHandler,{passive:false});
    window.addEventListener('beforeunload', function(){ editor.cleanup(); });
  },

  cleanup: function(){
    if(this._globalMoveHandler){
      document.removeEventListener('mousemove', this._globalMoveHandler);
      this._globalMoveHandler = null;
    }
    if(this._globalUpHandler){
      document.removeEventListener('mouseup', this._globalUpHandler);
      this._globalUpHandler = null;
    }
    if(this._touchStartHandler)this.canvasSvg.removeEventListener('touchstart',this._touchStartHandler);
    if(this._touchMoveHandler)document.removeEventListener('touchmove',this._touchMoveHandler);
    if(this._touchEndHandler)document.removeEventListener('touchend',this._touchEndHandler);
    clearTimeout(this._matchTimer);
    if(this._renderFrame)cancelAnimationFrame(this._renderFrame);
    this.components.forEach(function(c){if(c._previewTimer)clearTimeout(c._previewTimer)});
  },

  nudgeSelected: function(dx,dy){
    if(!this.selectedIndices.size) return;
    this._pushUndoGrouped('nudge');
    var vb=this._parseViewBox(this.canvasSvg), moved=false;
    this.selectedIndices.forEach(function(idx){
      var c=editor.components[idx]; if(!c||c.locked)return;
      c.x=Math.max(vb[0]-c.w+10,Math.min(vb[0]+vb[2]-10,c.x+dx));
      c.y=Math.max(vb[1]-c.h+10,Math.min(vb[1]+vb[3]-10,c.y+dy));
      moved=true;
    });
    if(moved){this.renderCanvas();this.renderProps();}
  },

  setupKeyboardShortcuts: function(){
    document.addEventListener('keydown', function(e){
      // 输入框/文本域聚焦时不拦截快捷键
      var tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : '';
      if(tag === 'input' || tag === 'textarea' || tag === 'select') return;
      var ctrl = e.ctrlKey || e.metaKey;
      if(ctrl && !e.shiftKey && (e.key === 'z' || e.key === 'Z')){
        e.preventDefault(); editor.undo();
      } else if(ctrl && ((e.shiftKey && (e.key === 'z' || e.key === 'Z')) || e.key === 'y' || e.key === 'Y')){
        e.preventDefault(); editor.redo();
      } else if(ctrl && (e.key === 'l' || e.key === 'L')){
        e.preventDefault(); editor.lockSelected();
      } else if((e.key === 'Delete' || e.key === 'Backspace') && !ctrl){
        if(editor.selectedIndices.size > 0){ e.preventDefault(); editor.deleteSelected(); }
      } else if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].indexOf(e.key)>=0){
        e.preventDefault();
        var step=e.shiftKey?10:1;
        editor.nudgeSelected(e.key==='ArrowLeft'?-step:e.key==='ArrowRight'?step:0,e.key==='ArrowUp'?-step:e.key==='ArrowDown'?step:0);
      }
    });
  },

  setupContextMenu: function(){
    var svg = this.canvasSvg;
    svg.addEventListener('contextmenu', function(e){
      e.preventDefault();
      var target = e.target;
      var idx = -1;
      while(target && target !== svg){
        if(target.classList && target.classList.contains('component-in-canvas')){
          idx = parseInt(target.dataset.index);
          if(!isNaN(idx)) break;
        }
        target = target.parentElement;
      }
      if(idx < 0) return;  // 空白处不弹菜单
      // 选中该元件（若未在多选集中则单选）
      if(!editor.selectedIndices.has(idx)){
        editor.selectedIndices.clear();
        editor.selectedIndices.add(idx);
        editor.renderCanvas();
        editor.renderProps();
      }
      editor._showContextMenu(e.clientX, e.clientY, idx);
    });
  },

  _showContextMenu: function(cx, cy, idx){
    this._hideContextMenu();
    var menu = document.createElement('div');
    menu.className = 'editor-ctx-menu';
    menu.style.cssText = 'position:fixed;z-index:9999;background:var(--card-bg);border:1px solid var(--border);'
      + 'border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,0.15);padding:4px 0;min-width:120px;font-size:12px';
    var items = [
      {text: '置顶', act: function(){ editor._setZ(idx, 'top'); }},
      {text: '置底', act: function(){ editor._setZ(idx, 'bottom'); }},
      {text: '删除', act: function(){ editor.deleteSelected(); }},
      {text: this._isRatioLocked(idx) ? '解除比例锁' : '比例锁', act: function(){
        if(editor._isRatioLocked(idx)) editor.unlinkSelectedRatios();
        else editor.lockSelectedRatios();
      }},
    ];
    for(var i=0;i<items.length;i++){
      var it = document.createElement('div');
      it.textContent = items[i].text;
      it.style.cssText = 'padding:6px 16px;cursor:pointer;white-space:nowrap';
      it.addEventListener('mouseenter', function(){ this.style.background = 'var(--hover)'; });
      it.addEventListener('mouseleave', function(){ this.style.background = 'transparent'; });
      (function(action){ it.addEventListener('click', function(){ editor._hideContextMenu(); action(); }); })(items[i].act);
      menu.appendChild(it);
    }
    menu.style.left = cx + 'px';
    menu.style.top = cy + 'px';
    document.body.appendChild(menu);
    // 点击他处或按 Esc 关闭
    var close = function(){ editor._hideContextMenu(); document.removeEventListener('mousedown', close); };
    setTimeout(function(){ document.addEventListener('mousedown', close); }, 0);
  },
  _hideContextMenu: function(){
    var old = document.querySelector('.editor-ctx-menu');
    if(old) old.remove();
  },
  _setZ: function(idx, mode){
    if(!this.components[idx]) return;
    this._pushUndo();
    var zs = [];
    for(var i=0;i<this.components.length;i++){
      if(this.components[i].z !== undefined) zs.push(this.components[i].z);
    }
    var maxZ = zs.length ? Math.max.apply(null, zs) : 0;
    var minZ = zs.length ? Math.min.apply(null, zs) : 0;
    if(mode === 'top') this.components[idx].z = maxZ + 1;
    else this.components[idx].z = minZ - 1;
    this.renderCanvas();
    this.renderProps();
    this.toast('已' + (mode === 'top' ? '置顶' : '置底'), 'ok');
  },

  // ─── Add Component ───
  addComponent: function(type, x, y){
    var def = null;
    for(var i=0;i<this.allComponentDefs.length;i++){
      if(this.allComponentDefs[i].type === type){ def = this.allComponentDefs[i]; break; }
    }
    if(!def){
      this.toast('未知元件: '+type, 'error');
      return;
    }
    this._pushUndo();
    var comp = {
      type: type, name: def.name, category: def.category,
      x: Math.round(x || 50), y: Math.round(y || 50),
      w: def.default_w || 40, h: def.default_h || 40,
      locked: false, label: def.name, annotation: '', shadow: false,
      link_group: null, link_ratio: 1,
      rotation: 0,  // 旋转角度（0~360）
    };
    if(type === 'function_curve'){
      comp.a = 1; comp.b = 0; comp.c = 0; comp.k = 2;
    }
    // 容器类：注入液面与液体颜色；温度计：注入温度
    var _LIQUID_TYPES = ['beaker','test_tube','conical_flask','round_bottom_flask','flat_bottom_flask',
      'separatory_funnel','graduated_cylinder','gas_bottle','water_bath','water_tank','evaporating_dish'];
    if(_LIQUID_TYPES.indexOf(type) >= 0){
      comp.liquid = 0; comp.liquid_color = 'water';
    }
    if(type === 'thermometer'){ comp.temperature = 25; }
    if(type === 'measure_display'){
      comp.measure_type = 'graduated_cylinder'; comp.min = 0; comp.max = 100;
      comp.value = 50; comp.position = 'right'; comp.direction = 'vertical';
    }
    this.components.push(comp);
    this.selectedIndices.clear();
    this.selectedIndices.add(this.components.length - 1);
    this.renderCanvas();
    this.renderProps();
    this.toast('已添加: '+def.name, 'ok');
    this.matchScenes();
  },

  // ─── Render Canvas with Real SVGs ───
  renderCanvas: function(){
    var svg = this.canvasSvg;
    svg.innerHTML = '<rect width="400" height="300" fill="#fff"/>';
    // z 序渲染：z 小的先画（底层）。数组索引不变，ratioUF/link_group 不受影响
    var zMap = {};
    for(var zi=0; zi<this.allComponentDefs.length; zi++){
      zMap[this.allComponentDefs[zi].type] = this.allComponentDefs[zi].z || 0;
    }
    var order = [];
    for(var oi=0; oi<this.components.length; oi++) order.push(oi);
    order.sort(function(a,b){
      var za = editor.components[a].z !== undefined ? editor.components[a].z : (zMap[editor.components[a].type]||0);
      var zb = editor.components[b].z !== undefined ? editor.components[b].z : (zMap[editor.components[b].type]||0);
      return za - zb;
    });
    for(var oi2=0; oi2<order.length; oi2++){
      var i = order[oi2];
      var comp = this.components[i];
      var isSelected = this.selectedIndices.has(i);
      var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.setAttribute('class', 'component-in-canvas' + (comp.locked ? ' group-locked' : ''));
      g.setAttribute('data-index', i);
      var rot = comp.rotation || 0;
      g.setAttribute('transform', 'translate('+comp.x+','+comp.y+') rotate('+rot+','+(comp.w/2)+','+(comp.h/2)+')');

      // Shadow (edge-only, replacing old filled shadow)
      if(comp.shadow){
        var sh = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        sh.setAttribute('x', 1.5); sh.setAttribute('y', 1.5);
        sh.setAttribute('width', comp.w - 3); sh.setAttribute('height', comp.h - 3);
        sh.setAttribute('rx', '3'); sh.setAttribute('fill', 'none');
        sh.setAttribute('stroke', 'rgba(0,0,0,0.15)'); sh.setAttribute('stroke-width', '1.5');
        sh.setAttribute('pointer-events', 'none');
        g.appendChild(sh);
      }

      // ── Transparent hit-area rect (ensures clicks always register on the component bounds) ──
      var hitRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      hitRect.setAttribute('x', 0); hitRect.setAttribute('y', 0);
      hitRect.setAttribute('width', comp.w); hitRect.setAttribute('height', comp.h);
      hitRect.setAttribute('fill', 'rgba(0,0,0,0)');
      hitRect.setAttribute('pointer-events', 'all');
      g.appendChild(hitRect);

      // Real component SVG from backend preview (instance-level override wins)
      var previewSvg = comp.previewOverride || this.componentPreviews[comp.type];
      if(previewSvg){
        // Container with pointer-events:none so preview SVG never intercepts clicks
        var previewContainer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        previewContainer.setAttribute('pointer-events', 'none');
        try {
          var parsed = new DOMParser().parseFromString(previewSvg, 'image/svg+xml');
          var parserErr = parsed.querySelector('parsererror');
          if(parserErr) throw new Error('SVG parse error');
          var rootSvg = parsed.documentElement;
          var pvb = rootSvg.getAttribute('viewBox');
          if(pvb){
            var parts = pvb.split(/[\s,]+/).map(Number);
            if(parts[2] > 0 && parts[3] > 0){
              var sx = comp.w / parts[2], sy = comp.h / parts[3];
              var sc = Math.min(sx, sy);
              var wrap = document.createElementNS('http://www.w3.org/2000/svg', 'g');
              var offsetX = (comp.w - parts[2] * sc) / 2 - parts[0] * sc;
              var offsetY = (comp.h - parts[3] * sc) / 2 - parts[1] * sc;
              wrap.setAttribute('transform', 'translate('+offsetX+','+offsetY+') scale('+sc+')');
              while(rootSvg.firstChild) wrap.appendChild(rootSvg.firstChild);
              previewContainer.appendChild(wrap);
            } else {
              while(rootSvg.firstChild) previewContainer.appendChild(rootSvg.firstChild);
            }
          } else {
            while(rootSvg.firstChild) previewContainer.appendChild(rootSvg.firstChild);
          }
        } catch(pe) {
          // 解析失败时不使用 innerHTML 回退，避免不可信 SVG 注入脚本/事件处理器；
          // 交由外层 colored rect + label 兜底显示。
          console.warn('SVG preview parse failed:', pe);
        }
        g.appendChild(previewContainer);
      } else {
        // Fallback: colored rect
        var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('width', comp.w); rect.setAttribute('height', comp.h);
        rect.setAttribute('rx', '3');
        rect.setAttribute('fill', 'var(--accent-soft)');
        rect.setAttribute('stroke', 'var(--accent)');
        rect.setAttribute('stroke-width', '1.5');
        g.appendChild(rect);
        if(comp.label){
          var text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          text.setAttribute('x', comp.w/2); text.setAttribute('y', comp.h/2 + 4);
          text.setAttribute('text-anchor', 'middle'); text.setAttribute('font-size', '10');
          text.textContent = comp.label;
          g.appendChild(text);
        }
      }

      // Selection outline
      if(isSelected){
        var sel = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        sel.setAttribute('x', -3); sel.setAttribute('y', -3);
        sel.setAttribute('width', comp.w+6); sel.setAttribute('height', comp.h+6);
        sel.setAttribute('rx', '4'); sel.setAttribute('fill', 'none');
        sel.setAttribute('stroke', 'var(--accent)'); sel.setAttribute('stroke-width', '2.5');
        sel.setAttribute('stroke-dasharray', '5,3');
        sel.setAttribute('pointer-events', 'none');
        g.appendChild(sel);

        // Resize handles: only four corners; ratio-locked components can only move.
        if(!comp.locked && !this._isRatioLocked(i)){
          var handles = [
            {cls:'resize-nw', cx:0, cy:0},
            {cls:'resize-ne', cx:comp.w, cy:0},
            {cls:'resize-se', cx:comp.w, cy:comp.h},
            {cls:'resize-sw', cx:0, cy:comp.h},
          ];
          var hs = 6; // half handle size
          handles.forEach(function(h){
            var hr = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            hr.setAttribute('x', h.cx - hs); hr.setAttribute('y', h.cy - hs);
            hr.setAttribute('width', hs*2); hr.setAttribute('height', hs*2);
            hr.setAttribute('fill', 'var(--paper-bg)'); hr.setAttribute('stroke', 'var(--accent)');
            hr.setAttribute('stroke-width', '1.5'); hr.setAttribute('rx', '1');
            hr.setAttribute('class', h.cls);
            var cursorMap = {'nw':'nwse-resize','ne':'nesw-resize','sw':'nesw-resize','se':'nwse-resize','n':'n-resize','s':'s-resize','e':'e-resize','w':'w-resize'};
            hr.style.cursor = cursorMap[h.cls.replace('resize-','')] || 'default';
            hr.setAttribute('data-handle', h.cls);
            g.appendChild(hr);
          });
        }
      }

      // Lock indicator
      if(comp.locked){
        var lockRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        lockRect.setAttribute('width', comp.w); lockRect.setAttribute('height', comp.h);
        lockRect.setAttribute('rx', '3'); lockRect.setAttribute('fill', 'var(--ok-bg)');
        lockRect.setAttribute('stroke', 'var(--ok)'); lockRect.setAttribute('stroke-width', '1.5');
        lockRect.setAttribute('stroke-dasharray', '4,3'); lockRect.setAttribute('pointer-events', 'none');
        g.appendChild(lockRect);
      }

      // Link ratio indicator (chain icon in top-right corner) — based on UF ratio locking
      if(this._isRatioLocked(i)){
        var linkIcon = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        linkIcon.setAttribute('transform', 'translate('+(comp.w-10)+', -2)');
        linkIcon.setAttribute('pointer-events', 'none');
        var linkBg = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        linkBg.setAttribute('cx', 5); linkBg.setAttribute('cy', 5); linkBg.setAttribute('r', 7);
        linkBg.setAttribute('fill', 'var(--accent)'); linkBg.setAttribute('opacity', '0.85');
        linkIcon.appendChild(linkBg);
        var linkTxt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        linkTxt.setAttribute('x', 5); linkTxt.setAttribute('y', 8.5);
        linkTxt.setAttribute('text-anchor', 'middle'); linkTxt.setAttribute('font-size', '8');
        linkTxt.setAttribute('fill', 'var(--on-accent)'); linkTxt.setAttribute('font-weight', 'bold');
        linkTxt.textContent = '\u223E';  // ∾ wave symbol
        linkIcon.appendChild(linkTxt);
        g.appendChild(linkIcon);
      }

      // Annotation text below
      if(comp.annotation){
        var an = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        an.setAttribute('x', comp.w/2); an.setAttribute('y', comp.h + 16);
        an.setAttribute('text-anchor', 'middle'); an.setAttribute('font-size', '11');
        an.setAttribute('font-weight', '600'); an.setAttribute('fill', 'var(--warn)');
        an.textContent = comp.annotation;
        g.appendChild(an);
      }

      svg.appendChild(g);
    }
    document.getElementById('canvasInfo').textContent = this.components.length + ' 个元件 (选中 ' + this.selectedIndices.size + ')';
    // Update ratio button states based on UF ratio locking
    var lockBtn = document.getElementById('lockRatioBtn');
    var unifyBtn = document.getElementById('unifyRatioBtn');
    var unlinkBtn = document.getElementById('unlinkRatioBtn');
    if(this.selectedIndices.size >= 2){
      lockBtn.classList.remove('disabled');
    } else {
      lockBtn.classList.add('disabled');
    }
    var hasLinks = false;
    for(var i=0;i<this.components.length;i++){ if(this._isRatioLocked(i)){ hasLinks = true; break; } }
    if(hasLinks){ unifyBtn.classList.remove('disabled'); }
    else { unifyBtn.classList.add('disabled'); }
    if(this.selectedIndices.size > 0){
      var selHasLink = false;
      var _this = this;
      this.selectedIndices.forEach(function(idx){ if(_this._isRatioLocked(idx)) selHasLink = true; });
      if(selHasLink) unlinkBtn.classList.remove('disabled'); else unlinkBtn.classList.add('disabled');
    } else {
      unlinkBtn.classList.add('disabled');
    }
  },

  // ─── 比例并查集 & 关联操作 ───
  // 以组件数组下标为 UF 元素 ID；link_group 仅作为视觉/兼容字段，同步为 root id。
  _findUF: function(id){
    if(this.ratioUF.parent[id] === undefined) return id;
    var root = this._findUF(this.ratioUF.parent[id]);
    if(root !== this.ratioUF.parent[id]){
      this.ratioUF.ratio[id] = (this.ratioUF.ratio[id] || 1) * (this.ratioUF.ratio[this.ratioUF.parent[id]] || 1);
      this.ratioUF.parent[id] = root;
    }
    return root;
  },

  _unionUF: function(a, b){
    var ra = this._findUF(a), rb = this._findUF(b);
    if(ra === rb) return;
    this.ratioUF.parent[rb] = ra;
    this.ratioUF.ratio[rb] = 1;
    this._syncLinkGroups();
  },

  _syncLinkGroups: function(){
    // 将 link_group 同步为当前 UF root，便于属性面板和旧数据兼容
    for(var i = 0; i < this.components.length; i++){
      var root = this._findUF(i);
      if(root !== i || this.ratioUF.parent[i] !== undefined){
        this.components[i].link_group = root;
      } else {
        this.components[i].link_group = null;
      }
      this.components[i].link_ratio = this.ratioUF.ratio[i] || 1;
    }
  },

  _getLinkedComponents: function(idx){
    var root = this._findUF(idx);
    var result = [];
    for(var i = 0; i < this.components.length; i++){
      if(this._findUF(i) === root){
        result.push({idx: i, comp: this.components[i], origW: this.components[i].w, origH: this.components[i].h});
      }
    }
    return result;
  },

  _isRatioLocked: function(idx){
    return this._findUF(idx) !== idx || this.ratioUF.parent[idx] !== undefined;
  },

  // 完全重置并查集（用于模板加载等旧数据重建）。
  // 兼容旧数据：将字符串型 link_group 按组名重新合并到 UF；数值型 link_group 是数组下标，已失效，清空。
  _resetUF: function(){
    this.ratioUF = {parent: {}, ratio: {}};
    var groups = {};
    for(var i = 0; i < this.components.length; i++){
      var c = this.components[i];
      if(typeof c.link_group === 'number'){
        c.link_group = null;
        c.link_ratio = 1;
      }
      if(typeof c.link_group === 'string' && c.link_group){
        if(!groups[c.link_group]) groups[c.link_group] = [];
        groups[c.link_group].push(i);
      }
    }
    for(var gk in groups){
      var members = groups[gk];
      if(members.length >= 2){
        for(var gi = 1; gi < members.length; gi++){
          this._unionUF(members[0], members[gi]);
        }
      }
    }
  },

  // 删除元件后保留其余元件的比例锁定关系：将旧下标映射到新下标后重建 ratioUF。
  _remapUFAfterDelete: function(deletedSet){
    var oldToNew = {};
    var newIdx = 0;
    var totalOld = this.components.length + deletedSet.size;
    for(var i = 0; i < totalOld; i++){
      if(!deletedSet.has(i)) oldToNew[i] = newIdx++;
    }
    var newParent = {}, newRatio = {};
    for(var oldId in this.ratioUF.parent){
      var oldIdNum = parseInt(oldId, 10);
      if(isNaN(oldIdNum) || deletedSet.has(oldIdNum)) continue;
      var root = this._findUF(oldIdNum);
      if(deletedSet.has(root)) continue;
      if(oldToNew[oldIdNum] !== undefined && oldToNew[root] !== undefined){
        newParent[oldToNew[oldIdNum]] = oldToNew[root];
      }
    }
    for(var oldId in this.ratioUF.ratio){
      var oldIdNum = parseInt(oldId, 10);
      if(isNaN(oldIdNum) || deletedSet.has(oldIdNum)) continue;
      var root = this._findUF(oldIdNum);
      if(deletedSet.has(root)) continue;
      if(oldToNew[oldIdNum] !== undefined && oldToNew[root] !== undefined){
        newRatio[oldToNew[oldIdNum]] = this.ratioUF.ratio[oldId];
      }
    }
    this.ratioUF = {parent: newParent, ratio: newRatio};
    this._syncLinkGroups();
  },

  lockSelectedRatios: function(){
    if(this.selectedIndices.size < 2){
      this.toast('至少需要选中 2 个元件才能锁定比例', 'warn');
      return;
    }
    this._pushUndo();
    var ids = Array.from(this.selectedIndices);
    var first = ids[0];
    for(var i = 1; i < ids.length; i++){
      this._unionUF(first, ids[i]);
    }
    this._syncLinkGroups();
    this.renderCanvas();
    this.renderProps();
    this.toast('已锁定 ' + ids.length + ' 个元件的比例关系', 'ok');
  },

  unlinkSelectedRatios: function(){
    if(this.selectedIndices.size === 0){
      this.toast('请先选中要解除比例锁定的元件', 'warn');
      return;
    }
    this._pushUndo();
    var rootsToClear = {};
    var _this = this;
    this.selectedIndices.forEach(function(idx){
      rootsToClear[_this._findUF(idx)] = true;
    });
    var count = 0;
    for(var i = 0; i < this.components.length; i++){
      if(rootsToClear[this._findUF(i)]){
        delete this.ratioUF.parent[i];
        delete this.ratioUF.ratio[i];
        this.components[i].link_group = null;
        this.components[i].link_ratio = 1;
        count++;
      }
    }
    this._syncLinkGroups();
    this.renderCanvas();
    this.renderProps();
    if(count > 0){
      this.toast('已解除 ' + count + ' 个元件的比例锁定', 'ok');
    } else {
      this.toast('选中的元件未锁定比例', 'warn');
    }
  },

  unlockAllRatios: function(){
    var count = 0;
    for(var i = 0; i < this.components.length; i++){
      if(this._isRatioLocked(i)){
        count++;
      }
    }
    if(count > 0) this._pushUndo();
    this.ratioUF = {parent: {}, ratio: {}};
    this._syncLinkGroups();
    if(count > 0){
      this.renderCanvas();
      this.renderProps();
      this.toast('已解除 ' + count + ' 个元件的比例锁定', 'ok');
    } else {
      this.toast('没有锁定的比例关系', 'warn');
    }
  },

  unifyAllRatios: function(){
    var groups = {};
    for(var i = 0; i < this.components.length; i++){
      var root = this._findUF(i);
      if(!groups[root]) groups[root] = [];
      groups[root].push(i);
    }
    var groupKeys = Object.keys(groups).filter(function(k){return groups[k].length > 1});
    if(groupKeys.length === 0){
      this.toast('没有比例锁定组可以统一', 'warn');
      return;
    }
    this._pushUndo();
    var totalAdjusted = 0;
    groupKeys.forEach(function(root){
      var members = groups[root];
      if(members.length < 2) return;
      var baseline = null, maxArea = 0;
      members.forEach(function(idx){
        var c = editor.components[idx];
        if(!c) return;
        var area = c.w * c.h;
        if(area > maxArea){ maxArea = area; baseline = c; }
      });
      if(!baseline) return;
      var baseW = baseline.w, baseH = baseline.h;
      members.forEach(function(idx){
        if(editor.components[idx] !== baseline){
          editor.components[idx].w = baseW;
          editor.components[idx].h = baseH;
          totalAdjusted++;
        }
      });
    });
    this._syncLinkGroups();
    this.renderCanvas();
    this.renderProps();
    this.toast('已统一 ' + groupKeys.length + ' 个比例组（' + totalAdjusted + ' 个元件调整）', 'ok');
  },

  // ─── Properties ───
  renderProps: function(){
    var body = document.getElementById('propsBody');
    body.innerHTML = '';
    function makeBtn(text, onclick, style){
      var b = document.createElement('button');
      b.className = 'btn btn-sm'; b.textContent = text; b.style.cssText = style || '';
      b.addEventListener('click', onclick);
      return b;
    }
    if(this.selectedIndices.size === 0){
      var wrap = document.createElement('div'); wrap.style.marginBottom = '12px';
      var lockSec = document.createElement('div'); lockSec.className = 'props-section';
      var h4a = document.createElement('h4'); h4a.textContent = '位置锁定';
      lockSec.appendChild(h4a);
      lockSec.appendChild(makeBtn('锁定全部', function(){ editor.lockAll(); }, 'width:100%;margin-bottom:4px'));
      lockSec.appendChild(makeBtn('锁定选中 (Ctrl+点击多选)', function(){ editor.lockSelected(); }, 'width:100%;margin-bottom:4px'));
      lockSec.appendChild(makeBtn('解锁全部', function(){ editor.unlockAll(); }, 'width:100%;margin-bottom:4px'));
      wrap.appendChild(lockSec);
      var ratioSec = document.createElement('div'); ratioSec.className = 'props-section';
      var h4b = document.createElement('h4'); h4b.textContent = '比例锁定';
      ratioSec.appendChild(h4b);
      ratioSec.appendChild(makeBtn('锁定比例 (Ctrl+点击多选2+元件)', function(){ editor.lockSelectedRatios(); }, 'width:100%;margin-bottom:4px'));
      ratioSec.appendChild(makeBtn('解除选中比例', function(){ editor.unlinkSelectedRatios(); }, 'width:100%;margin-bottom:4px'));
      ratioSec.appendChild(makeBtn('解除全部比例', function(){ editor.unlockAllRatios(); }, 'width:100%;margin-bottom:4px'));
      ratioSec.appendChild(makeBtn('统一比例组尺寸', function(){ editor.unifyAllRatios(); }, 'width:100%;margin-bottom:4px'));
      wrap.appendChild(ratioSec);
      var otherSec = document.createElement('div'); otherSec.className = 'props-section';
      var h4c = document.createElement('h4'); h4c.textContent = '其他';
      otherSec.appendChild(h4c);
      otherSec.appendChild(makeBtn('加载题目图片', function(){ editor.loadQuestionDiagram(); }, 'width:100%;margin-bottom:4px'));
      otherSec.appendChild(makeBtn('记录校准样本', function(){ editor.recordCalibration(); }, 'width:100%'));
      wrap.appendChild(otherSec);
      var hintSec = document.createElement('div'); hintSec.className = 'props-section';
      var h4d = document.createElement('h4'); h4d.textContent = '提示';
      hintSec.appendChild(h4d);
      var hint = document.createElement('div');
      hint.style.cssText = 'font-size:11px;color:var(--text2)';
      hint.textContent = '按住 Ctrl 点击可多选元件。位置锁定防止移动；比例锁定保持多元件尺寸联动。';
      hintSec.appendChild(hint);
      wrap.appendChild(hintSec);
      body.appendChild(wrap);
      return;
    }
    // Show properties of first selected (or common properties)
    var idx = this.selectedIndices.values().next().value;
    var comp = this.components[idx];
    var selCount = this.selectedIndices.size;
    var ratioLocked = this._isRatioLocked(idx);
    var ratioText = ratioLocked ? ('已锁定 (组 '+this._findUF(idx)+')') : '未锁定';
    function makeRow(label, control){
      var row = document.createElement('div'); row.className = 'prop-row';
      var lbl = document.createElement('label'); lbl.textContent = label;
      row.appendChild(lbl); row.appendChild(control);
      return row;
    }
    var countSpan = document.createElement('span');
    countSpan.style.cssText = 'flex:1;font-weight:600'; countSpan.textContent = selCount + ' 个元件';
    body.appendChild(makeRow('选中', countSpan));
    var typeSpan = document.createElement('span'); typeSpan.style.flex = '1'; typeSpan.textContent = comp.name || comp.type || '';
    body.appendChild(makeRow('类型', typeSpan));
    function makeNumberInput(val, key, disabled){
      var inp = document.createElement('input');
      inp.type = 'number'; inp.value = val; inp.disabled = !!disabled;
      inp.addEventListener('change', function(){ editor.updateProp(key, inp.value); });
      return inp;
    }
    body.appendChild(makeRow('X', makeNumberInput(comp.x, 'x', comp.locked)));
    body.appendChild(makeRow('Y', makeNumberInput(comp.y, 'y', comp.locked)));
    body.appendChild(makeRow('宽', makeNumberInput(comp.w, 'w', comp.locked || ratioLocked)));
    body.appendChild(makeRow('高', makeNumberInput(comp.h, 'h', comp.locked || ratioLocked)));
    if(ratioLocked){
      var ratioHint = document.createElement('div');
      ratioHint.style.cssText = 'font-size:11px;color:var(--accent);margin:-2px 0 6px 59px';
      ratioHint.textContent = '比例锁定：只能移动，不能调整大小';
      body.appendChild(ratioHint);
    }
    var labelInp = document.createElement('input');
    labelInp.type = 'text'; labelInp.value = comp.label || '';
    labelInp.addEventListener('input', function(){ editor.updateProp('label', labelInp.value, true); });
    body.appendChild(makeRow('标签', labelInp));
    if(comp.type === 'function_curve'){
      function makeCurveInput(val, key){
        var inp = document.createElement('input');
        inp.type = 'number'; inp.value = val; inp.step = 'any';
        inp.addEventListener('change', function(){ editor.updateProp(key, parseFloat(inp.value) || 0); });
        return inp;
      }
      body.appendChild(makeRow('a', makeCurveInput(comp.a !== undefined ? comp.a : 1, 'a')));
      body.appendChild(makeRow('b', makeCurveInput(comp.b !== undefined ? comp.b : 0, 'b')));
      body.appendChild(makeRow('c', makeCurveInput(comp.c !== undefined ? comp.c : 0, 'c')));
      body.appendChild(makeRow('k', makeCurveInput(comp.k !== undefined ? comp.k : 2, 'k')));
    }
    // 旋转角度（所有元件，0~360）
    var rotRow = document.createElement('div'); rotRow.className = 'prop-row';
    var rotLbl = document.createElement('label'); rotLbl.textContent = '旋转';
    var rotWrap = document.createElement('div'); rotWrap.style.cssText = 'flex:1;display:flex;align-items:center;gap:6px';
    var rotSlider = document.createElement('input'); rotSlider.type = 'range';
    rotSlider.min = 0; rotSlider.max = 360; rotSlider.step = 1; rotSlider.value = comp.rotation || 0;
    var rotNum = document.createElement('span'); rotNum.style.cssText = 'font-size:11px;min-width:34px;text-align:right';
    rotNum.textContent = (comp.rotation || 0) + '\u00B0';
    rotSlider.addEventListener('input', function(){
      rotNum.textContent = rotSlider.value + '\u00B0';
      editor.updateProp('rotation', parseInt(rotSlider.value, 10), true);
    });
    rotWrap.appendChild(rotSlider); rotWrap.appendChild(rotNum);
    rotRow.appendChild(rotLbl); rotRow.appendChild(rotWrap);
    body.appendChild(rotRow);
    // 温度计：温度滑块（-20~200）
    if(comp.type === 'thermometer'){
      var tRow = document.createElement('div'); tRow.className = 'prop-row';
      var tLbl = document.createElement('label'); tLbl.textContent = '温度';
      var tWrap = document.createElement('div'); tWrap.style.cssText = 'flex:1;display:flex;align-items:center;gap:6px';
      var tSlider = document.createElement('input'); tSlider.type = 'range';
      tSlider.min = -20; tSlider.max = 200; tSlider.step = 1; tSlider.value = comp.temperature !== undefined ? comp.temperature : 25;
      var tNum = document.createElement('span'); tNum.style.cssText = 'font-size:11px;min-width:42px;text-align:right';
      tNum.textContent = (tSlider.value) + '\u00B0C';
      tSlider.addEventListener('input', function(){
        tNum.textContent = tSlider.value + '\u00B0C';
        editor.updateProp('temperature', parseInt(tSlider.value, 10), true);
      });
      tWrap.appendChild(tSlider); tWrap.appendChild(tNum);
      tRow.appendChild(tLbl); tRow.appendChild(tWrap);
      body.appendChild(tRow);
    }
    if(comp.type === 'measure_display'){
      var mdTypes=[['graduated_cylinder','量筒'],['spring_scale','弹簧测力计'],['ruler','刻度尺'],['thermometer','温度计']];
      var mdType=document.createElement('select'); mdType.style.flex='1';
      mdTypes.forEach(function(opt){var o=document.createElement('option');o.value=opt[0];o.textContent=opt[1];o.selected=(comp.measure_type||'graduated_cylinder')===opt[0];mdType.appendChild(o)});
      mdType.addEventListener('change',function(){editor.updateProp('measure_type',mdType.value)});
      body.appendChild(makeRow('仪器',mdType));
      body.appendChild(makeRow('最小值',makeNumberInput(comp.min!==undefined?comp.min:0,'min',false)));
      body.appendChild(makeRow('最大值',makeNumberInput(comp.max!==undefined?comp.max:100,'max',false)));
      body.appendChild(makeRow('读数',makeNumberInput(comp.value!==undefined?comp.value:50,'value',false)));
      var mdDir=document.createElement('select');mdDir.style.flex='1';
      [['vertical','竖向'],['horizontal','横向']].forEach(function(opt){var o=document.createElement('option');o.value=opt[0];o.textContent=opt[1];o.selected=(comp.direction||'vertical')===opt[0];mdDir.appendChild(o)});
      mdDir.addEventListener('change',function(){editor.updateProp('direction',mdDir.value)});
      body.appendChild(makeRow('方向',mdDir));
      var mdPos=document.createElement('select');mdPos.style.flex='1';
      [['right','右侧刻度'],['left','左侧刻度']].forEach(function(opt){var o=document.createElement('option');o.value=opt[0];o.textContent=opt[1];o.selected=(comp.position||'right')===opt[0];mdPos.appendChild(o)});
      mdPos.addEventListener('change',function(){editor.updateProp('position',mdPos.value)});
      body.appendChild(makeRow('刻度位置',mdPos));
    }
    // 容器类：液面滑块 + 液体颜色下拉
    var _LIQUID_PROP_TYPES = ['beaker','test_tube','conical_flask','round_bottom_flask','flat_bottom_flask',
      'separatory_funnel','graduated_cylinder','gas_bottle','water_bath','water_tank','evaporating_dish'];
    if(_LIQUID_PROP_TYPES.indexOf(comp.type) >= 0){
      var lqRow = document.createElement('div'); lqRow.className = 'prop-row';
      var lqLbl = document.createElement('label'); lqLbl.textContent = '液面';
      var lqWrap = document.createElement('div'); lqWrap.style.cssText = 'flex:1;display:flex;align-items:center;gap:6px';
      var lqSlider = document.createElement('input'); lqSlider.type = 'range';
      lqSlider.min = 0; lqSlider.max = 100; lqSlider.step = 1;
      lqSlider.value = Math.round((comp.liquid || 0) * 100);
      var lqNum = document.createElement('span'); lqNum.style.cssText = 'font-size:11px;min-width:34px;text-align:right';
      lqNum.textContent = lqSlider.value + '%';
      lqSlider.addEventListener('input', function(){
        lqNum.textContent = lqSlider.value + '%';
        editor.updateProp('liquid', parseInt(lqSlider.value, 10) / 100, true);
      });
      lqWrap.appendChild(lqSlider); lqWrap.appendChild(lqNum);
      lqRow.appendChild(lqLbl); lqRow.appendChild(lqWrap);
      body.appendChild(lqRow);
      var lcRow = document.createElement('div'); lcRow.className = 'prop-row';
      var lcLbl = document.createElement('label'); lcLbl.textContent = '液体';
      var lcSel = document.createElement('select'); lcSel.style.flex = '1';
      var _LIQUID_OPTS = [['water','水 (蓝)'],['acid','酸 (绿)'],['base','碱 (红)'],['oil','油 (橙)'],['indicator','指示剂 (紫)']];
      for(var oi=0; oi<_LIQUID_OPTS.length; oi++){
        var opt = document.createElement('option'); opt.value = _LIQUID_OPTS[oi][0]; opt.textContent = _LIQUID_OPTS[oi][1];
        if((comp.liquid_color || 'water') === _LIQUID_OPTS[oi][0]) opt.selected = true;
        lcSel.appendChild(opt);
      }
      lcSel.addEventListener('change', function(){ editor.updateProp('liquid_color', lcSel.value); });
      lcRow.appendChild(lcLbl); lcRow.appendChild(lcSel);
      body.appendChild(lcRow);
    }
    var annoInp = document.createElement('input');
    annoInp.type = 'text'; annoInp.value = comp.annotation || ''; annoInp.placeholder = '说明文字';
    annoInp.addEventListener('input', function(){ editor.updateProp('annotation', annoInp.value, true); });
    body.appendChild(makeRow('标注', annoInp));
    var shadowInp = document.createElement('input');
    shadowInp.type = 'checkbox'; shadowInp.checked = !!comp.shadow;
    shadowInp.addEventListener('change', function(){ editor.updateProp('shadow', shadowInp.checked); });
    body.appendChild(makeRow('阴影', shadowInp));
    var ratioSpan = document.createElement('span');
    ratioSpan.style.cssText = 'flex:1;color:'+(ratioLocked?'var(--accent)':'var(--text3)')+';font-size:11px';
    ratioSpan.textContent = ratioText;
    body.appendChild(makeRow('比例', ratioSpan));
    var delRow = document.createElement('div'); delRow.className = 'prop-row'; delRow.style.marginTop = '8px';
    var delBtn = document.createElement('button'); delBtn.className = 'btn btn-sm btn-danger'; delBtn.style.flex = '1';
    delBtn.textContent = '删除选中';
    delBtn.addEventListener('click', function(){ editor.deleteSelected(); });
    delRow.appendChild(delBtn);
    body.appendChild(delRow);
  },
  updateProp: function(key, value, live){
    if(this.selectedIndices.size === 0) return;
    this._pushUndoGrouped('prop:'+key+':'+Array.from(this.selectedIndices).join(','));
    var _this = this;
    var scaledRoots = {};  // track which UF roots already scaled to prevent cascading
    this.selectedIndices.forEach(function(idx){
      var comp = _this.components[idx];
      if(!comp) return;
      if(key === 'shadow'){ comp[key] = !!value; }
      else if(key === 'x' || key === 'y' || key === 'w' || key === 'h'){
        // 空串/NaN 不写入：Number('') 为 0、Number('abc') 为 NaN，都会让元件跳 0 或从画布消失
        var num = Number(value);
        if(value === '' || value === null || !isFinite(num)) return;
        var oldVal = comp[key];
        comp[key] = num;
        // Proportional resize: sync BOTH w and h for all UF-linked elements
        if((key === 'w' || key === 'h') && oldVal > 0){
          var root = _this._findUF(idx);
          if(root !== idx || _this.ratioUF.parent[idx] !== undefined){
            if(!scaledRoots[root]){
              scaledRoots[root] = true;
              var scale = comp[key] / oldVal;
              for(var j=0;j<_this.components.length;j++){
                if(j !== idx && _this._findUF(j) === root){
                  var linked = _this.components[j];
                  linked.w = Math.round(linked.w * scale);
                  linked.h = Math.round(linked.h * scale);
                }
              }
            }
          }
        }
      }
      else { comp[key] = value; }
      if((key === 'a' || key === 'b' || key === 'c' || key === 'k' || key === 'label') && comp.type === 'function_curve'){
        editor._scheduleCompPreview(comp);
      } else if(key === 'temperature' || key === 'liquid' || key === 'liquid_color' || key === 'measure_type' || key === 'min' || key === 'max' || key === 'value' || key === 'position' || key === 'direction'){
        // 温度/液面/液体颜色由后端渲染，需重建实例级预览
        editor._scheduleCompPreview(comp);
      }
    });
    this._syncLinkGroups();
    this.renderCanvas();
    // live=true（滑块/文本连续输入）：跳过属性面板整体重建，否则输入 1 字符即失焦
    if(!live) this.renderProps();
  },
  deleteSelected: function(){
    if(this.selectedIndices.size === 0) return;
    this._pushUndo();
    var indices = Array.from(this.selectedIndices).sort(function(a,b){return b-a});
    var deletedSet = new Set(indices);
    for(var i=0;i<indices.length;i++) this.components.splice(indices[i], 1);
    this.selectedIndices.clear();
    this._remapUFAfterDelete(deletedSet);
    this.renderCanvas();
    this.renderProps();
    this.matchScenes();
  },
  annotationSelected: async function(){
    if(this.selectedIndices.size === 0){ this.toast('请先选中元件', 'error'); return; }
    var idx = this.selectedIndices.values().next().value;
    var anno = await $prompt('输入标注文字:', {default: this.components[idx].annotation || ''}); if(anno===null) return;
    if(anno !== null){
      this._pushUndo();
      this.components[idx].annotation = anno;
      this.renderCanvas();
      this.renderProps();
    }
  },

  // ─── Lock / Unlock ───
  lockAll: function(){
    this._pushUndo();
    for(var i=0;i<this.components.length;i++) this.components[i].locked = true;
    this.selectedIndices.clear();
    this.renderCanvas();
    this.renderProps();
    this.toast('已锁定全部 '+this.components.length+' 个元件', 'ok');
    if(this.components.length >= 3) this.recordCalibration();
  },
  lockSelected: function(){
    if(this.selectedIndices.size === 0){ this.toast('请先选中要锁定的元件 (Ctrl+点击多选)', 'error'); return; }
    this._pushUndo();
    var _this = this;
    this.selectedIndices.forEach(function(idx){
      if(_this.components[idx]) _this.components[idx].locked = true;
    });
    this.renderCanvas();
    this.renderProps();
    this.toast('已锁定 '+this.selectedIndices.size+' 个元件', 'ok');
    if(this.components.length >= 3) this.recordCalibration();
  },
  unlockAll: function(){
    this._pushUndo();
    for(var i=0;i<this.components.length;i++) this.components[i].locked = false;
    this.selectedIndices.clear();
    this.renderCanvas();
    this.renderProps();
    this.toast('已解锁全部', 'ok');
  },

  // ─── Ratio Locking (legacy aliases, now unified to UF-based lockSelectedRatios/unlinkSelectedRatios) ───
  linkRatio: function(){ this.lockSelectedRatios(); },
  unlinkRatio: function(){ this.unlinkSelectedRatios(); },

  // ─── Semantic Scene Combos (multi-config semantic ports) ───
  matchScenes: function(){
    clearTimeout(this._matchTimer);
    this._matchTimer = setTimeout(function(){ editor._doMatchScenes(); }, 500);
  },
  _doMatchScenes: async function(){
    var seq = ++this._matchSeq;
    var types = [];
    for(var i=0;i<this.components.length;i++){
      if(types.indexOf(this.components[i].type) < 0) types.push(this.components[i].type);
    }
    if(!types.length){ this.sceneMatches = []; this._updateSceneBtn(); return; }
    var r;
    try {
      r = await window.$API.post('/api/diagram/semantic-match', {component_types: types});
    } catch(e){ return; }  // 匹配失败保持旧状态，下次操作再试
    if(seq !== this._matchSeq) return;  // 已有更新的请求发出，丢弃陈旧响应
    this.sceneMatches = (r && r.scenes) || [];
    this._updateSceneBtn();
  },
  _updateSceneBtn: function(){
    var btn = document.getElementById('sceneComboBtn');
    if(!btn) return;
    // 清除旧 badge，避免重复累加
    var oldBadge = btn.querySelector('.toolbar-badge');
    if(oldBadge) oldBadge.remove();
    var n = this.sceneMatches.length;
    var multi = 0;
    for(var i=0;i<this.sceneMatches.length;i++){
      var cfgs = this.sceneMatches[i].configs || [];
      if(cfgs.length > 1) multi++;
    }
    btn.textContent = '组合方案';
    if(n){
      btn.classList.remove('disabled');
      var badge = document.createElement('span');
      badge.className = 'toolbar-badge';
      badge.textContent = String(n);
      btn.appendChild(badge);
    } else {
      btn.classList.add('disabled');
    }
    btn.title = n ? ('匹配到 '+n+' 个组合场景'+(multi ? '，其中 '+multi+' 个有多种摆法' : '')+'，点击选择摆法') : '当前元件未匹配到组合场景，可尝试添加铁架台、酒精灯、烧杯等常见装置';
  },
  showScenePicker: function(){
    if(!this.sceneMatches.length){ this.toast('当前元件未匹配到组合场景，请先放入装置元件', 'warn'); return; }
    var old = document.querySelector('.modal-overlay'); if(old) old.remove();
    var modal = document.createElement('div'); modal.className='modal-overlay show'; modal.style.display='flex';
    var inner = document.createElement('div');
    inner.className = 'modal'; inner.style.maxWidth = '560px';
    var closeBtn = document.createElement('button');
    closeBtn.className = 'modal-close'; closeBtn.innerHTML = '&times;';
    closeBtn.addEventListener('click', function(){ modal.remove(); });
    var h2 = document.createElement('h2'); h2.textContent = '选择组合摆法';
    var hint = document.createElement('p');
    hint.style.cssText = 'font-size:12px;color:var(--text2);margin-bottom:8px';
    hint.textContent = '同一组元件可有多种摆法，选择后自动按语义端口对齐落位';
    var list = document.createElement('div');
    list.className = 'scene-list'; list.style.cssText = 'max-height:420px;overflow-y:auto';
    inner.appendChild(closeBtn); inner.appendChild(h2); inner.appendChild(hint); inner.appendChild(list);
    modal.appendChild(inner);
    // DOM 构建（textContent + addEventListener），杜绝 innerHTML 注入与 label 转义失真
    this.sceneMatches.forEach(function(s){
      var sec = document.createElement('div');
      sec.className = 'props-section'; sec.style.marginBottom = '8px';
      var h4 = document.createElement('h4');
      h4.textContent = s.name + ' ';
      var score = document.createElement('span');
      score.style.cssText = 'font-weight:400;color:var(--text3);font-size:11px';
      score.textContent = '匹配 ' + Math.round((s.match_score||0)*100) + '%';
      h4.appendChild(score);
      if(s.missing && s.missing.length){
        var ms = document.createElement('span');
        ms.style.cssText = 'color:var(--warn);font-size:11px';
        ms.textContent = '（缺 ' + s.missing.join('、') + '）';
        h4.appendChild(ms);
      }
      sec.appendChild(h4);
      var cfgs = s.configs || [];
      if(cfgs.length === 0){
        var emptyTip = document.createElement('div');
        emptyTip.style.cssText = 'font-size:11px;color:var(--text3);padding:4px 0';
        emptyTip.textContent = '该场景暂无可用摆法';
        sec.appendChild(emptyTip);
      } else {
        cfgs.forEach(function(cfg){
          var b = document.createElement('button');
          b.className = 'btn btn-sm'; b.style.margin = '2px 4px 2px 0';
          b.textContent = cfg.label;
          b.title = cfg.description || ('应用「'+cfg.label+'」摆法');
          b.addEventListener('click', function(){ editor.applySceneConfig(s.scene_id, cfg.label); });
          sec.appendChild(b);
        });
      }
      list.appendChild(sec);
    });
    document.body.appendChild(modal);
  },
  applySceneConfig: async function(sceneId, configLabel){
    var modal = document.querySelector('.modal-overlay'); if(modal) modal.remove();
    var payload = {scene_id: sceneId, config_label: configLabel,
                   components: this.components.map(function(c){ return {type: c.type}; })};
    var r;
    try { r = await window.$API.post('/api/diagram/semantic-resolve', payload); }
    catch(e){ this.toast('解析组合方案失败: '+e.message, 'error'); return; }
    if(!r || !r.label){ this.toast('组合方案不存在', 'error'); return; }
    // 先保留显式快照，即使 undo 栈异常也能可靠回滚
    var preSnap = {
      comps: JSON.parse(JSON.stringify(this._stripOverrides(this.components))),
      uf: JSON.parse(JSON.stringify(this.ratioUF))
    };
    this._pushUndo();
    try {
      // 1) 摆法参数写入目标组件实例：先清旧摆法键再写新，防止跨摆法残留
      var params = r.params || {};
      var pTarget = r.params_target || '';
      if(pTarget && Object.keys(params).length){
        var ti = this._findCompIdxByType(pTarget);
        if(ti >= 0){
          var tc = this.components[ti];
          for(var pi=0; pi<this._SCENE_PARAM_KEYS.length; pi++){ delete tc[this._SCENE_PARAM_KEYS[pi]]; }
          for(var k in params){ tc[k] = params[k]; }  // 顶层键优先被 _render_component 读取
          tc.params = Object.assign({}, params);
          this._refreshCompPreview(tc);
        }
      }
      // 2) 端口对齐落位（ring 端口随 clamp_y 动态修正；锁定元件不挪动）
      var stat = this._applyPortBindings(r.port_bindings || [], params.clamp_y);
      // 3) 渲染（z 序由 renderCanvas 保证，数组索引不变）
      this.renderCanvas();
      this.renderProps();
      var notes = [];
      if(r.warnings && r.warnings.length) notes = notes.concat(r.warnings);
      if(stat.skippedLocked) notes.push(stat.skippedLocked + ' 个锁定元件未移动');
      if(notes.length){
        this.toast('已部分应用「'+r.label+'」：'+notes.join('；'), 'warn');
      } else {
        this.toast('已应用组合摆法「'+r.label+'」', 'ok');
      }
    } catch(err) {
      console.error('applySceneConfig failed:', err);
      // 显式快照回滚，避免 undo 栈异常导致二次错误
      this.components = JSON.parse(JSON.stringify(preSnap.comps));
      this.ratioUF = JSON.parse(JSON.stringify(preSnap.uf));
      this._syncLinkGroups();
      this.renderCanvas();
      this.renderProps();
      this.toast('应用摆法失败，已回滚: '+err.message, 'error');
    }
  },
  _findCompIdxByType: function(type){
    for(var i=0;i<this.components.length;i++){
      if(this.components[i].type === type) return i;
    }
    return -1;
  },
  _portPos: function(comp, portId, ringClampY){
    var def = this.getDef(comp.type);
    if(!def || !def.ports_data) return null;
    var pd = null;
    for(var i=0;i<def.ports_data.length;i++){
      if(def.ports_data[i].id === portId){ pd = def.ports_data[i]; break; }
    }
    if(!pd) return null;
    var sx = def.default_w > 0 ? comp.w / def.default_w : 1;
    var sy = def.default_h > 0 ? comp.h / def.default_h : 1;
    var dy = pd.dy;
    // 铁圈端口随 clamp_y 动态修正（clamp_y 为组件默认高度坐标系像素）
    if(comp.type === 'iron_stand' && (portId === 'ring_top' || portId === 'ring_bottom')){
      var baseY = (typeof ringClampY === 'number' && isFinite(ringClampY)) ? ringClampY
                : ((typeof comp.clamp_y === 'number' && isFinite(comp.clamp_y)) ? comp.clamp_y : 42);
      baseY = Math.max(4, Math.min(73, baseY));  // 与后端 clamp [20, VH-40] 对应的默认系范围
      dy = (portId === 'ring_top') ? baseY - 2.5 : baseY + 2.5;  // ±2.5 ≈ 铁圈 ry 换算到默认系
    }
    return {x: comp.x + pd.dx * sx, y: comp.y + dy * sy};
  },
  _applyPortBindings: function(bindings, ringClampY){
    var stat = {moved: 0, skippedLocked: 0};
    var vb = this._parseViewBox(this.canvasSvg);
    // 迭代应用到不动点：链式绑定（A→B、B→C）第一轮先对齐的会被后续移动撕开，
    // 多轮应用可收敛（树状/链式结构）；3 轮上限防环形结构振荡
    for(var round=0; round<3; round++){
      var anyMoved = false;
      for(var bi=0; bi<bindings.length; bi++){
        var b = bindings[bi];
        if(!b || b.length < 4) continue;
        var si = this._findCompIdxByType(b[0]);
        var di = this._findCompIdxByType(b[2]);
        if(si < 0 || di < 0) continue;  // 缺组件跳过（resolve 已返回 warnings）
        var srcComp = this.components[si];
        var dstComp = this.components[di];
        var dstP = this._portPos(dstComp, b[3], ringClampY);
        var srcP = this._portPos(srcComp, b[1], ringClampY);
        if(!dstP || !srcP) continue;
        var dx = dstP.x - srcP.x, dy = dstP.y - srcP.y;
        if(Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;  // 已对齐
        // 根据锁定状态决定移动谁：都锁则跳过；只锁一个则移动另一个；都未锁默认移动 dst
        var moveComp = null;
        if(srcComp.locked && dstComp.locked){
          if(round === 0) stat.skippedLocked++;
          continue;
        } else if(srcComp.locked){
          moveComp = dstComp;
        } else if(dstComp.locked){
          moveComp = srcComp;
        } else {
          moveComp = dstComp;
        }
        moveComp.x = Math.round(moveComp.x + dx);
        moveComp.y = Math.round(moveComp.y + dy);
        moveComp.x = Math.max(vb[0] - moveComp.w + 10, Math.min(moveComp.x, vb[0] + vb[2] - 10));
        moveComp.y = Math.max(vb[1] - moveComp.h + 10, Math.min(moveComp.y, vb[1] + vb[3] - 10));
        anyMoved = true;
        if(round === 0) stat.moved++;
      }
      if(!anyMoved) break;
    }
    return stat;
  },
  _scheduleCompPreview: function(comp){
    clearTimeout(comp._previewTimer);
    comp._previewTimer=setTimeout(function(){
      delete comp._previewTimer;
      editor._refreshCompPreview(comp);
    },120);
  },
  _refreshCompPreview: function(comp){
    comp._previewSeq = (comp._previewSeq || 0) + 1;
    var seq = comp._previewSeq;
    var payload = {type: comp.type, w: comp.w, h: comp.h, label: comp.label || ''};
    var keys = ['liquid','liquid_color','filled','water_level','angle','temperature','rotation','clamp_y','clamp_w','has_ring','has_clamp','a','b','c','k','measure_type','min','max','value','position','direction'];
    for(var i=0;i<keys.length;i++){
      var k = keys[i];
      if(comp[k] !== undefined) payload[k] = comp[k];
    }
    window.$API.post('/api/gallery/render-component', payload).then(function(r){
      if(seq !== comp._previewSeq) return;  // 已有更新的预览请求
      if(r && r.svg){ comp.previewOverride = r.svg; editor.renderCanvas(); }
    }).catch(function(e){
      console.warn('preview refresh failed:', e);
      editor.toast('元件预览刷新失败，导出图仍以摆法参数为准', 'warn');
    });
  },
  // 恢复类操作（undo/快照/图库/题目加载）后，对带摆法参数的组件重建实例级预览
  _refreshParamPreviews: function(){
    // 实例级渲染键（非摆法管理，但需重建预览）：温度、液体颜色
    var _INSTANCE_RENDER_KEYS = ['temperature','liquid_color','measure_type','min','max','value','position','direction'];
    for(var i=0;i<this.components.length;i++){
      var c = this.components[i];
      var hasParams = false;
      for(var ki=0; ki<this._SCENE_PARAM_KEYS.length; ki++){
        if(c[this._SCENE_PARAM_KEYS[ki]] !== undefined){ hasParams = true; break; }
      }
      if(!hasParams){
        for(var ei=0; ei<_INSTANCE_RENDER_KEYS.length; ei++){
          if(c[_INSTANCE_RENDER_KEYS[ei]] !== undefined){ hasParams = true; break; }
        }
      }
      if(hasParams){ this._refreshCompPreview(c); }
      else if(c.previewOverride){ delete c.previewOverride; }
    }
  },
  _stripOverrides: function(comps){
    return comps.map(function(c){
      var o = {};
      for(var k in c){ if(k !== 'previewOverride' && k !== '_previewSeq' && k !== '_previewTimer') o[k] = c[k]; }
      return o;
    });
  },

  // ─── Snapshots ───
  saveSnapshot: function(){
    var snap = {
      comps: JSON.parse(JSON.stringify(this._stripOverrides(this.components))),
      uf: JSON.parse(JSON.stringify(this.ratioUF))
    };
    this.snapshots.push(snap);
    if(this.snapshots.length > 20) this.snapshots.shift();
    this.toast('快照已保存 ('+this.snapshots.length+')', 'ok');
  },
  loadSnapshot: function(){
    if(!this.snapshots.length){ this.toast('没有快照可恢复', 'error'); return; }
    this._pushUndo();
    var snap = this.snapshots[this.snapshots.length-1];
    this.components = JSON.parse(JSON.stringify(snap.comps || snap));
    this.ratioUF = JSON.parse(JSON.stringify(snap.uf || {parent:{},ratio:{}}));
    this.selectedIndices.clear();
    this._syncLinkGroups();
    this.renderCanvas();
    this.renderProps();
    this.toast('已恢复快照', 'ok');
    this._refreshParamPreviews();
    this.matchScenes();
  },

  // ─── Undo ───
  undo: function(){
    if(!this.undoStack.length){ this.toast('没有可撤销的操作', 'ok'); return; }
    // 把当前状态压入重做栈，再弹出撤销栈
    this.redoStack.push({
      comps: JSON.parse(JSON.stringify(this._stripOverrides(this.components))),
      uf: JSON.parse(JSON.stringify(this.ratioUF)),
    });
    var st = this.undoStack.pop();
    this.components = st.comps;
    this.ratioUF = st.uf || {parent: {}, ratio: {}};
    this._syncLinkGroups();
    this.selectedIndices.clear();
    this.renderCanvas();
    this.renderProps();
    this.toast('已撤销', 'ok');
    this.matchScenes();
    this._refreshParamPreviews();
  },
  redo: function(){
    if(!this.redoStack.length){ this.toast('没有可重做的操作', 'ok'); return; }
    // 把当前状态压回撤销栈，再弹出重做栈
    this.undoStack.push({
      comps: JSON.parse(JSON.stringify(this._stripOverrides(this.components))),
      uf: JSON.parse(JSON.stringify(this.ratioUF)),
    });
    var st = this.redoStack.pop();
    this.components = st.comps;
    this.ratioUF = st.uf || {parent: {}, ratio: {}};
    this._syncLinkGroups();
    this.selectedIndices.clear();
    this.renderCanvas();
    this.renderProps();
    this.toast('已重做', 'ok');
    this.matchScenes();
    this._refreshParamPreviews();
  },

  // ─── Templates, Save, Load, Calibration, Export (unchanged core) ───
  newFromTemplate: function(){
    var tpls = window._templates || [];
    if(!tpls.length){
      window.$API.get('/api/gallery/templates').then(function(r){
        if(r && r.templates) window._templates = r.templates;
        editor._showTemplatePicker(r.templates || []);
      }).catch(function(e){ editor.toast('加载模板失败: '+e.message, 'error'); });
    } else { this._showTemplatePicker(tpls); }
  },
  _showTemplatePicker: function(tpls){
    if(!tpls || !tpls.length){ this.toast('没有可用模板', 'error'); return; }
    var old = document.querySelector('.modal-overlay'); if(old) old.remove();
    var grid = document.createElement('div');
    grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px';
    tpls.forEach(function(t){
      var card = document.createElement('div');
      card.className = 'card'; card.style.cssText = 'cursor:pointer;text-align:center';
      var title = document.createElement('div');
      title.style.cssText = 'font-weight:600;font-size:13px'; title.textContent = t.name || '';
      var meta = document.createElement('div');
      meta.style.cssText = 'font-size:11px;color:var(--text3)'; meta.textContent = t.viewBox || '400x300';
      card.appendChild(title); card.appendChild(meta);
      card.addEventListener('click', function(){ editor.loadTemplate(t.id); });
      grid.appendChild(card);
    });
    var modal = document.createElement('div'); modal.className='modal-overlay show'; modal.style.display='flex';
    var inner = document.createElement('div');
    inner.className = 'modal'; inner.style.maxWidth = '600px';
    var closeBtn = document.createElement('button');
    closeBtn.className = 'modal-close'; closeBtn.innerHTML = '&times;';
    closeBtn.addEventListener('click', function(){ modal.remove(); });
    var h2 = document.createElement('h2'); h2.textContent = '选择模板';
    inner.appendChild(closeBtn); inner.appendChild(h2); inner.appendChild(grid);
    modal.appendChild(inner);
    document.body.appendChild(modal);
  },
  loadTemplate: function(tplId){
    var tpls = window._templates || [];
    var tpl = null;
    for(var i=0;i<tpls.length;i++){ if(tpls[i].id === tplId){ tpl = tpls[i]; break; } }
    if(!tpl){ this.toast('模板不存在', 'error'); return; }
    var modal = document.querySelector('.modal-overlay');
    if(modal) modal.remove();
    this._pushUndo();
    this.components = [];
    if(tpl.components && Array.isArray(tpl.components)){
      tpl.components.forEach(function(c){
        if(Array.isArray(c) && c.length >= 3){
          var ctype = c[0], cx = c[1], cy = c[2], params = c[3] || {};
          var def = editor.getDef(ctype);
          var comp = {
            type: ctype, name: def ? def.name : ctype,
            category: def ? def.category : 'chem',
            x: cx, y: cy, w: def ? def.default_w : 40, h: def ? def.default_h : 40,
            locked: false, label: params.label || (def ? def.name : ctype),
            annotation: '', shadow: false,
            link_group: null, link_ratio: 1,
          };
          if(params.liquid !== undefined) comp.liquid = params.liquid;
          editor.components.push(comp);
        }
      });
    }
    this.selectedIndices.clear();
    this._resetUF();
    this.renderCanvas();
    this.renderProps();
    this.toast('已加载模板: '+tpl.name+' ('+this.components.length+' 个元件)', 'ok');
    this.matchScenes();
  },
  saveToGallery: async function(name){
    name = name || await $prompt('输入图名称:', {default: '实验图_' + new Date().toLocaleDateString()}); if(name===null) return;
    if(!name) return;
    if(!this.components.length){ this.toast('画布为空', 'error'); return; }
    var spec = {
      components: JSON.parse(JSON.stringify(this._stripOverrides(this.components))),
      uf: JSON.parse(JSON.stringify(this.ratioUF)),
      title: name
    };
    window.$API.post('/api/gallery/presets', {name:name, spec:spec}).then(function(r){
      editor.toast('已保存到图库', 'ok'); editor.recordCalibration();
    }).catch(function(e){ editor.toast('保存失败: '+e.message, 'error'); });
  },
  saveToQuestion: async function(){
    var qid = this.questionId; if(!qid){ qid = await $prompt('输入题目ID (留空则保存到图库):', {default: ''}); if(qid===null) return; }
    if(!qid || qid === ''){ this.saveToGallery(); return; }
    if(!this.components.length){ this.toast('画布为空', 'error'); return; }
    var spec = {
      components: JSON.parse(JSON.stringify(this._stripOverrides(this.components))),
      uf: JSON.parse(JSON.stringify(this.ratioUF))
    };
    window.$API.post('/api/diagram/generate', {question_id: qid, prompt: '从编辑器保存', index: 0, spec_override: spec})
      .then(function(r){ editor.toast('已保存到题目 '+qid, 'ok'); editor.recordCalibration(); })
      .catch(function(e){ editor.toast('保存失败: '+e.message, 'error'); });
  },
  loadFromGallery: function(){
    var old = document.querySelector('.modal-overlay'); if(old) old.remove();
    window.$API.get('/api/gallery/presets').then(function(r){
      var presets = r.presets || [];
      if(!presets.length){ editor.toast('图库为空', 'error'); return; }
      var grid = document.createElement('div');
      grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;max-height:400px;overflow-y:auto';
      presets.forEach(function(p,i){
        var card = document.createElement('div');
        card.className = 'card'; card.style.cssText = 'cursor:pointer';
        var title = document.createElement('div');
        title.style.cssText = 'font-weight:600;font-size:13px'; title.textContent = p.name || '';
        var meta = document.createElement('div');
        meta.style.cssText = 'font-size:11px;color:var(--text3)'; meta.textContent = p.created || '';
        card.appendChild(title); card.appendChild(meta);
        card.addEventListener('click', function(){ editor._loadPreset(i); });
        var del = document.createElement('button');
        del.className = 'btn btn-sm btn-danger'; del.style.cssText = 'margin-top:6px;width:100%';
        del.textContent = '删除';
        del.addEventListener('click', function(ev){
          ev.stopPropagation();
          editor._deletePreset(p.id, p.name);
        });
        card.appendChild(del);
        grid.appendChild(card);
      });
      var modal = document.createElement('div'); modal.className='modal-overlay show'; modal.style.display='flex';
      var inner = document.createElement('div');
      inner.className = 'modal'; inner.style.maxWidth = '600px';
      var closeBtn = document.createElement('button');
      closeBtn.className = 'modal-close'; closeBtn.innerHTML = '&times;';
      closeBtn.addEventListener('click', function(){ modal.remove(); });
      var h2 = document.createElement('h2'); h2.textContent = '从图库加载';
      inner.appendChild(closeBtn); inner.appendChild(h2); inner.appendChild(grid);
      modal.appendChild(inner);
      document.body.appendChild(modal);
      window._presets = presets;
    }).catch(function(e){ editor.toast('加载失败: '+e.message, 'error'); });
  },
  _deletePreset: async function(id, name){
    if(!id) return;
    if(!await $confirm('删除图库「'+(name||'')+'」？此操作不可恢复。', {danger:true})) return;
    try{
      await window.$API.del('/api/gallery/presets/'+encodeURIComponent(id));
      editor.toast('已删除', 'ok');
      var modal = document.querySelector('.modal-overlay'); if(modal) modal.remove();
      editor.loadFromGallery();
    }catch(e){ editor.toast('删除失败: '+e.message, 'error'); }
  },
  // ─── 场景库浏览（semantic-scenes / semantic-search / check-safety）───
  showSceneBrowser: function(){
    var old = document.querySelector('.modal-overlay'); if(old) old.remove();
    var modal = document.createElement('div'); modal.className='modal-overlay show'; modal.style.display='flex';
    var inner = document.createElement('div');
    inner.className = 'modal'; inner.style.maxWidth = '640px';
    var closeBtn = document.createElement('button');
    closeBtn.className = 'modal-close'; closeBtn.innerHTML = '&times;';
    closeBtn.addEventListener('click', function(){ modal.remove(); });
    var h2 = document.createElement('h2'); h2.textContent = '语义场景库';
    var searchRow = document.createElement('div');
    searchRow.style.cssText = 'display:flex;gap:6px;margin:10px 0';
    var input = document.createElement('input');
    input.type = 'text'; input.placeholder = '按关键词搜索场景（如：加热）';
    input.style.cssText = 'flex:1;padding:8px 10px;border:1px solid var(--border);border-radius:var(--r-sm);background:var(--card-bg)';
    var clearBtn = document.createElement('button');
    clearBtn.className = 'btn btn-sm'; clearBtn.textContent = '全部';
    var list = document.createElement('div');
    list.className = 'scene-list'; list.style.cssText = 'max-height:440px;overflow-y:auto';
    searchRow.appendChild(input); searchRow.appendChild(clearBtn);
    inner.appendChild(closeBtn); inner.appendChild(h2); inner.appendChild(searchRow); inner.appendChild(list);
    modal.appendChild(inner);
    document.body.appendChild(modal);

    var searchTimer = null;
    function setLoading(){
      list.innerHTML = '<div class="empty"><span class="spin"></span> 加载中...</div>';
    }
    function renderScenes(scenes, withSafety){
      list.innerHTML = '';
      if(!scenes || !scenes.length){
        list.innerHTML = '<div class="empty">没有匹配的场景</div>';
        return;
      }
      var safetyCache = window._sceneSafetyCache = window._sceneSafetyCache || {};
      function safetyOf(src, dst, cb){
        var key = src+'|'+dst;
        if(safetyCache[key] !== undefined){ cb(safetyCache[key]); return; }
        window.$API.post('/api/diagram/check-safety', {src_type: src, dst_type: dst}).then(function(r){
          safetyCache[key] = (r && r.safe === false) ? (r.reason || '') : '';
          cb(safetyCache[key]);
        }).catch(function(){ safetyCache[key] = ''; cb(''); });
      }
      scenes.forEach(function(s){
        // semantic-scenes 返回全量结构；semantic-search 返回摘要结构，统一归一后再渲染
        var compList = s.components || s.components_required || [];
        var sec = document.createElement('div');
        sec.className = 'props-section'; sec.style.marginBottom = '8px';
        var h4 = document.createElement('h4');
        h4.textContent = s.name || '';
        var comps = document.createElement('span');
        comps.style.cssText = 'font-weight:400;color:var(--text3);font-size:11px';
        comps.textContent = ' 需要: ' + (compList.join('、') || '无');
        h4.appendChild(comps);
        sec.appendChild(h4);
        if(s.configs && s.configs.length){
          s.configs.forEach(function(cfg){
            var row = document.createElement('div');
            row.style.cssText = 'font-size:12px;color:var(--text2);padding:2px 0';
            row.textContent = cfg.label || '';
            if(withSafety && cfg.binding_pairs && cfg.binding_pairs.length){
              row.appendChild(document.createTextNode(' '));
              var tag = document.createElement('span');
              tag.className = 'tag'; tag.style.cssText = 'font-size:10px';
              tag.textContent = '安全: 检查中...';
              row.appendChild(tag);
              var pending = cfg.binding_pairs.length;
              var unsafe = [];
              cfg.binding_pairs.forEach(function(pair){
                if(!pair || !pair.src || !pair.dst){ pending--; return; }
                safetyOf(pair.src, pair.dst, function(reason){
                  if(reason) unsafe.push(reason);
                  pending--;
                  if(pending === 0){
                    if(unsafe.length){
                      tag.style.cssText = 'font-size:10px;background:var(--err-bg);color:var(--err)';
                      tag.textContent = '安全警告: ' + unsafe.join('；');
                    } else {
                      tag.style.cssText = 'font-size:10px;background:var(--ok-bg);color:var(--ok)';
                      tag.textContent = '连接安全';
                    }
                  }
                });
              });
              if(pending === 0){
                tag.style.cssText = 'font-size:10px;background:var(--ok-bg);color:var(--ok)';
                tag.textContent = '连接安全';
              }
            } else if(withSafety){
              var tag2 = document.createElement('span');
              tag2.className = 'tag'; tag2.style.cssText = 'font-size:10px;background:var(--ok-bg);color:var(--ok)';
              tag2.textContent = '连接安全';
              row.appendChild(tag2);
            }
            sec.appendChild(row);
          });
        } else if(s.config_count !== undefined && withSafety === false){
          // 摘要结构（semantic-search 结果）：只展示摆法数量与默认摆法
          var tip = document.createElement('div');
          tip.style.cssText = 'font-size:11px;color:var(--text3);padding:2px 0';
          tip.textContent = '共 ' + (s.config_count||0) + ' 种摆法' + (s.default_config ? ('，默认：' + s.default_config) : '');
          sec.appendChild(tip);
        } else {
          var tip2 = document.createElement('div');
          tip2.style.cssText = 'font-size:11px;color:var(--text3);padding:2px 0';
          tip2.textContent = '该场景暂无摆法配置';
          sec.appendChild(tip2);
        }
        list.appendChild(sec);
      });
    }
    function loadAll(){
      setLoading();
      window.$API.get('/api/diagram/semantic-scenes').then(function(r){
        renderScenes(r && r.scenes, true);
      }).catch(function(e){ list.innerHTML = '<div class="status-error" style="padding:6px">加载失败: '+$esc(e.message)+'</div>'; });
    }
    input.addEventListener('input', function(){
      clearTimeout(searchTimer);
      var q = input.value.trim();
      if(!q){ loadAll(); return; }
      searchTimer = setTimeout(function(){
        setLoading();
        window.$API.get('/api/diagram/semantic-search?q='+encodeURIComponent(q)).then(function(r){
          renderScenes(r && r.scenes, false);
        }).catch(function(e){ list.innerHTML = '<div class="status-error" style="padding:6px">搜索失败: '+$esc(e.message)+'</div>'; });
      }, 300);
    });
    clearBtn.addEventListener('click', function(){ input.value=''; loadAll(); });
    loadAll();
  },
  // ─── AI 插入元件（diagram/insert）───
  insertComponent: async function(){
    var qid = this.questionId;
    if(!qid){ qid = await $prompt('输入题目ID:', {default: ''}); if(qid===null) return; }
    if(!qid) return;
    var prompt = await $prompt('描述要插入的元件或标注（如：在烧杯上方加一个温度计）:', {default: ''});
    if(prompt===null) return;
    prompt = (prompt||'').trim();
    if(!prompt) return;
    // 以当前已加载的图作为插入基准，连续插入才能累积
    var targetIdx = Math.max(0, Math.min(9999, parseInt(this.lastDiagramIndex, 10) || 0));
    this.toast('AI 正在生成插入内容...', 'ok');
    var self = this;
    window.$API.post('/api/diagram/insert', {question_id: qid, prompt: prompt, index: targetIdx}).then(function(r){
      if(!r || !r.path){
        self.toast('插入失败：服务端未返回图片路径', 'error');
        return;
      }
      var m = /diagram_(\d+)\.svg/.exec(r.path);
      var idx = m ? parseInt(m[1], 10) : targetIdx;
      self.toast('已插入，正在重新加载...', 'ok');
      self.loadQuestionDiagram(qid, idx);
    }).catch(function(e){ self.toast('插入失败: '+e.message, 'error'); });
  },
  _loadPreset: function(index){
    var presets = window._presets || [];
    var p = presets[index];
    if(!p || !p.spec) return;
    document.querySelector('.modal-overlay')?.remove();
    this._pushUndo();
    this.components = JSON.parse(JSON.stringify(p.spec.components || []));
    this.ratioUF = JSON.parse(JSON.stringify(p.spec.uf || {parent:{},ratio:{}}));
    // Patch missing fields for backward compatibility
    for(var i=0;i<this.components.length;i++){
      var c = this.components[i];
      if(c.link_group === undefined) c.link_group = null;
      if(c.link_ratio === undefined) c.link_ratio = 1;
    }
    this.selectedIndices.clear();
    this._syncLinkGroups();
    this.renderCanvas();
    this.renderProps();
    this.toast('已加载: '+p.name, 'ok');
    this.checkCalibration();
    this.matchScenes();
    this._refreshParamPreviews();
  },
  loadQuestionDiagram: async function(qid, index){
    if(!qid){ qid = await $prompt('输入题目ID:', {default: ''}); if(qid===null) return; }
    if(!qid) return;
    if(index===undefined || index===null || isNaN(index)) index = 0;
    index = Math.max(0, Math.min(9999, parseInt(index, 10) || 0));
    this.questionId = qid;
    this.lastDiagramIndex = index;
    this.toast('正在加载题目 '+qid+' 的图...', 'ok');
    var self = this;
    // 统一走后端 spec 接口（含新鲜度校验），不再猜测 /static/questions 静态路径
    fetch('/api/diagram/spec/'+encodeURIComponent(qid)+'/'+index,{headers:{'X-Auth-Token':window.$API._authToken||'Ntmhzsgtc'}}).then(function(resp){
      if(resp.status === 404){
        // 仅“无此图”才回退到 SVG 检查；其它错误必须如实上报，不能掩盖服务器故障
        return fetch('/storage/questions/'+encodeURIComponent(qid)+'/diagram_'+index+'.svg',{headers:{'X-Auth-Token':window.$API._authToken||'Ntmhzsgtc'}}).then(function(r2){
          if(!r2.ok) throw new Error('该题目没有图片');
          self.toast('该题目有图但无元件信息，保存后可重新编辑', 'warn');
          return null;
        });
      }
      return resp.json().then(function(data){
        if(!resp.ok){
          var detail = '';
          if(data && typeof data.detail === 'string') detail = data.detail;
          throw new Error(detail || ('加载失败 ('+resp.status+')'));
        }
        return data;
      });
    }).then(function(r){
      if(r === null) return;  // 已走 SVG 回退提示
      var spec = r && r.spec;
      if(!spec || !(spec.components instanceof Array)){ throw new Error('无spec'); }
      self._pushUndo();
      self.components = JSON.parse(JSON.stringify(spec.components));
      // 兼容旧 spec：补 link_group/link_ratio 缺省，防止 renderProps toFixed 抛错
      for(var i=0;i<self.components.length;i++){
        var c = self.components[i];
        if(c.link_group === undefined) c.link_group = null;
        if(c.link_ratio === undefined) c.link_ratio = 1;
      }
      self.selectedIndices.clear();
      // 优先恢复 spec 中保存的比例锁定关系；旧 spec 无 uf 时降级重建
      if(spec.uf && spec.uf.parent){
        self.ratioUF = JSON.parse(JSON.stringify(spec.uf));
        self._syncLinkGroups();
      } else {
        self._resetUF();
      }
      self.renderCanvas(); self.renderProps();
      if(r.fresh){
        self.toast('已加载题目 '+qid+' 的图', 'ok');
      } else {
        self.toast('已加载题目 '+qid+' 的图；题目在上次保存后可能被修改，保存前请核对', 'warn');
      }
      self.matchScenes();
      self._refreshParamPreviews();
    }).catch(function(e){ self.toast(e && e.message ? e.message : '加载失败', 'error'); });
  },
  recordCalibration: function(){
    if(!this.components.length) return;
    window.$API.post('/api/diagram/calibrate', {components: this._stripOverrides(this.components)}).then(function(r){
      if(r && r.calibrated && r.calibrated_positions){
        editor.toast('校准完成! 已累计 '+r.sample_count+' 个样本', 'ok');
        editor._applyCalibratedPositions(r.calibrated_positions);
      } else if(r && r.sample_count > 0){
        editor.toast('已记录调整样本 ('+r.sample_count+'/5)', 'ok');
      }
    }).catch(function(e){ console.warn('Calibration record failed:', e); });
  },
  checkCalibration: async function(){
    if(!this.components.length) return;
    window.$API.post('/api/diagram/calibration-check', {components: this._stripOverrides(this.components)}).then(async function(r){
      var info = document.getElementById('calibrationInfo');
      if(r && r.has_calibration && r.calibrated_positions){
        info.innerHTML = '<span class="calibration-badge active">已校准</span>';
        editor.calibrationInfo = r;
        if(typeof $confirm==='function'){
          if(await $confirm('该装置组合已有校准数据，是否应用校准位置？')) editor._applyCalibratedPositions(r.calibrated_positions);
        }else if(window.confirm('该装置组合已有校准数据，是否应用校准位置？')){
          editor._applyCalibratedPositions(r.calibrated_positions);
        }
      } else if(r && r.sample_count > 0){
        info.innerHTML = '<span class="calibration-badge pending">样本:'+r.sample_count+'/5</span>';
      } else { info.innerHTML = ''; }
    }).catch(function(e){if(typeof $toast==='function')$toast(e.message||'校准检查失败','error')});
  },
  _applyCalibratedPositions: function(positions){
    if(!positions) return;
    this._pushUndo();
    positions.forEach(function(cal){
      for(var i=0;i<editor.components.length;i++){
        if(editor.components[i].type === cal.type){
          editor.components[i].x = cal.x;
          editor.components[i].y = cal.y;
          editor.components[i].locked = false;
        }
      }
    });
    this.renderCanvas(); this.renderProps();
    this.toast('已应用校准位置并自动解锁', 'ok');
  },
  showCalibration: function(){
    window.$API.get('/api/diagram/calibration-summary').then(function(r){
      var combos = r.combinations || [];
      var wrap = document.createElement('div');
      wrap.style.cssText = 'max-height:400px;overflow-y:auto';
      var table = document.createElement('table');
      table.style.cssText = 'width:100%;font-size:12px';
      var thead = document.createElement('thead');
      var headTr = document.createElement('tr');
      ['组合','元件数','样本数','状态'].forEach(function(h){
        var th = document.createElement('th'); th.textContent = h; headTr.appendChild(th);
      });
      thead.appendChild(headTr); table.appendChild(thead);
      var tbody = document.createElement('tbody');
      combos.forEach(function(c){
        var tr = document.createElement('tr');
        var typesTd = document.createElement('td');
        typesTd.style.cssText = 'font-family:monospace;font-size:10px';
        typesTd.textContent = (c.component_types || []).join(', ');
        var countTd = document.createElement('td'); countTd.textContent = (c.component_types||[]).length;
        var sampleTd = document.createElement('td'); sampleTd.textContent = c.sample_count;
        var statusTd = document.createElement('td');
        var badge = document.createElement('span');
        badge.className = 'calibration-badge ' + (c.has_calibration ? 'active' : 'pending');
        badge.textContent = c.has_calibration ? '已校准' : '待积累';
        statusTd.appendChild(badge);
        tr.appendChild(typesTd); tr.appendChild(countTd); tr.appendChild(sampleTd); tr.appendChild(statusTd);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody); wrap.appendChild(table);
      var modal = document.createElement('div'); modal.className='modal-overlay show'; modal.style.display='flex';
      var inner = document.createElement('div');
      inner.className = 'modal'; inner.style.maxWidth = '650px';
      var closeBtn = document.createElement('button');
      closeBtn.className = 'modal-close'; closeBtn.innerHTML = '&times;';
      closeBtn.addEventListener('click', function(){ modal.remove(); });
      var h2 = document.createElement('h2'); h2.textContent = '位置校准统计';
      var hint = document.createElement('p');
      hint.style.cssText = 'font-size:12px;color:var(--text2);margin-bottom:10px';
      hint.textContent = '同一装置组合调整 5 次后去掉极值取平均并解锁';
      inner.appendChild(closeBtn); inner.appendChild(h2); inner.appendChild(hint); inner.appendChild(wrap);
      modal.appendChild(inner);
      document.body.appendChild(modal);
    }).catch(function(e){ editor.toast('加载校准统计失败', 'error'); });
  },
  exportSVG: function(){
    if(!this.components.length){ this.toast('没有元件可导出', 'error'); return; }
    var spec = { components: JSON.parse(JSON.stringify(this._stripOverrides(this.components))) };
    this.toast('正在渲染真实SVG...', 'ok');
    window.$API.post('/api/diagram/render-svg', {spec: spec}).then(function(r){
      if(r && r.svg){
        var blob = new Blob([r.svg], {type:'image/svg+xml'});
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a'); a.href = url; a.download = 'diagram_export_'+Date.now()+'.svg';
        a.click(); URL.revokeObjectURL(url);
        if(r.warnings && r.warnings.length) editor.toast('已导出，后端自动修正 '+r.warnings.length+' 项参数', 'warn');
        else editor.toast('真实SVG已导出', 'ok');
      } else { editor.toast('导出失败: 后端无返回', 'error'); }
    }).catch(function(e){ editor.toast('导出失败: '+e.message, 'error'); });
  },

  // ─── Helpers ───
  getDef: function(type){
    for(var i=0;i<this.allComponentDefs.length;i++){
      if(this.allComponentDefs[i].type === type) return this.allComponentDefs[i];
    }
    return null;
  },
  _parseViewBox: function(svg){
    var raw = svg.getAttribute('viewBox') || '0 0 400 300';
    var parts = raw.trim().replace(/,/g, ' ').split(/\s+/).filter(function(v){ return v !== ''; });
    var nums = parts.map(Number);
    if(nums.length !== 4 || nums.some(function(v){ return !isFinite(v); })) return [0, 0, 400, 300];
    return nums;
  },
  toast: function(msg, type){
    $toast(msg, type);
  },
};
document.addEventListener('DOMContentLoaded', function(){ editor.init(); });


