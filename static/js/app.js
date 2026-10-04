/* EduPortal client script. No inline JS anywhere (strict Content-Security-Policy). */
(function () {
  'use strict';
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var csrf = function () { return document.body.getAttribute('data-csrf') || ''; };

  document.addEventListener('DOMContentLoaded', function () {
    /* ---- progress meters (widths come from data attributes so no inline styles are needed) ---- */
    $$('[data-width]').forEach(function (el) { el.style.width = Math.max(0, Math.min(100, parseFloat(el.getAttribute('data-width')) || 0)) + '%'; });
    window.requestAnimationFrame(function () { document.documentElement.classList.add('meters-ready'); });

    /* ---- mobile nav + theme ---- */
    $$('[data-nav-toggle]').forEach(function (b) { b.addEventListener('click', function () { document.body.classList.toggle('nav-open'); }); });
    $$('[data-theme-toggle]').forEach(function (b) {
      b.addEventListener('click', function () {
        var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('theme', next); } catch (e) {}
      });
    });

    /* ---- generic helpers ---- */
    $$('[data-toggle]').forEach(function (btn) {
      btn.addEventListener('click', function () { var t = document.getElementById(btn.getAttribute('data-toggle')); if (t) t.hidden = !t.hidden; });
    });
    $$('form[data-confirm]').forEach(function (form) {
      form.addEventListener('submit', function (e) { if (!window.confirm(form.getAttribute('data-confirm'))) e.preventDefault(); });
    });
    $$('[data-autosubmit]').forEach(function (s) { s.addEventListener('change', function () { s.form.submit(); }); });
    $$('[data-print]').forEach(function (b) { b.addEventListener('click', function () { window.print(); }); });
    $$('[data-reload]').forEach(function (b) { b.addEventListener('click', function () { window.location.reload(); }); });
    $$('[data-toggle-password]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var input = document.getElementById(btn.getAttribute('data-toggle-password')); var hide = input.type === 'password';
        input.type = hide ? 'text' : 'password'; btn.textContent = hide ? 'Hide' : 'Show';
      });
    });

    /* ---- login: swap student / staff fields ---- */
    var roles = $$('input[name="role"]');
    if (roles.length) {
      var syncRole = function () {
        var role = $('input[name="role"]:checked').value;
        $$('[data-role-field]').forEach(function (f) {
          var on = f.getAttribute('data-role-field') === (role === 'staff' ? 'staff' : 'student');
          f.hidden = !on; $$('input', f).forEach(function (i) { i.disabled = !on; });
        });
      };
      roles.forEach(function (r) { r.addEventListener('change', function () { syncRole(); var f = $(role_first()); if (f) f.focus(); }); });
      var role_first = function () { return $('input[name="role"]:checked').value === 'staff' ? '#username' : '#student_id'; };
      syncRole(); var first = $(role_first()); if (first) first.focus();
    }

    /* ---- table search / status filter ---- */
    var applyFilter = function (table) {
      var box = $('[data-filter="#' + table.id + '"]');
      var status = $('[data-status-filter="#' + table.id + '"]');
      var q = box ? box.value.trim().toLowerCase() : '';
      var st = status ? status.value : '';
      var lastVisible = true;
      $$('tbody tr', table).forEach(function (tr) {
        if (tr.classList.contains('subrow')) { if (!lastVisible) tr.hidden = true; return; }
        var ok = (!q || tr.textContent.toLowerCase().indexOf(q) !== -1) && (!st || tr.getAttribute('data-status') === st);
        if (tr.querySelector('.empty')) ok = true;
        tr.hidden = !ok; lastVisible = ok;
      });
    };
    $$('[data-filter]').forEach(function (box) {
      var table = $(box.getAttribute('data-filter')); if (!table) return;
      box.addEventListener('input', function () { applyFilter(table); });
    });
    $$('[data-status-filter]').forEach(function (sel) {
      var table = $(sel.getAttribute('data-status-filter')); if (!table) return;
      sel.addEventListener('change', function () { applyFilter(table); });
    });

    /* ---- exam question editor: show fields for the chosen type ---- */
    $$('[data-qtype]').forEach(function (sel) {
      var form = sel.closest('form');
      var sync = function () { $$('[data-qfields]', form).forEach(function (b) { var on = b.getAttribute('data-qfields') === sel.value; b.hidden = !on; $$('input,select', b).forEach(function (i) { i.disabled = !on; }); }); };
      sel.addEventListener('change', sync); sync();
    });
    $$('[data-policy]').forEach(function (sel) {
      var form = sel.closest('form'); var pen = $('[data-policy-penalty]', form);
      var sync = function () { if (pen) pen.hidden = sel.value !== 'penalty'; };
      sel.addEventListener('change', sync); sync();
    });

    initExamTimer();
    initVideoUpload();
    initPlayer();
    initPwa();
  });

  /* ======================= exam timer + local draft ======================= */
  function initExamTimer() {
    var form = document.getElementById('exam-form'); if (!form) return;
    var key = 'exam-draft:' + form.getAttribute('action');
    try {   // restore answers if the page was reloaded
      var draft = JSON.parse(localStorage.getItem(key) || '{}');
      Object.keys(draft).forEach(function (name) {
        var els = form.elements[name]; if (!els) return;
        if (els.length && els[0] && els[0].type === 'radio') { for (var i = 0; i < els.length; i++) if (els[i].value === draft[name]) els[i].checked = true; }
        else if (els.type === 'text') els.value = draft[name];
      });
    } catch (e) {}
    var save = function () {
      try {
        var data = {}; new FormData(form).forEach(function (v, k) { if (k.indexOf('q_') === 0) data[k] = v; });
        localStorage.setItem(key, JSON.stringify(data));
      } catch (e) {}
    };
    form.addEventListener('change', save); form.addEventListener('input', save);
    form.addEventListener('submit', function () { try { localStorage.removeItem(key); } catch (e) {} });

    var timer = document.getElementById('timer'); if (!timer) return;
    var end = Date.now() + parseInt(timer.getAttribute('data-remaining'), 10) * 1000;
    var text = document.getElementById('timer-text'); var fired = false;
    var tick = function () {
      var left = Math.max(0, Math.round((end - Date.now()) / 1000));
      var h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = left % 60;
      text.textContent = (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
      timer.classList.toggle('low', left <= 60);
      if (left <= 0 && !fired) { fired = true; text.textContent = 'Time is up — submitting…'; try { localStorage.removeItem(key); } catch (e) {} form.removeAttribute('data-confirm'); form.submit(); }
    };
    tick(); setInterval(tick, 250);
  }

  /* ======================= video upload (duration + progress) ======================= */
  function initVideoUpload() {
    var form = $('[data-video-form]'); if (!form) return;
    var file = $('[data-video-file]', form), dur = $('[data-video-duration]', form), info = $('[data-video-info]', form);
    var submit = $('[data-submit]', form), box = $('[data-progress]', form), bar = $('[data-progress-bar]', form), txt = $('[data-progress-text]', form);
    var fmt = function (s) { s = Math.round(s); var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); };
    var mb = function (b) { return (b / 1048576).toFixed(1) + ' MB'; };
    var limit = parseInt(document.body.getAttribute('data-max-video') || '0', 10);

    file.addEventListener('change', function () {
      dur.value = '0'; var f = file.files[0]; if (!f) { info.textContent = 'Choose a file to begin.'; return; }
      info.textContent = 'Reading video… (' + mb(f.size) + ')';
      var probe = document.createElement('video'); probe.preload = 'metadata'; var url = URL.createObjectURL(f);
      probe.onloadedmetadata = function () {
        if (isFinite(probe.duration) && probe.duration > 0) { dur.value = probe.duration.toFixed(2); info.textContent = 'Length ' + fmt(probe.duration) + ' · ' + mb(f.size); }
        URL.revokeObjectURL(url);
      };
      probe.onerror = function () { info.textContent = 'This browser cannot read that file. Convert it to MP4 (H.264/AAC) or WebM.'; URL.revokeObjectURL(url); };
      probe.src = url;
    });

    form.addEventListener('submit', function (e) {
      if (!window.XMLHttpRequest || !window.FormData) return;     // plain post fallback
      e.preventDefault();
      var xhr = new XMLHttpRequest(); xhr.open('POST', form.action);
      xhr.setRequestHeader('X-Requested-With', 'XHR-upload');
      xhr.upload.onprogress = function (ev) {
        if (!ev.lengthComputable) return; box.hidden = false;
        var p = Math.round(100 * ev.loaded / ev.total); bar.style.width = p + '%';
        txt.textContent = p < 100 ? 'Uploading… ' + p + '% (' + mb(ev.loaded) + ' of ' + mb(ev.total) + ')' : 'Processing on the server…';
      };
      xhr.onload = function () {
        var target = window.location.href;
        try { var res = JSON.parse(xhr.responseText); if (res.redirect) target = res.redirect; } catch (err) {}
        window.location.href = target;
      };
      xhr.onerror = function () { submit.disabled = false; box.hidden = false; txt.textContent = 'Upload failed — check your connection and try again.'; };
      submit.disabled = true; box.hidden = false; txt.textContent = 'Starting upload…';
      xhr.send(new FormData(form));
    });
  }

  /* ======================= video player: restrictions + heartbeats ======================= */
  function initPlayer() {
    var v = document.getElementById('player'); if (!v) return;
    var tracked = v.getAttribute('data-tracked') === '1';
    var url = v.getAttribute('data-progress-url');
    var completed = v.getAttribute('data-completed') === '1';
    var resume = parseFloat(v.getAttribute('data-resume')) || 0;
    var maxAllowed = Math.max(parseFloat(v.getAttribute('data-furthest')) || 0, resume);
    var msg = document.getElementById('player-msg'), msgTimer = null;
    var say = function (t) { if (!msg) return; msg.textContent = t; msg.hidden = false; clearTimeout(msgTimer); msgTimer = setTimeout(function () { msg.hidden = true; }, 3500); };
    var lastTime = 0;

    v.addEventListener('loadedmetadata', function () { if (tracked && !completed && resume > 5 && resume < v.duration - 3) { v.currentTime = resume; lastTime = resume; say('Resuming where you left off'); } });
    if (!tracked) return;          // teacher preview: no restrictions, nothing recorded

    var restricted = function () { return !completed; };
    v.addEventListener('timeupdate', function () {
      if (v.seeking) return;
      if (Math.abs(v.currentTime - lastTime) < 2) maxAllowed = Math.max(maxAllowed, v.currentTime);
      lastTime = v.currentTime;
    });
    v.addEventListener('seeking', function () {
      if (restricted() && v.currentTime > maxAllowed + 1.5) { v.currentTime = maxAllowed; say('Skipping ahead is disabled until you have watched that part.'); }
    });
    v.addEventListener('ratechange', function () { if (restricted() && v.playbackRate !== 1) { v.playbackRate = 1; say('Playback speed is fixed at 1×.'); } });
    document.addEventListener('visibilitychange', function () { if (document.hidden && !v.paused) { v.pause(); say('Paused — the video only plays while you are watching it.'); } });

    var fill = document.getElementById('watch-fill'), pctEl = document.getElementById('watch-pct'), label = document.getElementById('watch-label');
    var inflight = false;
    var send = function (state) {
      if (inflight && state === 'playing') return; inflight = true;
      fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
        body: JSON.stringify({ position: v.currentTime, state: state }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          inflight = false; if (!d || !d.ok) return;
          if (fill) fill.style.width = d.percent + '%'; if (pctEl) pctEl.textContent = d.percent + '%';
          if (d.completed && !completed) {
            completed = true; if (fill) fill.classList.add('ok'); say('Completed — well done!');
            if (label) label.textContent = 'Completed — thank you for watching!';
          }
        }).catch(function () { inflight = false; });
    };
    v.addEventListener('playing', function () { send('playing'); });
    v.addEventListener('pause', function () { if (!v.ended) send('paused'); });
    v.addEventListener('ended', function () { send('ended'); });
    setInterval(function () { if (!v.paused && !v.seeking && !v.ended) send('playing'); }, 3000);
  }

  /* ======================= PWA ======================= */
  function initPwa() {
    if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
    }
    var deferred = null; var btn = $('[data-install]');
    window.addEventListener('beforeinstallprompt', function (e) { e.preventDefault(); deferred = e; if (btn) btn.hidden = false; });
    if (btn) btn.addEventListener('click', function () { if (deferred) { deferred.prompt(); deferred = null; btn.hidden = true; } });
  }
})();
