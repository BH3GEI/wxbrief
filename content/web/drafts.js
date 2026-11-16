function cpdraft(b){var t=b.dataset.t,o=b.textContent;
 function done(ok){b.textContent=ok?'已复制':'复制失败，请长按手动复制';setTimeout(function(){b.textContent=o},2000)}
 function old(){var a=document.createElement('textarea');a.value=t;a.setAttribute('readonly','');a.style.position='fixed';a.style.opacity='0';
  document.body.appendChild(a);a.select();a.setSelectionRange(0,t.length);var ok=false;try{ok=document.execCommand('copy')}catch(e){}a.remove();done(ok)}
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(function(){done(true)},old)}else{old()}}
