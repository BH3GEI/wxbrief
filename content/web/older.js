(function(){var box=document.getElementById('msgs'),bar=document.getElementById('olderbar'),cid=box.dataset.chat,busy=false,done=false;
function say(t){bar.textContent=t||''}
function more(){if(busy||done)return;var f=box.querySelector('.m');if(!f){return}busy=true;say('加载更早的消息…');
 var top0=f.getBoundingClientRect().top;
 fetch('/older?c='+encodeURIComponent(cid)+'&ts='+f.dataset.ts+'&id='+f.dataset.id,{credentials:'same-origin',cache:'no-store'})
 .then(function(r){return r.json()}).then(function(d){
  if(d.error){say((d.message||'读取失败')+(d.retry_after?'（'+d.retry_after+' 秒后再往上划一下）':'，往上再划一下重试'));busy=false;return}
  if(d.html){box.insertAdjacentHTML('afterbegin',d.html);var top1=f.getBoundingClientRect().top;window.scrollBy(0,top1-top0)}
  done=!d.more;say(done?'没有更早的消息了':'');busy=false;
  if(!done&&document.documentElement.scrollHeight<=innerHeight+60)more()})
 .catch(function(){say('加载失败，往上再划一下重试');busy=false})}
window.addEventListener('scroll',function(){if(window.scrollY<120)more()},{passive:true});
window.addEventListener('load',function(){if(document.documentElement.scrollHeight<=innerHeight+60)more()});
})()
