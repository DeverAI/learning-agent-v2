// offline.js - 离线增强层（IndexedDB 只读缓存 + 上传队列）
// 此文件先于 app.js 加载，通过 window._offline 供 $API._request 挂接。
// 设计：Techniques.md §11.6 / Design.md §13.2。
// 不依赖 Service Worker（本项目经 http://IP 访问，SW 不可用）。

(function () {
  'use strict';
  if (!('indexedDB' in window)) { window._offline = null; return; }

  var DB_NAME = 'la_offline';
  var DB_VERSION = 1;
  var CACHE_MAX = 200;

  // GET 只读缓存白名单（前缀匹配；写操作结果与隐私临时记录一律不缓存）
  var GET_CACHE_PREFIXES = [
    '/api/questions', '/api/papers', '/api/notes', '/api/banks',
    '/api/profile', '/api/daily-quote', '/api/system-messages',
    '/api/knowledge-graph', '/api/lessons',
  ];
  // 上传/提交队列白名单（离线入队，联网重放；其余写操作离线直接提示失败）
  var QUEUE_POST_PREFIXES = [
    '/api/ocr/upload', '/api/ocr/upload-multi', '/api/notes/upload-images',
  ];
  var QUEUE_POST_RE = [
    /^\/api\/ocr\/session\/[^/]+\/upload$/,
    /^\/api\/search\/upload$/,
    /^\/api\/correction\/upload$/,
  ];
  var MAX_RETRIES = 5;

  var _db = null;
  var _replayTimer = null;

  function openDb() {
    if (_db) return Promise.resolve(_db);
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains('cache')) db.createObjectStore('cache', { keyPath: 'url' });
        if (!db.objectStoreNames.contains('queue')) db.createObjectStore('queue', { keyPath: 'id', autoIncrement: true });
      };
      req.onsuccess = function () { _db = req.result; resolve(_db); };
      req.onerror = function () { reject(req.error || new Error('IndexedDB 打开失败')); };
    });
  }

  function tx(store, mode) {
    return openDb().then(function (db) { return db.transaction(store, mode).objectStore(store); });
  }

  function idbReq(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error || new Error('IndexedDB 操作失败')); };
    });
  }

  function isOnline() { return navigator.onLine !== false; }
  function pathOf(u) {
    try { return new URL(u, location.href).pathname; } catch (e) { return u; }
  }
  function cacheableGet(u) {
    var p = pathOf(u);
    return GET_CACHE_PREFIXES.some(function (pre) { return p === pre || p.indexOf(pre + '/') === 0 || p.indexOf(pre + '?') === 0; });
  }
  function queueable(u, method) {
    var p = pathOf(u);
    if (method !== 'POST' && method !== 'PUT') return false;
    return QUEUE_POST_PREFIXES.some(function (pre) { return p === pre || p.indexOf(pre + '/') === 0; })
      || QUEUE_POST_RE.some(function (re) { return re.test(p); });
  }

  function putCache(url, status, body, ctype) {
    return tx('cache', 'readwrite').then(function (st) {
      return idbReq(st.put({ url: url, status: status, body: body, ctype: ctype, ts: Date.now() }));
    }).then(function () { return trimCache(); }).catch(function () { });
  }
  function getCache(url) {
    return tx('cache', 'readonly').then(function (st) { return idbReq(st.get(url)); }).catch(function () { return undefined; });
  }
  function trimCache() {
    return tx('cache', 'readonly').then(function (st) { return idbReq(st.getAll()); }).then(function (items) {
      if (!Array.isArray(items) || items.length <= CACHE_MAX) return;
      items.sort(function (a, b) { return a.ts - b.ts; });
      var excess = items.slice(0, items.length - CACHE_MAX);
      return tx('cache', 'readwrite').then(function (st) {
        excess.forEach(function (it) { st.delete(it.url); });
        return new Promise(function (r) { st.transaction.oncomplete = r; });
      });
    }).catch(function () { });
  }
  function enqueue(item) {
    return tx('queue', 'readwrite').then(function (st) { return idbReq(st.add(item)); });
  }
  function allQueue() {
    return tx('queue', 'readonly').then(function (st) { return idbReq(st.getAll()); }).catch(function () { return []; });
  }
  function deleteQueue(id) {
    return tx('queue', 'readwrite').then(function (st) { return idbReq(st.delete(id)); }).catch(function () { });
  }
  function updateQueue(item) {
    return tx('queue', 'readwrite').then(function (st) { return idbReq(st.put(item)); }).catch(function () { });
  }

  // 序列化 FormData 为可克隆 entries（File/Blob 均可进 IndexedDB）
  function serializeBody(body) {
    if (!body) return null;
    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      return { type: 'formdata', entries: Array.from(body.entries()).map(function (e) {
        return { key: e[0], value: e[1] };
      }) };
    }
    if (typeof body === 'string') return { type: 'text', value: body };
    return null; // 其它类型不支持离线排队
  }
  function rebuildBody(serialized) {
    if (!serialized) return null;
    if (serialized.type === 'formdata' && typeof FormData !== 'undefined') {
      var fd = new FormData();
      serialized.entries.forEach(function (e) { fd.append(e.key, e.value); });
      return fd;
    }
    if (serialized.type === 'text') return serialized.value;
    return null;
  }

  // 入队：离线时把可排队请求写入 IndexedDB
  function enqueueRequest(u, opts) {
    var body = serializeBody(opts && opts.body);
    if (body === null) return Promise.reject(new Error('当前离线，此操作不支持离线使用，请联网后重试'));
    return enqueue({
      url: u,
      method: (opts && opts.method) || 'POST',
      headers: (opts && opts.headers) || {},
      body: body,
      ts: Date.now(),
      retries: 0,
    }).then(function () {
      scheduleReplay();
      throw new Error('当前离线，已加入队列，联网后将自动上传');
    });
  }

  // 重放：按序上传队列请求，成功删除并提示，失败保留计数。
  // _replaying 守卫：启动补传/online 事件/60s 定时可能并发触发，防止同一队列项重复上传。
  var _replaying = false;
  function replay(force) {
    if (_replaying) return Promise.resolve(0);
    if (!isOnline() && !force) return Promise.resolve(0);
    _replaying = true;
    return allQueue().then(function (items) {
      if (!items || !items.length) return 0;
      items.sort(function (a, b) { return (a.id || 0) - (b.id || 0); });
      var done = 0;
      var chain = Promise.resolve();
      items.forEach(function (item) {
        chain = chain.then(function () {
          if ((item.retries || 0) >= MAX_RETRIES) return null;
          var body = rebuildBody(item.body);
          if (body === null) return deleteQueue(item.id);
          var opts = { method: item.method || 'POST', headers: item.headers || {}, body: body };
          return fetch(item.url, opts).then(function (r) {
            if (r.ok) { done += 1; return deleteQueue(item.id); }
            item.retries = (item.retries || 0) + 1;
            return updateQueue(item);
          }).catch(function () {
            item.retries = (item.retries || 0) + 1;
            return updateQueue(item);
          });
        });
      });
      return chain.then(function () {
        if (done > 0 && typeof $toast === 'function') $toast('离线内容已同步上传（' + done + ' 项）', 'ok');
        return done;
      });
    }).finally(function () { _replaying = false; });
  }

  function scheduleReplay() {
    if (_replayTimer) return;
    _replayTimer = setInterval(function () {
      allQueue().then(function (items) {
        if (items && items.length && isOnline()) return replay();
      }).catch(function () { });
    }, 60000);
  }

  // $API._request 挂接入口
  // doFetchRaw: () => Promise<Response>（原始 fetch，网络层失败时 reject）
  // doParse: (Response) => 解析后的数据（复用 $API._parse，含错误状态映射）
  function handle(u, opts, type, doFetchRaw, doParse) {
    var method = ((opts && opts.method) || 'GET').toUpperCase();
    var online = isOnline();

    if (method === 'GET' && cacheableGet(u)) {
      if (online) {
        // network-first：网络层成功则回写缓存（HTTP 4xx/5xx 不回退缓存，交由 $API 错误分支）
        return doFetchRaw().then(function (resp) {
          if (resp && resp.ok) {
            try {
              var clone = resp.clone();
              clone.text().then(function (t) {
                putCache(u, resp.status, t, resp.headers.get('content-type') || 'application/json');
              }).catch(function () { });
            } catch (e) { }
          }
          return doParse(resp);
        }).catch(function (err) {
          // 仅网络层失败（err.isNetwork）回退缓存；HTTP 4xx/5xx 原样抛出，不得用旧缓存掩盖
          if (!(err && err.isNetwork)) throw err;
          return getCache(u).then(function (cached) {
            if (cached) {
              if (typeof $toast === 'function') $toast('网络异常，当前展示离线缓存数据', 'warn');
              return doParse(buildCachedResponse(cached));
            }
            throw err;
          });
        });
      }
      return getCache(u).then(function (cached) {
        if (cached) {
          if (typeof $toast === 'function') $toast('当前离线，展示缓存数据', 'warn');
          return doParse(buildCachedResponse(cached));
        }
        throw new Error('当前离线，且没有可用的缓存数据');
      });
    }

    if (!online && method !== 'GET') {
      if (queueable(u, method)) {
        return enqueueRequest(u, opts);
      }
      return Promise.reject(new Error('当前离线，此操作需要联网，请恢复网络后重试'));
    }

    return doFetchRaw().then(doParse);
  }

  function buildCachedResponse(cached) {
    var headers = new Headers({ 'Content-Type': cached.ctype || 'application/json', 'X-Offline-Cache': '1' });
    return new Response(cached.body, { status: cached.status || 200, headers: headers });
  }

  window._offline = {
    handle: handle,
    replay: replay,
    enqueueRequest: enqueueRequest,
    isOnline: isOnline,
    cacheableGet: cacheableGet,
    queueable: queueable,
    _start: function () {
      window.addEventListener('online', function () {
        if (typeof $toast === 'function') $toast('网络已恢复', 'ok');
        replay();
      });
      window.addEventListener('offline', function () {
        if (typeof $toast === 'function') $toast('当前离线：可查看已缓存内容，上传将排队', 'warn');
      });
      replay(); // 启动时补传
      scheduleReplay();
    },
  };

  // 自动启动。此前 _start 全仓零调用点（只有定义），导致：
  //   ① online/offline 的网络状态提示永不触发；
  //   ② 上次会话遗留的上传队列在重开应用后不会自动补传——scheduleReplay 只在
  //      enqueueRequest 里被被动调用，必须先有一次新入队才会开始轮询。
  // 本文件由 base.html 在 enable_offline 为真时以 defer 引入，启动时机安全；
  // $toast 未就绪时 _start 内部已有 typeof 守卫。
  try {
    window._offline._start();
  } catch (e) {
    if (typeof console !== 'undefined' && console.warn) console.warn('offline layer start failed:', e);
  }
})();
