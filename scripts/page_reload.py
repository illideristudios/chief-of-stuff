"""The reload check every served page carries (injected by pages.py at serve time, not written into the file).

It polls the file's Last-Modified with a HEAD every 5 s and when the tab is shown again (a hidden tab's timers are
throttled). A change reloads the page, except while the user has unsaved input in a form (typing, a picked option): then it shows one notice with a button.
"""

# Set by the timer's reload, so the page server does not count it as a person looking at the board (#229); the
# notice's Reload button is the person's own click and sets none.
RELOAD_COOKIE = "cos-reload=1"

SNIPPET = """<script>
(function(){
  var seen,ready=false;
  function some(sel,f){return Array.prototype.some.call(document.querySelectorAll(sel),f);}
  function selectDirty(s){
    var o=s.options,def=[],cur=[],i;
    for(i=0;i<o.length;i++) if(o[i].defaultSelected) def.push(o[i]);
    if(!def.length&&!s.multiple) for(i=0;i<o.length;i++) if(!o[i].disabled){def=[o[i]];break;}
    for(i=0;i<o.length;i++) if(o[i].selected) cur.push(o[i]);
    if(cur.length!==def.length) return true;
    for(i=0;i<cur.length;i++) if(cur[i]!==def[i]) return true;
    return false;
  }
  function typing(){var a=document.activeElement;
    return a&&(/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)||a.isContentEditable)||
      some('form',function(f){return Array.prototype.some.call(f.elements,function(e){
        if(e.tagName==='SELECT') return selectDirty(e);
        if(!/^(INPUT|TEXTAREA)$/.test(e.tagName)) return false;  // buttons and fieldsets have no default to differ from
        return e.value!==e.defaultValue||e.checked!==e.defaultChecked;
      });});}
  function notice(){
    if(document.getElementById('page-changed'))return;
    var n=document.createElement('div');n.id='page-changed';n.setAttribute('role','status');
    n.style.cssText='position:fixed;right:16px;bottom:16px;z-index:99999;padding:8px 12px;background:#222;color:#fff;font:14px/1.4 system-ui,sans-serif;border-radius:6px';
    n.textContent='This page changed. ';
    var b=document.createElement('button');b.textContent='Reload';b.onclick=function(){location.reload();};
    n.appendChild(b);document.body.appendChild(n);
  }
  function poll(){
    fetch(location.pathname,{method:'HEAD',cache:'no-store'}).then(function(r){
      var t=r.headers.get('Last-Modified');
      if(!ready){seen=t;ready=true;return;}
      if(t!=null&&t!==seen){if(typing())notice();else{document.cookie='%s;max-age=10;path=/';location.reload();}}
    }).catch(function(){});
  }
  poll();setInterval(poll,5000);document.addEventListener('visibilitychange',poll);
})();
</script>
""" % RELOAD_COOKIE


ENCODED = SNIPPET.encode()


def inject(page: bytes) -> bytes:
    """`page` with SNIPPET before its last `</body>`, or appended when it has none."""
    at = page.lower().rfind(b"</body>")
    return page + ENCODED if at < 0 else page[:at] + ENCODED + page[at:]
