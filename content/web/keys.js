function cp(b,only){var d=b.parentNode,t=only?b.dataset.k:d.querySelector('textarea:not([hidden])').value,o=b.textContent;
 function done(ok){b.textContent=ok?'已复制':'复制失败，请长按文本框手动复制';setTimeout(function(){b.textContent=o},2000)}
 function old(){var a=document.createElement('textarea');a.value=t;a.setAttribute('readonly','');a.style.position='fixed';a.style.opacity='0';
  document.body.appendChild(a);a.select();a.setSelectionRange(0,t.length);var ok=false;try{ok=document.execCommand('copy')}catch(e){}a.remove();done(ok)}
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(function(){done(true)},old)}else{old()}}
function pick(b,w){var d=b.parentNode.parentNode;d.querySelectorAll('textarea').forEach(function(t){t.hidden=t.dataset.w!==w});
 d.querySelectorAll('.tab2').forEach(function(x){x.classList.toggle('on',x===b)})}
document.querySelectorAll('form.two').forEach(function(f){var b=f.querySelector('button'),o=b.textContent,tm;
 f.addEventListener('submit',function(ev){if(f.dataset.armed){return}ev.preventDefault();f.dataset.armed='1';b.textContent=b.dataset.ask;b.classList.add('armed');
  clearTimeout(tm);tm=setTimeout(function(){delete f.dataset.armed;b.textContent=o;b.classList.remove('armed')},5000)})});
