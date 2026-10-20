// Виджет поддержки для любого сайта: <script src="https://ВАШ-АДРЕС/widget.js" async></script>
// Рисует круглую кнопку в углу и открывает чат во фрейме — стили сайта и виджета не смешиваются
(function () {
  if (window.__supportWidget) return
  window.__supportWidget = true
  var base = new URL(document.currentScript.src).origin
  fetch(base + '/api/web/config').then(function (r) { return r.json() }).then(function (cfg) {
    var accent = cfg.accent || '#2481cc'
    var btn = document.createElement('button')
    btn.setAttribute('aria-label', cfg.title)
    btn.innerHTML = '<svg width="28" height="28" viewBox="0 0 24 24" fill="#fff"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2z"/></svg>'
    btn.style.cssText = 'position:fixed;right:20px;bottom:20px;width:60px;height:60px;border-radius:50%;border:0;cursor:pointer;' +
      'z-index:2147483646;box-shadow:0 6px 24px rgba(0,0,0,.25);display:grid;place-items:center;background:' + accent
    var frame = document.createElement('iframe')
    frame.title = cfg.title
    frame.src = base + '/chat'
    var mobile = window.matchMedia('(max-width: 500px)').matches
    frame.style.cssText = 'position:fixed;z-index:2147483647;border:0;display:none;background:#fff;box-shadow:0 12px 40px rgba(0,0,0,.25);' +
      (mobile ? 'inset:0;width:100%;height:100%' : 'right:20px;bottom:92px;width:380px;height:600px;max-height:calc(100vh - 110px);border-radius:16px')
    btn.onclick = function () {
      var open = frame.style.display === 'none'
      frame.style.display = open ? 'block' : 'none'
      if (mobile) btn.style.display = open ? 'none' : 'grid'
    }
    // Кнопка «закрыть» внутри чата присылает сообщение родительской странице
    window.addEventListener('message', function (e) {
      if (e.origin === base && e.data === 'support:close') { frame.style.display = 'none'; btn.style.display = 'grid' }
    })
    document.body.appendChild(frame)
    document.body.appendChild(btn)
  })
})()
