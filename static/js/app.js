/* EduPortal client script. No inline JS is used anywhere (strict Content-Security-Policy). */
document.documentElement.classList.add('js');

document.addEventListener('DOMContentLoaded', function () {
  // ---- Dashboard tabs (hash-based so reloads and redirects keep your place) ----
  var panels = document.querySelectorAll('[data-panel]');
  var tabs = document.querySelectorAll('[data-tab]');
  if (panels.length) {
    var names = Array.prototype.map.call(panels, function (p) { return p.getAttribute('data-panel'); });
    var show = function () {
      var name = (location.hash || '').replace('#', '');
      if (names.indexOf(name) === -1) name = names[0];
      panels.forEach(function (p) { p.classList.toggle('active', p.getAttribute('data-panel') === name); });
      tabs.forEach(function (t) { t.classList.toggle('active', t.getAttribute('data-tab') === name); });
    };
    window.addEventListener('hashchange', function () { show(); window.scrollTo(0, 0); });
    show();
  }

  // ---- Show/hide extra rows (add question, edit assignment) ----
  document.querySelectorAll('[data-toggle]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var row = document.getElementById(btn.getAttribute('data-toggle'));
      if (row) row.hidden = !row.hidden;
    });
  });

  // ---- Confirm dialogs for destructive / final actions ----
  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (!window.confirm(form.getAttribute('data-confirm'))) e.preventDefault();
    });
  });

  // ---- Login: swap student / teacher fields ----
  var roleInputs = document.querySelectorAll('input[name="role"]');
  if (roleInputs.length) {
    var sync = function () {
      var role = document.querySelector('input[name="role"]:checked').value;
      document.querySelectorAll('[data-role-field]').forEach(function (f) {
        var active = f.getAttribute('data-role-field') === role;
        f.hidden = !active;
        f.querySelectorAll('input').forEach(function (i) { i.disabled = !active; });
      });
      var focusEl = document.getElementById(role === 'teacher' ? 'password' : 'student_id');
      if (focusEl && document.activeElement && document.activeElement.name === 'role') focusEl.focus();
    };
    roleInputs.forEach(function (r) { r.addEventListener('change', sync); });
    sync();
    var first = document.getElementById(document.querySelector('input[name="role"]:checked').value === 'teacher' ? 'password' : 'student_id');
    if (first && !first.disabled) first.focus();
  }

  // ---- Show / hide password ----
  document.querySelectorAll('[data-toggle-password]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var input = document.getElementById(btn.getAttribute('data-toggle-password'));
      var hidden = input.type === 'password';
      input.type = hidden ? 'text' : 'password';
      btn.textContent = hidden ? 'Hide' : 'Show';
    });
  });
});
