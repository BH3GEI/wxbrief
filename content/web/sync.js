(function(){
/* Chat pages open from the local cache at once; Telegram is synced in the background (started by the server when the
   page was rendered). This polls /sync and puts new messages into the page in time order, without a reload. */
var box=document.getElementById('msgs');if(!box)return;
var cid=box.dataset.chat,after=+box.dataset.maxid||0,note=document.getElementById('syncnote'),pill=null,timer=null;
function auto(){try{return localStorage.getItem('wxb_auto')!=='0'}catch(e){return true}}
function atBottom(){return window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-120}
function down(){window.scrollTo(0,document.documentElement.scrollHeight)}
function say(t){if(!note)return;note.textContent=t||'';note.hidden=!t}
function showPill(){if(pill)return;pill=document.createElement('button');pill.className='newpill';pill.textContent='↓ 有新消息';
 pill.onclick=function(){down();pill.remove();pill=null};document.body.appendChild(pill)}
function merge(html){var t=document.createElement('div'),n=0;t.innerHTML=html;
 [].slice.call(t.children).forEach(function(el){if(!el.id||document.getElementById(el.id))return;
  var ts=+el.dataset.ts,id=+el.dataset.id,kids=box.children,i=kids.length;
  while(i>0){var k=kids[i-1],kt=+k.dataset.ts,ki=+k.dataset.id;if(kt<ts||(kt===ts&&ki<id))break;i--}
  box.insertBefore(el,kids[i]||null);n++});
 return n}
function later(running){clearTimeout(timer);timer=setTimeout(poll,running?700:10000)}
function poll(force){
 if(window.__leaving)return;
 if(!force&&document.visibilityState!=='visible'){later(false);return}
 fetch('/sync?c='+encodeURIComponent(cid)+'&after='+after+(force?'&force=1':''),{credentials:'same-origin',cache:'no-store'})
 .then(function(r){return r.json()}).then(function(d){
  if(window.__leaving)return;
  var bottom=atBottom(),empty=!box.children.length,n=d.html?merge(d.html):0;
  after=Math.max(after,d.maxid||0);
  if(n){if(bottom||empty)down();else showPill()}
  if(d.running)say(box.children.length?'正在同步新消息…':'正在从 Telegram 读取最近的消息…');
  else if(d.error)say('现在读不到 Telegram 的新消息：'+d.error+(d.retry_after?'（'+d.retry_after+' 秒后再试）':'')+'。显示的是已缓存的。');
  else say(box.children.length?'':'这个会话还没有消息。');
  if(d.running||auto())later(d.running)})
 .catch(function(){later(false)})}
window.wxbSync=poll;
poll();
})();
